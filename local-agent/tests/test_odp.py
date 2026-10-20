import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent import catalog, odp  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.launcher import build_launch  # noqa: E402
from tests.test_launcher import NEW_HELP  # noqa: E402


def zipped(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeGitHub:
    """The releases API of the private repo, which wants the token."""

    def __init__(self, releases: list, assets: dict):
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.seen.append((self.path, self.headers.get("Authorization"), self.headers.get("Accept")))
                if self.headers.get("Authorization") != "Bearer tok":
                    body, status = json.dumps({"message": "Not Found"}).encode(), 404
                elif self.path.startswith(f"/repos/{odp.REPO}/releases"):
                    body, status = json.dumps(releases).encode(), 200
                elif self.path in assets and self.headers.get("Accept") == "application/octet-stream":
                    body, status = assets[self.path], 200
                else:
                    body, status = b"", 404
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def release(tag, when, url, name=odp.ASSET, draft=False):
    return {"tag_name": tag, "published_at": when, "draft": draft,
            "assets": [{"name": name, "url": url, "state": "uploaded", "size": 10}]}


class OdpTest(unittest.TestCase):
    def setUp(self):
        self.preset = catalog.BY_KEY["qwen27b-1bit-odp"]
        self.root = Path(tempfile.mkdtemp()) / "odp-llama"

    def test_the_preset(self):
        p = self.preset
        self.assertTrue(odp.supports(p))
        self.assertFalse(odp.supports(catalog.BY_KEY["swift-27b"]))
        # Unsloth's 1-bit file and the 64 prefill blocks trained for it, all in one folder
        self.assertEqual(p.files, ["Qwen3.8-27B-UD-IQ1_S.gguf"])
        blocks = [f for r, f in p.extra]
        self.assertEqual(len(blocks), 64)
        self.assertEqual({r for r, _ in p.extra}, {"ISTA-DASLab/Qwen3.8-27B-NVFP4-prefiller"})
        self.assertEqual(blocks[0], p.odp["blocks"])
        self.assertEqual(blocks[-1], "Qwen3.8-27B-ODP-IQ1_S-00064-of-00064.gguf")
        # Hugging Face's sizes (2026-10-20): the decoder and 13,731,156,992 bytes of blocks
        self.assertEqual(sum(p.sizes[f] for f in blocks), 13731156992)
        self.assertAlmostEqual(p.total_bytes() / catalog.GB, 19.9, delta=0.05)
        # no draft (a verify batch would stream the blocks), no vision
        self.assertEqual(p.launch["speedup"], "off")
        self.assertFalse(p.launch["use_vision"])
        self.assertNotIn("engine", p.launch)        # Settings.engine is Strata's choice; it stays as it was
        self.assertIs(catalog.matching(f"D:/m/{p.files[0]}"), p)

    def test_blocks_must_sit_beside_the_decoder(self):
        p, d = self.preset, Path(tempfile.mkdtemp())
        folder = d / p.folder
        folder.mkdir()
        for _, f in p.all_files():
            (folder / f).write_bytes(b"")
        p2 = catalog.Preset(**{**p.__dict__, "sizes": {}})       # no size check for empty stand-ins
        model, _, missing = catalog.model_path(p2, [d])
        self.assertEqual(model, folder / p.files[0])
        self.assertEqual(missing, [])
        self.assertEqual(odp.blocks_path(p, model), folder / p.odp["blocks"])
        (folder / p.extra[5][1]).unlink()
        model, _, missing = catalog.model_path(p2, [d])
        self.assertIsNone(model)
        self.assertEqual(missing, [p.extra[5][1]])

    def test_launch_streams_the_blocks_and_skips_speedups(self):
        model = Path(tempfile.mkdtemp()) / self.preset.files[0]
        s = Settings(model_path=str(model), speedup="mtp", use_vision=False)
        blocks = model.parent / self.preset.odp["blocks"]
        lp = build_launch(s, Path("llama-server"), NEW_HELP, odp_blocks=blocks)
        a = lp.args
        self.assertEqual(a[a.index("--odp-blocks") + 1], str(blocks))
        self.assertNotIn("--spec-type", a)
        self.assertTrue(any("No speed-up" in n for n in lp.notes))
        self.assertNotIn("-ub", a)          # the fork raises the batch to 8192 itself
        self.assertNotIn("--odp-blocks", build_launch(s, Path("llama-server"), NEW_HELP).args)

    def test_pick_release(self):
        rels = [release("odp-llama-b2-a-1", "2026-10-01", "u1"), release("odp-llama-b2-a-2", "2026-10-03", "u2"),
                release("odp-llama-b2-a-3", "2026-10-05", "u3", draft=True), release("b9000", "2026-10-09", "u4"),
                release("odp-llama-b2-a-4", "2026-10-04", "u5", name="other.zip"),
                release("odp-llama-a0557fc-7496aff", "2026-10-06", "u6")]       # build 1: no OpenSSL DLLs
        rel, asset = odp.pick_release(rels)
        self.assertEqual((rel["tag_name"], asset["url"]), ("odp-llama-b2-a-2", "u2"))
        self.assertEqual(odp.pick_release([]), (None, None))

    def test_download_with_the_token(self):
        data = zipped({odp.EXE: b"exe", "ggml-cuda.dll": b"dll", "BUILD.json": json.dumps({"build": odp.BUILD})})
        rels = [release(f"odp-llama-b{odp.BUILD}-a-b", "2026-10-20", "")]
        gh2 = FakeGitHub(rels, {"/asset/1": data})
        self.addCleanup(gh2.httpd.shutdown)
        rels[0]["assets"][0]["url"] = f"{gh2.url}/asset/1"      # the asset's API url, as GitHub lists it
        exe = odp.download(Settings(github_token="tok"), root=self.root, api=gh2.url)
        self.assertEqual(exe, self.root / odp.EXE)
        self.assertEqual(exe.read_bytes(), b"exe")
        self.assertEqual(odp.find_server(self.root), exe)
        self.assertTrue(all(auth == "Bearer tok" for _, auth, _ in gh2.seen))
        # a newer build replaces the old one, leaving nothing of it behind
        (self.root / "stale.dll").write_bytes(b"old")
        odp.download(Settings(github_token="tok"), root=self.root, api=gh2.url)
        self.assertFalse((self.root / "stale.dll").exists())

    def test_an_older_build_counts_as_missing(self):
        self.root.mkdir(parents=True)
        (self.root / odp.EXE).write_bytes(b"exe")
        (self.root / "BUILD.json").write_text('{"fork": "x", "commit": "y"}', encoding="utf-8-sig")   # build 1
        self.assertIsNone(odp.find_server(self.root))
        self.assertTrue(odp.outdated(self.root))
        (self.root / "BUILD.json").write_text(json.dumps({"build": odp.BUILD}), encoding="utf-8-sig")
        self.assertEqual(odp.find_server(self.root), self.root / odp.EXE)
        self.assertFalse(odp.outdated(self.root))

    def test_download_says_what_is_wrong(self):
        gh = FakeGitHub([], {})
        self.addCleanup(gh.httpd.shutdown)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("GH_TOKEN", None)
        with self.assertRaisesRegex(RuntimeError, "refused"):
            odp.download(Settings(github_token="wrong"), root=self.root, api=gh.url)
        with self.assertRaisesRegex(RuntimeError, "No working build"):
            odp.download(Settings(github_token="tok"), root=self.root, api=gh.url)
        self.assertIsNone(odp.find_server(self.root))


if __name__ == "__main__":
    unittest.main()
