import atexit
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent import gguf, launcher  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.launcher import build_launch, choose_release, find_mmproj, find_models, pick_assets  # noqa: E402

GB = 1024 ** 3

# What a current llama-server --help contains (the parts we look for).
NEW_HELP = """
-fa,   --flash-attn [on|off|auto]  set Flash Attention use
-ctk,  --cache-type-k TYPE
-ncmoe, --n-cpu-moe N
--jinja, --no-jinja
-mm,   --mmproj FILE
--mmproj-auto, --no-mmproj
--spec-type  none, draft-simple, draft-mtp, ngram-mod
-md, --spec-draft-model
--spec-draft-n-max N
-ngld, --gpu-layers-draft
-fit, --fit [on|off]
--cache-reuse N
-cram, --cache-ram N
--spec-draft-sampling {greedy,probabilistic}
-ot, --override-tensor <tensor name pattern>=<buffer type>,...
"""
OLD_HELP = "-fa, --flash-attn\n--jinja\n-ngl\n--n-cpu-moe N\n-md\n--draft-max N\n"


def _s(b: str) -> bytes:
    e = b.encode()
    return struct.pack("<Q", len(e)) + e


def sized_file(path, size: int, head: bytes = b"") -> None:
    """A file `size` bytes long that takes no drive space on Linux, where truncate() leaves a hole.
    On Windows truncate() writes every byte as a real zero (twice that filled a user's C: drive with
    60+ GB), so there a test that needs a file over 64 MB is skipped instead. Deleted when the tests end."""
    if os.name == "nt" and size > 64 * 1024 * 1024:
        raise unittest.SkipTest(f"needs a {size / 1024 ** 3:.1f} GB stand-in file, which Windows would really write")
    with open(path, "wb") as f:
        f.write(head)
        f.truncate(size)

    def drop():
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    atexit.register(drop)


def write_gguf(path: Path, meta: dict, tensors: list[tuple[str, int]], pad_to: int = 0):
    """Tiny GGUF writer: metadata plus tensor infos whose offsets imply the given byte sizes."""
    out = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(meta))
    for k, v in meta.items():
        out += _s(k)
        if isinstance(v, str):
            out += struct.pack("<I", 8) + _s(v)
        elif isinstance(v, list) and v and isinstance(v[0], str):
            out += struct.pack("<IIQ", 9, 8, len(v)) + b"".join(_s(x) for x in v)
        elif isinstance(v, list) and v and isinstance(v[0], bool):
            out += struct.pack("<IIQ", 9, 7, len(v)) + b"".join(struct.pack("<?", x) for x in v)
        elif isinstance(v, list):
            out += struct.pack("<IIQ", 9, 4, len(v)) + b"".join(struct.pack("<I", x) for x in v)
        else:
            out += struct.pack("<II", 4, v)
    off = 0
    for name, size in tensors:
        out += _s(name) + struct.pack("<I", 2) + struct.pack("<QQ", 1, 1) + struct.pack("<IQ", 0, off)
        off += size
    align = 32
    out += b"\0" * ((-len(out)) % align)
    sized_file(path, len(out) + off, out)


def moe_model(path: Path):
    meta = {"general.architecture": "qwen3moe", "general.name": "Test MoE", "qwen3moe.block_count": 4,
            "qwen3moe.expert_count": 128, "qwen3moe.expert_used_count": 8, "qwen3moe.context_length": 262144,
            "qwen3moe.attention.head_count": 32, "qwen3moe.attention.head_count_kv": 4,
            "qwen3moe.attention.key_length": 128, "qwen3moe.attention.value_length": 128,
            "qwen3moe.embedding_length": 2048, "qwen3moe.nextn_predict_layers": 1,
            "tokenizer.ggml.tokens": [f"tok{i}" for i in range(5000)],
            "qwen3moe.attention.head_count_kv_list": [4, 4]}
    tensors = [("token_embd.weight", int(0.5 * GB))]
    for i in range(4):
        tensors += [(f"blk.{i}.attn_q.weight", int(0.2 * GB)), (f"blk.{i}.ffn_up_exps.weight", int(2.5 * GB)),
                    (f"blk.{i}.ffn_down_exps.weight", int(2.5 * GB))]
    write_gguf(path, meta, tensors)


