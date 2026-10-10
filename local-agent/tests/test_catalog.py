import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent import catalog, gguf  # noqa: E402
from localagent.config import Settings  # noqa: E402
from tests.test_launcher import GB, write_gguf  # noqa: E402

FILES = {("u/Flash-GGUF", "M-00001-of-00002.gguf"): os.urandom(300_000),
         ("u/Flash-GGUF", "M-00002-of-00002.gguf"): os.urandom(200_000),
         ("u/Flash-GGUF", "mmproj-M-BF16.gguf"): os.urandom(50_000)}


def preset():
    return catalog.Preset(key="t", title="Test Flash", blurb="", repo="u/Flash-GGUF",
                          files=["M-00001-of-00002.gguf", "M-00002-of-00002.gguf"],
                          mmproj=("u/Flash-GGUF", "mmproj-M-BF16.gguf"), folder="Flash",
                          sizes={f: len(b) for (_, f), b in FILES.items()},
                          launch={"ctx_size": 65536, "speedup": "off", "use_vision": True})


class FakeHF:
    """Serves FILES with Range support, like Hugging Face's resolve URLs."""

    def __init__(self):
        self.ranges, self.fail_after, self.status = [], None, 200
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                repo, _, name = self.path.strip("/").partition("/resolve/main/")
                data = FILES.get((repo, name))
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    return
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                start = 0
                rng = self.headers.get("Range")
                outer.ranges.append(rng)
                if rng:
                    start = int(rng.split("=")[1].split("-")[0])
                    if start >= len(data):
                        self.send_response(416)
                        self.end_headers()
                        return
                body = data[start:]
                self.send_response(206 if rng else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if outer.fail_after is not None:
                    self.wfile.write(body[:outer.fail_after])    # then the connection drops
                    outer.fail_after = None
                    return
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class CatalogTest(unittest.TestCase):
    def setUp(self):
        catalog.CHUNK = 16 * 1024
        self.hf = FakeHF()
        self.addCleanup(self.hf.close)
        self.root = Path(tempfile.mkdtemp())

    def test_presets_are_complete(self):
        for p in catalog.PRESETS:
            self.assertEqual(set(p.sizes), {f for _, f in p.all_files()}, p.key)
            self.assertTrue(p.launch)
        flash = catalog.BY_KEY["swift-flash-next"]
        self.assertEqual(flash.launch["speedup"], "off")       # its GGUFs have no MTP layers
        self.assertGreater(flash.total_bytes(), 60 * catalog.GB)
        self.assertIs(catalog.matching("D:/m/Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00001-of-00002.gguf"), flash)
        self.assertIsNone(catalog.matching("other.gguf"))

    def test_download_resumes_and_finds_everything(self):
        p = preset()
        model, mm, missing = catalog.model_path(p, [self.root])
        self.assertIsNone(model)
        self.assertEqual(len(missing), 3)
        dest = self.root / p.folder
        # the first try breaks off part-way through the first file
        self.hf.fail_after = 100_000
        with self.assertRaises(catalog.DownloadError) as e:
            catalog.download(p, dest, base=self.hf.url)
        self.assertIn("resume", str(e.exception))
        kept = (dest / "M-00001-of-00002.gguf.part").stat().st_size
        self.assertGreater(kept, 0)
        seen = []
        model, mm = catalog.download(p, dest, lambda m, f: seen.append(f), base=self.hf.url)
        self.assertIn(f"bytes={kept}-", self.hf.ranges)             # carried on from where it stopped
        self.assertEqual(model.read_bytes(), FILES[("u/Flash-GGUF", "M-00001-of-00002.gguf")])
        self.assertEqual(mm.name, "mmproj-M-BF16.gguf")
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(catalog.model_path(p, [self.root])[:2], (model, mm))
        # nothing left to fetch
        n = len(self.hf.ranges)
        catalog.download(p, dest, base=self.hf.url)
        self.assertEqual(len(self.hf.ranges), n)

    def test_wrong_size_or_missing_shard_is_not_ready(self):
        p = preset()
        d = self.root / "elsewhere"
        d.mkdir()
        (d / "M-00001-of-00002.gguf").write_bytes(b"x" * 10)        # cut short
        self.assertIsNone(catalog.model_path(p, [self.root])[0])
        (d / "M-00001-of-00002.gguf").write_bytes(FILES[("u/Flash-GGUF", "M-00001-of-00002.gguf")])
        model, mm, missing = catalog.model_path(p, [self.root])
        self.assertIsNone(model)                                     # shard 2 is missing
        self.assertIn("M-00002-of-00002.gguf", missing)

    def test_refused_and_paused_downloads(self):
        p = preset()
        self.hf.status = 403
        with self.assertRaises(catalog.DownloadError) as e:
            catalog.download(p, self.root / "x", base=self.hf.url)
        self.assertIn("accept the terms", str(e.exception))
        self.hf.status = 200
        stop = threading.Event()
        stop.set()
        with self.assertRaises(catalog.DownloadError) as e:
            catalog.download(p, self.root / "x", stop=stop, base=self.hf.url)
        self.assertIn("paused", str(e.exception))

    def test_not_enough_space(self):
        big = preset()
        big.sizes = {f: 10 ** 15 for f in big.sizes}
        with self.assertRaises(catalog.DownloadError) as e:
            catalog.download(big, self.root / "y", base=self.hf.url)
        self.assertIn("Not enough free space", str(e.exception))

    def test_apply_sets_the_server_settings(self):
        s = Settings()
        s.save = lambda *a: None
        catalog.apply(preset(), s, Path("/m/M-00001-of-00002.gguf"), Path("/m/mmproj-M-BF16.gguf"))
        self.assertEqual((s.ctx_size, s.speedup, s.use_vision), (65536, "off", True))
        self.assertEqual(s.mmproj_path, str(Path("/m/mmproj-M-BF16.gguf")))


class LazyTableTest(unittest.TestCase):
    def test_ngram_table_is_left_out_of_memory_planning(self):
        d = Path(tempfile.mkdtemp())
        meta = {"general.architecture": "qwen38moe", "qwen38moe.block_count": 48, "qwen38moe.expert_count": 512,
                "qwen38moe.expert_used_count": 10, "qwen38moe.attention.head_count": 24,
                "qwen38moe.attention.head_count_kv": 2, "qwen38moe.attention.key_length": 256,
                "qwen38moe.embedding_length": 2560, "qwen38moe.full_attention_interval": 4}
        layers = [(f"blk.{i}.ffn_up_exps.weight", int(0.7 * GB)) for i in range(48)] + \
                 [(f"blk.{i}.attn_q.weight", int(0.05 * GB)) for i in range(48)]
        write_gguf(d / "F-00001-of-00002.gguf", meta, [("token_embd.weight", int(0.6 * GB))] + layers)
        write_gguf(d / "F-00002-of-00002.gguf", {}, [("token_ngram_embd.weight", int(26.4 * GB))])
        info = gguf.model_info(d / "F-00001-of-00002.gguf")
        self.assertAlmostEqual(info.lazy_bytes / GB, 26.4, places=1)
        p = gguf.plan(info, 65536, "q8_0", 15.9, extra_gb=0.85)
        self.assertGreater(p.n_cpu_moe, 0)
        self.assertLess(p.ram_gb, 30)                  # the 26 GB table isn't counted as RAM
        self.assertTrue(any("stay on the drive" in n for n in p.notes))
        self.assertLess(p.kv_gb, 1.0)                  # 12 attention layers x 2 KV heads: a tiny cache


if __name__ == "__main__":
    unittest.main()
