import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent import gguf  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.launcher import build_launch, choose_release, find_models, pick_assets  # noqa: E402

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
"""
OLD_HELP = "-fa, --flash-attn\n--jinja\n-ngl\n--n-cpu-moe N\n-md\n--draft-max N\n"


def _s(b: str) -> bytes:
    e = b.encode()
    return struct.pack("<Q", len(e)) + e


def write_gguf(path: Path, meta: dict, tensors: list[tuple[str, int]], pad_to: int = 0):
    """Tiny GGUF writer: metadata plus tensor infos whose offsets imply the given byte sizes."""
    out = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(meta))
    for k, v in meta.items():
        out += _s(k)
        if isinstance(v, str):
            out += struct.pack("<I", 8) + _s(v)
        elif isinstance(v, list) and v and isinstance(v[0], str):
            out += struct.pack("<IIQ", 9, 8, len(v)) + b"".join(_s(x) for x in v)
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
    with open(path, "wb") as f:
        f.write(out)
        f.truncate(len(out) + off)   # sparse file: sizes without real data


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


def dense_model(path: Path, layer_gb=0.6, layers=40):
    meta = {"general.architecture": "qwen35", "qwen35.block_count": layers, "qwen35.context_length": 131072,
            "qwen35.attention.head_count": 24, "qwen35.attention.head_count_kv": 4,
            "qwen35.embedding_length": 5120, "qwen35.attention.key_length": 256,
            "qwen35.full_attention_interval": 4}
    tensors = [("token_embd.weight", int(0.8 * GB))] + [(f"blk.{i}.ffn_up.weight", int(layer_gb * GB)) for i in range(layers)]
    write_gguf(path, meta, tensors)


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

    def test_small_model_fits(self):
        p = self.dir / "s.gguf"
        dense_model(p, layer_gb=0.2, layers=40)   # 8.8 GB
        pl = gguf.plan(gguf.model_info(p), 32768, "q8_0", 16.0)
        self.assertTrue(pl.fits_fully)
        self.assertEqual(pl.gpu_layers, 999)

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
        (self.dir / "mmproj-F16.gguf").write_bytes(b"x" * 1000)
        self.exe = self.dir / "llama-server"

    def settings(self, **kw):
        return Settings(model_path=str(self.model), **kw)

    def test_new_server_auto_fit(self):
        lp = build_launch(self.settings(), self.exe, NEW_HELP)
        a = " ".join(lp.args)
        for part in ["--jinja", "-fa on", "-ctk q8_0 -ctv q8_0", "--mmproj", "--fit on", "-np 1",
                     "-ub 2048", "--cache-reuse 256", "-c 32768"]:
            self.assertIn(part, a)
        self.assertNotIn("-ngl", a)

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

    def test_mtp_turns_vision_off(self):
        lp = build_launch(self.settings(speedup="mtp"), self.exe, NEW_HELP)
        a = " ".join(lp.args)
        self.assertIn("--spec-type draft-mtp", a)
        self.assertNotIn("--mmproj ", a + " ")
        self.assertTrue(any("vision" in n for n in lp.notes))

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
        names = [p.name for p in find_models([self.dir])]
        self.assertIn("a-00001-of-00002.gguf", names)
        self.assertNotIn("a-00002-of-00002.gguf", names)
        self.assertNotIn("mmproj-F16.gguf", names)

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