def projector(path: Path, proj_dim: int):
    """A vision file (mmproj) whose header says which model width it is made for."""
    write_gguf(path, {"general.architecture": "clip", "clip.has_vision_encoder": 1,
                      "clip.vision.projection_dim": proj_dim}, [("v.blk.0.attn.weight", 4096)])


def dense_model(path: Path, layer_gb=0.6, layers=40):
    meta = {"general.architecture": "qwen35", "qwen35.block_count": layers, "qwen35.context_length": 131072,
            "qwen35.attention.head_count": 24, "qwen35.attention.head_count_kv": 4,
            "qwen35.embedding_length": 5120, "qwen35.attention.key_length": 256,
            "qwen35.full_attention_interval": 4}
    tensors = [("token_embd.weight", int(0.8 * GB))] + [(f"blk.{i}.ffn_up.weight", int(layer_gb * GB)) for i in range(layers)]
    write_gguf(path, meta, tensors)


class GpuPickTest(unittest.TestCase):
    TWO = ("0, GPU-aaa, NVIDIA GeForce RTX 3080 Ti, 12288, 2900, 8.6\n"
           "1, GPU-bbb, NVIDIA GeForce RTX 5080, 16303, 400, 12.0\n")

    def smi(self, out_by_fields):
        orig = gguf._smi
        self.addCleanup(setattr, gguf, "_smi", orig)
        gguf._smi = lambda fields: out_by_fields(fields)

    def test_reads_every_gpu(self):
        self.smi(lambda f: self.TWO)
        gpus = gguf.list_gpus()
        self.assertEqual([(g.index, g.uuid, g.cc) for g in gpus], [(0, "GPU-aaa", 8.6), (1, "GPU-bbb", 12.0)])
        self.assertAlmostEqual(gpus[1].total_gb, 16303 / 1024)

    def test_an_old_driver_without_compute_cap(self):
        def out(fields):
            if "compute_cap" in fields:
                raise OSError("Field \"compute_cap\" is not a valid field to query.")
            return "0, GPU-aaa, NVIDIA GeForce RTX 3080 Ti, 12288, 2900\n"
        self.smi(out)
        self.assertEqual([(g.name, g.cc) for g in gguf.list_gpus()], [("NVIDIA GeForce RTX 3080 Ti", 0.0)])

    def test_no_nvidia_smi(self):
        def out(fields):
            raise FileNotFoundError("nvidia-smi")
        self.smi(out)
        self.assertEqual(gguf.list_gpus(), [])
        self.assertIsNone(gguf.gpu_info())
        self.assertEqual(gguf.gpu_env("", []), {})

    def test_the_choice(self):
        self.smi(lambda f: self.TWO)
        gpus = gguf.list_gpus()
        # nothing picked: the newest card alone (the 5080, though nvidia-smi numbers it second)
        self.assertEqual([g.index for g in gguf.chosen_gpus("", gpus)], [1])
        self.assertEqual(gguf.gpu_env("", gpus), {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "1"})
        self.assertEqual(gguf.gpu_env("GPU-aaa", gpus)["CUDA_VISIBLE_DEVICES"], "0")
        self.assertEqual(gguf.gpu_env("all", gpus)["CUDA_VISIBLE_DEVICES"], "1,0")     # the main card first
        self.assertEqual(gguf.gpu_env("GPU-gone", gpus)["CUDA_VISIBLE_DEVICES"], "1")  # a card taken out
        name, total, used = gguf.gpu_info("all", gpus)
        self.assertEqual(name, "NVIDIA GeForce RTX 5080 + NVIDIA GeForce RTX 3080 Ti")
        self.assertAlmostEqual(total, (16303 + 12288) / 1024)
        self.assertEqual(gguf.gpu_info("", gpus)[0], "NVIDIA GeForce RTX 5080")

    def test_where_the_monitors_are(self):
        def out(fields):
            if "display_active" not in fields:
                raise AssertionError("asked first")
            return ("0, GPU-aaa, NVIDIA GeForce RTX 3080 Ti, 12288, 2900, 8.6, Enabled\n"
                    "1, GPU-bbb, NVIDIA GeForce RTX 5080, 16303, 400, 12.0, Disabled\n")
        self.smi(out)
        gpus = gguf.list_gpus()
        self.assertEqual([g.display for g in gpus], [True, False])
        self.assertFalse(gguf.monitors_on("", gpus))            # the AI on the 5080, the desktop on the 3080 Ti
        self.assertTrue(gguf.monitors_on("all", gpus))
        self.assertTrue(gguf.monitors_on("GPU-aaa", gpus))
        self.smi(lambda f: self.TWO)                            # the driver doesn't say: taken to be on it
        self.assertTrue(gguf.monitors_on("", gguf.list_gpus()))
        self.assertTrue(gguf.monitors_on("", []))

    def test_the_server_gets_the_gpus(self):
        d = Path(tempfile.mkdtemp())
        script = d / "env.py"
        script.write_text("import os\n"
                          "print('seen', os.environ.get('CUDA_VISIBLE_DEVICES'), os.environ.get('PATH') is not None)\n")
        proc = launcher.ServerProcess()
        proc.env = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "1"}
        self.assertEqual(proc.run_step([sys.executable, str(script)], d), 0)
        self.assertIn("seen 1 True", list(proc.lines))           # added to the environment, not instead of it
        self.assertIn("GPUs: CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=1", list(proc.lines))


class GGUFTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_reads_moe_info(self):
        p = self.dir / "Test-MoE-Q4_K_M.gguf"
        moe_model(p)
        info = gguf.model_info(p)
        self.assertEqual((info.arch, info.n_layers, info.n_experts, info.n_experts_used), ("qwen3moe", 4, 128, 8))
        self.assertTrue(info.is_moe)
        self.assertTrue(info.has_mtp)
        self.assertEqual(info.quant, "Q4_K_M")
        self.assertAlmostEqual(info.expert_bytes[0] / GB, 5.0, places=2)
        self.assertEqual(info.kv_bytes_per_token_f16, 4 * 4 * 256 * 2)
        self.assertIn("MoE", info.summary())

    def test_moe_plan_moves_experts_to_ram(self):
        p = self.dir / "m.gguf"
        moe_model(p)   # ~21.3 GB
        pl = gguf.plan(gguf.model_info(p), 32768, "q8_0", 16.0)
        self.assertFalse(pl.fits_fully)
        self.assertEqual(pl.gpu_layers, 999)
        self.assertGreaterEqual(pl.n_cpu_moe, 2)
        self.assertLessEqual(pl.gpu_gb, 16.0)
        self.assertGreater(pl.tokens_per_sec, 0)

    def test_dense_hybrid_kv_and_partial_offload(self):
        p = self.dir / "d.gguf"
        dense_model(p)   # 24.8 GB, does not fit
        info = gguf.model_info(p)
        self.assertEqual(info.kv_bytes_per_token_f16, 4 * 10 * (256 + 256) * 2)  # 10 attention layers of 40
        pl = gguf.plan(info, 32768, "q8_0", 16.0)
        self.assertLess(pl.gpu_layers, 40)
        self.assertGreater(pl.gpu_layers, 10)
        self.assertTrue(any("CPU" in n for n in pl.notes))

    def test_hybrid_kv_follows_llama_cpp(self):
        base = {"general.architecture": "qwen35", "qwen35.block_count": 65, "qwen35.nextn_predict_layers": 1,
                "qwen35.attention.head_count": 24, "qwen35.attention.head_count_kv": 4,
                "qwen35.embedding_length": 5120, "qwen35.attention.key_length": 256}
        per_layer = 4 * 512 * 2
        # no full_attention_interval: llama.cpp takes every 4th of the 64 layers, plus the MTP layer
        write_gguf(self.dir / "a.gguf", base, [("blk.0.x", 1000)])
        self.assertEqual(gguf.model_info(self.dir / "a.gguf").kv_bytes_per_token_f16, (16 + 1) * per_layer)
        # attention.recurrent_layers wins over the interval (here: 8 attention layers + the MTP layer)
        rec = [i % 8 != 7 for i in range(64)] + [False]
        write_gguf(self.dir / "b.gguf", dict(base, **{"qwen35.full_attention_interval": 4,
                                                       "qwen35.attention.recurrent_layers": rec}), [("blk.0.x", 1000)])
        self.assertEqual(gguf.model_info(self.dir / "b.gguf").kv_bytes_per_token_f16, 9 * per_layer)
        # a model that isn't hybrid keeps every layer
        write_gguf(self.dir / "c.gguf", {"general.architecture": "llama", "llama.block_count": 32,
                                         "llama.attention.head_count": 32, "llama.attention.head_count_kv": 8,
                                         "llama.embedding_length": 4096}, [("blk.0.x", 1000)])
        self.assertEqual(gguf.model_info(self.dir / "c.gguf").kv_bytes_per_token_f16, 32 * 8 * 256 * 2)

    def test_sliding_window_layers_keep_only_their_window(self):
        meta = {"general.architecture": "gemma3", "gemma3.block_count": 48, "gemma3.context_length": 131072,
                "gemma3.attention.head_count": 16, "gemma3.attention.head_count_kv": 8,
                "gemma3.attention.key_length": 256, "gemma3.embedding_length": 3840,
                "gemma3.attention.sliding_window": 1024}
        write_gguf(self.dir / "g.gguf", meta, [("blk.0.x", 1000)])
        info = gguf.model_info(self.dir / "g.gguf")
        self.assertEqual(info.kv_bytes_per_token_f16, 8 * 8 * 512 * 2)        # 8 of 48 layers see everything
        self.assertEqual(info.swa_kv_bytes_per_token_f16, 40 * 8 * 512 * 2)
        self.assertEqual(info.swa_window, 1024)
        full = 48 * 8 * 512 * 2 * 131072 / GB                                     # what every layer would take
        self.assertLess(gguf.kv_cache_gb(info, 131072, "f16"), full / 5)
        # the pattern can also come from the file, as a list
        write_gguf(self.dir / "h.gguf", dict(meta, **{"gemma3.attention.sliding_window_pattern": [True, False] * 24}),
                   [("blk.0.x", 1000)])
        self.assertEqual(gguf.model_info(self.dir / "h.gguf").kv_bytes_per_token_f16, 24 * 8 * 512 * 2)

    def test_small_model_fits(self):
        p = self.dir / "s.gguf"
        dense_model(p, layer_gb=0.2, layers=40)   # 8.8 GB
        pl = gguf.plan(gguf.model_info(p), 32768, "q8_0", 16.0)
        self.assertTrue(pl.fits_fully)
        self.assertEqual(pl.gpu_layers, 999)

    def test_context_that_fits(self):
        p = self.dir / "Swift-1.5-Qwen3.8-27B-IQ3_XXS-mtp.gguf"
        dense_model(p, layer_gb=(11.3 - 0.8) / 64, layers=64)   # about the size of Aidan's model
        info = gguf.model_info(p)
        pl = gguf.plan(info, 16384, "q8_0", 15.9, extra_gb=0.63)   # with the vision file
        self.assertTrue(pl.fits_fully)
        self.assertTrue(48 * 1024 <= pl.ctx_fit <= 64 * 1024, pl.ctx_fit)
        self.assertTrue(any("room for about" in n for n in pl.notes))
        # no nagging when the context is already close to the most that fits
        self.assertFalse(any("room for" in n for n in gguf.plan(info, 32768, "q8_0", 15.9, 0.63).notes))
        # q4_0 cache fits about twice as much; never more than the model was trained on
        self.assertGreater(gguf.plan(info, 16384, "q4_0", 15.9, 0.63).ctx_fit, pl.ctx_fit * 1.8)
        self.assertEqual(gguf.plan(info, 16384, "q8_0", 64.0).ctx_fit, 131072)

    def test_not_gguf(self):
        p = self.dir / "x.gguf"
        p.write_bytes(b"nope" * 10)
        with self.assertRaises(gguf.GGUFError):
            gguf.model_info(p)


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.model = self.dir / "Qwen-MoE-Q4_K_M.gguf"
        moe_model(self.model)
        projector(self.dir / "mmproj-F16.gguf", 2048)       # made for this model's width
        self.exe = self.dir / "llama-server"

    def settings(self, **kw):
        return Settings(model_path=str(self.model), **kw)

    def test_new_server_auto_fit(self):
        lp = build_launch(self.settings(), self.exe, NEW_HELP)
        a = " ".join(lp.args)
        for part in ["--jinja", "-fa on", "-ctk q8_0 -ctv q8_0", "--mmproj", "--fit on", "-np 1",
                     "-ub 2048", "-c 32768"]:
            self.assertIn(part, a)
        self.assertNotIn("-ngl", a)
        # llama.cpp turns --cache-reuse off with a vision file and for hybrid models: not sent
        self.assertNotIn("--cache-reuse", a)
        # experts in RAM: llama.cpp's own (8 GB) prompt cache in RAM is kept
        self.assertNotIn("--cache-ram", a)

    def test_old_server_uses_our_plan(self):
        lp = build_launch(self.settings(), self.exe, OLD_HELP)
        a = " ".join(lp.args)
        self.assertIn("-fa", lp.args)
        self.assertNotIn("-fa on", a)
        self.assertIn("-ngl 999", a)
        self.assertIn("--n-cpu-moe", a)
        self.assertNotIn("--fit", a)
        self.assertNotIn("--mmproj-auto", a)

    def test_manual_placement(self):
        lp = build_launch(self.settings(placement="manual", gpu_layers=30, n_cpu_moe=3), self.exe, NEW_HELP)
        a = " ".join(lp.args)
        self.assertIn("-ngl 30", a)
        self.assertIn("--n-cpu-moe 3", a)
        self.assertIn("--fit off", a)

    def test_mtp_keeps_vision(self):
        lp = build_launch(self.settings(speedup="mtp"), self.exe, NEW_HELP)
        a = " ".join(lp.args)
        self.assertIn("--spec-type draft-mtp --spec-draft-n-max 2", a)
        self.assertIn("--spec-draft-sampling probabilistic", a)
        self.assertIn("--mmproj ", a)
        self.assertIn("-np 1", a)
        old = NEW_HELP.replace("--spec-draft-sampling {greedy,probabilistic}", "")
        self.assertNotIn("--spec-draft-sampling", build_launch(self.settings(speedup="mtp"), self.exe, old).args)

    def test_mtp_with_vision_warns_on_builds_before_b11548(self):
        old = NEW_HELP + "version: 11500 (build 11500, commit abc1234)\n"
        lp = build_launch(self.settings(speedup="mtp"), self.exe, old)
        self.assertTrue(any("b11548" in n for n in lp.notes), lp.notes)
        new = NEW_HELP + "version: 11600 (build 11600, commit abc1234)\n"
        self.assertFalse(any("b11548" in n for n in build_launch(self.settings(speedup="mtp"), self.exe, new).notes))
        # no warning without vision, or when the build is unknown
        self.assertFalse(any("b11548" in n for n in
                             build_launch(self.settings(speedup="mtp", use_vision=False), self.exe, old).notes))
        unknown = build_launch(self.settings(speedup="mtp"), self.exe, NEW_HELP)
        self.assertFalse(any("b11548" in n for n in unknown.notes))
        # older --version lines, and the b1234 folder the app unpacks into
        self.assertEqual(launcher.build_number("version: 11435 (deadbee)\n"), 11435)
        self.assertEqual(launcher.build_number("", Path("/x/llama.cpp/b11433/llama-server.exe")), 11433)

    def test_cache_ram_only_when_the_model_is_on_the_gpu(self):
        small = self.dir / "Small-Q4_K_M.gguf"
        dense_model(small, layer_gb=0.2, layers=40)       # 8.8 GB, fits on the GPU
        s = Settings(model_path=str(small), use_vision=False)
        self.assertIn("--cache-ram 24576", " ".join(build_launch(s, self.exe, NEW_HELP).args))
        self.assertNotIn("--cache-ram", build_launch(s, self.exe, NEW_HELP.replace("--cache-ram", "")).args)
        s.placement, s.gpu_layers = "manual", 20                # half the layers on the CPU by hand
        self.assertNotIn("--cache-ram", build_launch(s, self.exe, NEW_HELP).args)

    def test_draft_model(self):
        draft = self.dir / "draft.gguf"
        draft.write_bytes(b"x")
        lp = build_launch(self.settings(speedup="draft", draft_path=str(draft)), self.exe, NEW_HELP)
        a = " ".join(lp.args)
        self.assertIn("-md", lp.args)
        self.assertIn("--spec-type draft-simple", a)
        lp_old = build_launch(self.settings(speedup="draft", draft_path=str(draft)), self.exe, OLD_HELP)
        self.assertIn("--draft-max 8", " ".join(lp_old.args))

    def test_find_models_skips_mmproj_and_later_shards(self):
        for n in ["a-00001-of-00002.gguf", "a-00002-of-00002.gguf"]:
            (self.dir / n).write_bytes(b"GGUF")
        # Strata's draft layer and data, and a folder still being downloaded, aren't chat models
        for sub in ["Strata-data/mtp", "Strata-data/models/swift-iq2_xs", "x/mtp", "y.part"]:
            (self.dir / sub).mkdir(parents=True)
            (self.dir / sub / f"{sub.replace('/', '_')}-q2_0.gguf").write_bytes(b"GGUF")
        names = [p.name for p in find_models([self.dir])]
        self.assertIn("a-00001-of-00002.gguf", names)
        self.assertNotIn("a-00002-of-00002.gguf", names)
        self.assertNotIn("mmproj-F16.gguf", names)
        self.assertFalse([n for n in names if n.endswith("-q2_0.gguf")], names)

    def test_find_mmproj_only_takes_one_made_for_the_model(self):
        d = Path(tempfile.mkdtemp())
        qwen, gemma = d / "Qwen3.8-27B-IQ4_XS.gguf", d / "gemma-3-12b-it-Q4_K_M.gguf"
        dense_model(qwen, layer_gb=0.01, layers=4)           # 5120 wide
        projector(d / "mmproj-gemma-3-12b-it-f16.gguf", 3840)
        self.assertIsNone(find_mmproj(qwen))                   # another model's: llama.cpp would stop at start
        projector(d / "mmproj-Qwen3.8-27B-Q8_0.gguf", 5120)
        projector(d / "mmproj-Qwen3.8-27B-F16.gguf", 5120)
        self.assertEqual(find_mmproj(qwen).name, "mmproj-Qwen3.8-27B-F16.gguf")   # same file, f16 preferred
        # two different ones that fit and whose names don't say which: none is guessed
        e = Path(tempfile.mkdtemp())
        m = e / "my-finetune.gguf"
        dense_model(m, layer_gb=0.01, layers=4)
        projector(e / "mmproj-Alpha-5120-F16.gguf", 5120)
        self.assertEqual(find_mmproj(m).name, "mmproj-Alpha-5120-F16.gguf")
        projector(e / "mmproj-Beta-5120-F16.gguf", 5120)
        self.assertIsNone(find_mmproj(m))
        # what the launcher says about it
        lp = build_launch(Settings(model_path=str(m), use_vision=True), self.exe, NEW_HELP)
        self.assertNotIn("--mmproj", lp.args)
        self.assertTrue(any("made for it" in n for n in lp.notes), lp.notes)
        self.assertFalse(gemma.exists())

    def test_stale_vision_file_is_said(self):
        s = self.settings(mmproj_path=str(self.dir / "moved-away.gguf"))
        lp = build_launch(s, self.exe, NEW_HELP)
        self.assertTrue(any("isn't there any more" in n for n in lp.notes), lp.notes)
        self.assertIn(str(self.dir / "mmproj-F16.gguf"), lp.args)      # the one next to the model fits: used
        (self.dir / "mmproj-F16.gguf").unlink()
        lp = build_launch(s, self.exe, NEW_HELP)
        self.assertIn("--no-mmproj", lp.args)
        self.assertTrue(any("isn't there any more" in n for n in lp.notes), lp.notes)

    def test_extra_options_keep_paths_with_spaces_on_windows(self):
        self.assertEqual(launcher.split_extra('--chat-template-file "C:\\My Files\\t.jinja" -x', windows=True),
                         ["--chat-template-file", "C:\\My Files\\t.jinja", "-x"])
        self.assertEqual(launcher.split_extra('--alias="my model" --temp 0.5', windows=True),
                         ["--alias=my model", "--temp", "0.5"])
        self.assertEqual(launcher.split_extra("--x 'a b'", windows=False), ["--x", "a b"])
        lp = build_launch(self.settings(extra_args='--alias "unclosed'), self.exe, NEW_HELP)
        self.assertIn("--alias", lp.args)
        self.assertTrue(any("quote" in n for n in lp.notes))

    def test_old_server_without_n_cpu_moe_uses_override_tensor(self):
        help_text = "-fa, --flash-attn\n-ot, --override-tensor <pattern>=<type>\n"
        lp = build_launch(self.settings(placement="manual", gpu_layers=99, n_cpu_moe=3), self.exe, help_text)
        self.assertEqual(lp.args[lp.args.index("-ot") + 1], "blk\\.(0|1|2)\\.ffn_.*exps=CPU")
        s = self.settings(placement="manual", gpu_layers=99, n_cpu_moe=3)
        lp = build_launch(s, self.exe, "-fa, --flash-attn\n")
        self.assertNotIn("-ot", lp.args)
        self.assertTrue(any("--n-cpu-moe" in n for n in lp.notes))

    def test_failed_help_is_not_kept(self):
        exe = self.dir / "llama-server"
        exe.write_text("")
        calls = []
        real = launcher._run_text
        self.addCleanup(setattr, launcher, "_run_text", real)
        launcher._run_text = lambda args: calls.append(args) or ""
        self.assertEqual(launcher.server_help(exe), "")
        self.assertTrue(any("gave no --help" in n for n in build_launch(self.settings(), exe, "").notes))
        launcher._help_failed.clear()                           # some time later
        launcher._run_text = lambda args: NEW_HELP if "--help" in args else "version: 11600 (build 11600, commit x)"
        self.assertIn("--jinja", launcher.server_help(exe))
        launcher._run_text = lambda args: ""
        self.assertIn("build 11600", launcher.server_help(exe))  # a good one is kept

    def test_cuda_build_needs_its_runtime_and_a_new_enough_driver(self):
        rel = {"assets": [{"name": n} for n in [
            "llama-b1-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip",
            "llama-b1-bin-win-cuda-13.4-x64.zip", "llama-b1-bin-win-vulkan-x64.zip"]]}
        main, rt = pick_assets(rel)                 # 13.4's runtime isn't there (yet): 12.4 with its runtime
        self.assertEqual((main["name"], rt["name"]),
                         ("llama-b1-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"))
        rel["assets"].append({"name": "cudart-llama-bin-win-cuda-13.4-x64.zip", "state": "starter"})   # uploading
        self.assertEqual(pick_assets(rel)[0]["name"], "llama-b1-bin-win-cuda-12.4-x64.zip")
        rel["assets"][-1]["state"] = "uploaded"
        self.assertEqual(pick_assets(rel)[0]["name"], "llama-b1-bin-win-cuda-13.4-x64.zip")
        self.assertEqual(pick_assets(rel, max_cuda=12)[0]["name"], "llama-b1-bin-win-cuda-12.4-x64.zip")
        # the newest release has a CUDA build without its runtime: the one before it is taken
        rels = [{"tag_name": "b2", "published_at": "2026-10-08T10:00:00Z",
                 "assets": [{"name": "llama-b2-bin-win-cuda-13.4-x64.zip", "state": "uploaded"}]},
                {"tag_name": "b1", "published_at": "2026-10-06T10:00:00Z", "assets": rel["assets"]}]
        r, main, rt = choose_release(rels)
        self.assertEqual((r["tag_name"], rt["name"]), ("b1", "cudart-llama-bin-win-cuda-13.4-x64.zip"))

    def test_download_never_leaves_half_an_install(self):
        import io
        import zipfile

        import requests

        def zipped(files):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for n, data in files.items():
                    z.writestr(n, data)
            return buf.getvalue()

        exe = launcher.EXE
        assets = {"llama-b9-bin-win-cuda-13.4-x64.zip": zipped({exe: "exe", "ggml-cuda.dll": "dll"}),
                  "cudart-llama-bin-win-cuda-13.4-x64.zip": zipped({"cudart64_13.dll": "rt"}),
                  "llama-b9-bin-win-cuda-12.4-x64.zip": zipped({exe: "exe12"}),
                  "cudart-llama-bin-win-cuda-12.4-x64.zip": zipped({"cudart64_12.dll": "rt"})}
        release = [{"tag_name": "b9", "published_at": "2026-10-08T10:00:00Z",
                    "assets": [{"name": n, "browser_download_url": n, "size": len(b)} for n, b in assets.items()]}]
        broken = set()

        class Resp:
            def __init__(self, name):
                self.name, self.headers = name, {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def json(self):
                return release

            def raise_for_status(self):
                if self.name in broken:
                    raise requests.HTTPError("500")

            def iter_content(self, n):
                yield assets[self.name]

        root = Path(tempfile.mkdtemp()) / "llama.cpp"
        for name, value in [("LLAMA_DIR", root), ("driver_version", lambda: 581)]:
            self.addCleanup(setattr, launcher, name, getattr(launcher, name))
            setattr(launcher, name, value)
        self.addCleanup(setattr, launcher.requests, "get", launcher.requests.get)
        launcher.requests.get = lambda url, **kw: Resp(url)
        broken.add("cudart-llama-bin-win-cuda-13.4-x64.zip")
        with self.assertRaises(requests.HTTPError):
            launcher.download_llamacpp()
        self.assertFalse((root / "b9").exists())                      # no CUDA build without its runtime
        found = launcher.find_server(Settings(server_exe=""))          # nor one half unpacked
        self.assertTrue(found is None or "b9.tmp" not in str(found), found)
        broken.clear()
        got = launcher.download_llamacpp()
        self.assertEqual(got, root / "b9" / exe)
        self.assertTrue((root / "b9" / "cudart64_13.dll").exists())
        self.assertFalse((root / "b9.tmp").exists())
        self.assertEqual(launcher.find_server(Settings(server_exe="")), got)
        launcher.driver_version = lambda: 560                         # too old for CUDA 13
        launcher.download_llamacpp()
        self.assertEqual((root / "b9" / exe).read_text(), "exe12")

    def test_pick_assets(self):
        rel = {"assets": [{"name": n} for n in [
            "llama-b1-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip",
            "llama-b1-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip",
            "llama-b1-bin-win-cuda-13.4-arm64.zip", "llama-b1-bin-win-vulkan-x64.zip"]]}
        main, rt = pick_assets(rel)
        self.assertEqual(main["name"], "llama-b1-bin-win-cuda-13.4-x64.zip")
        self.assertEqual(rt["name"], "cudart-llama-bin-win-cuda-13.4-x64.zip")
        self.assertEqual(pick_assets(rel, "vulkan")[0]["name"], "llama-b1-bin-win-vulkan-x64.zip")

    def test_choose_release_skips_latest_without_builds(self):
        # What GitHub returned in October 2026: "latest" was v0.6.0 with no Windows zips,
        # the builds were pre-releases.
        win = lambda tag: [{"name": f"llama-{tag}-bin-win-cuda-13.4-x64.zip"},
                           {"name": "cudart-llama-bin-win-cuda-13.4-x64.zip"},
                           {"name": f"llama-{tag}-bin-win-vulkan-x64.zip"}]
        releases = [
            {"tag_name": "v0.6.0", "published_at": "2026-10-05T16:56:00Z", "assets": [{"name": "source.tar.gz"}]},
            {"tag_name": "b11435", "prerelease": True, "published_at": "2026-10-06T10:00:00Z", "assets": win("b11435")},
            {"tag_name": "b11436", "prerelease": True, "published_at": "2026-10-07T10:00:00Z", "assets": []},  # still uploading
            {"tag_name": "b11433", "prerelease": True, "published_at": "2026-10-05T23:10:00Z", "assets": win("b11433")},
            {"tag_name": "b99999", "draft": True, "published_at": "2026-10-08T10:00:00Z", "assets": win("b99999")},
        ]
        rel, main, rt = choose_release(releases)
        self.assertEqual(rel["tag_name"], "b11435")
        self.assertEqual(main["name"], "llama-b11435-bin-win-cuda-13.4-x64.zip")
        self.assertEqual(rt["name"], "cudart-llama-bin-win-cuda-13.4-x64.zip")
        self.assertEqual(choose_release([releases[0]]), (None, None, None))


if __name__ == "__main__":
    unittest.main()


class PortTest(unittest.TestCase):
    def test_listeners_read_from_netstat(self):
        from localagent.launcher import listeners
        out = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1104
  TCP    127.0.0.1:8070         0.0.0.0:0              LISTENING       23116
  TCP    127.0.0.1:8070         0.0.0.0:0              LISTENING       30480
  TCP    127.0.0.1:8070         127.0.0.1:51234        ESTABLISHED     23116
  TCP    127.0.0.1:18070        0.0.0.0:0              LISTENING       999
"""
        self.assertEqual(listeners(out, 8070), [23116, 30480])
        self.assertEqual(listeners(out, 1), [])
