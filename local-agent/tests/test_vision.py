import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent import gguf, vision  # noqa: E402
from tests.test_launcher import write_gguf  # noqa: E402

TMP = Path(tempfile.mkdtemp())


def mmproj(name, proj_dim, vision_enc=1):
    p = TMP / name
    write_gguf(p, {"general.architecture": "clip", "clip.has_vision_encoder": vision_enc,
                   "clip.vision.projection_dim": proj_dim}, [("v.blk.0.attn.weight", 4096)])
    return p.read_bytes()


FILES = {
    # a popular repo whose projector is for a different model size: must be skipped
    ("unsloth/Qwen3.8-27B-GGUF", "mmproj-F16.gguf"): mmproj("a.gguf", 2048),
    ("ggml-org/Qwen3.8-27B-GGUF", "mmproj-Qwen3.8-27B-BF16.gguf"): mmproj("b.gguf", 5120),
    ("ggml-org/Qwen3.8-27B-GGUF", "mmproj-Qwen3.8-27B-Q8_0.gguf"): mmproj("c.gguf", 5120),
}
REPOS = [
    {"id": "someone/Qwen3.8-27B-Uncensored-GGUF", "downloads": 9000},
    {"id": "unsloth/Qwen3.8-27B-GGUF", "downloads": 5000},
    {"id": "ggml-org/Qwen3.8-27B-GGUF", "downloads": 100},
    {"id": "x/Qwen3.8-270B-GGUF", "downloads": 1},
]


class FakeHF:
    def __init__(self):
        self.searches = []

        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data, ctype="application/json"):
                if not isinstance(data, bytes):
                    data = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/api/models":
                    q = parse_qs(u.query)["search"][0]
                    outer.searches.append(q)
                    return self._send(200, [r for r in REPOS if q.lower() in r["id"].lower()])
                if u.path.startswith("/api/models/"):
                    rid = u.path[len("/api/models/"):]
                    sib = [{"rfilename": f, "size": len(b)} for (r, f), b in FILES.items() if r == rid]
                    sib.append({"rfilename": "Qwen3.8-27B-Q4_K_M.gguf", "size": 1})
                    return self._send(200, {"id": rid, "siblings": sib})
                m = u.path.strip("/").split("/resolve/main/")
                if len(m) == 2 and (m[0], m[1]) in FILES:
                    return self._send(200, FILES[(m[0], m[1])], "application/octet-stream")
                return self._send(404, {"error": "nope"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


def swift_model():
    p = TMP / "Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_XXS-mtp.gguf"
    write_gguf(p, {"general.architecture": "qwen35", "general.name": "Swift 1.5 Qwen3.8 27b",
                   "qwen35.block_count": 2, "qwen35.embedding_length": 5120,
                   "qwen35.attention.head_count": 24, "qwen35.attention.head_count_kv": 4}, [("blk.0.x", 100)])
    return p


class VisionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hf = FakeHF()

    def test_base_names(self):
        self.assertEqual(vision.base_names(None, "Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_XXS-mtp.gguf"), ["Qwen3.8-27B"])
        self.assertEqual(vision.base_names(None, "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"), ["Qwen3.6-35B-A3B"])
        self.assertEqual(vision.base_names(None, "gemma-3-27b-it-qat-Q4_0.gguf"), ["gemma-3-27b"])
        info = gguf.model_info(swift_model())
        info.meta["general.base_model.0.repo_url"] = "https://huggingface.co/Qwen/Qwen3.8-27B"
        self.assertEqual(vision.base_names(info, "my-finetune.gguf"), ["Qwen3.8-27B"])
        self.assertEqual(vision.base_names(None, "mystery.gguf"), [])
        # names with -VL- / -Omni- / -Small- in the middle
        self.assertEqual(vision.base_names(None, "Qwen3-VL-8B-Instruct-Q4_K_M.gguf"), ["Qwen3-VL-8B"])
        self.assertEqual(vision.base_names(None, "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"), ["Qwen2.5-VL-7B"])
        self.assertEqual(vision.base_names(None, "Qwen3-VL-30B-A3B-Thinking-Q4_K_M.gguf"), ["Qwen3-VL-30B-A3B"])
        self.assertEqual(vision.base_names(None, "Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf"),
                         ["Mistral-Small-3.2-24B"])
        self.assertEqual(vision.base_names(None, "Llama-3.1-8B-Instruct-Q4_K_M.gguf"), ["Llama-3.1-8B"])

    def test_find_skips_wrong_size_and_prefers_q8(self):
        model = swift_model()
        info = gguf.model_info(model)
        c = vision.find(info, model, base=self.hf.url)
        self.assertEqual((c.repo, c.file), ("ggml-org/Qwen3.8-27B-GGUF", "mmproj-Qwen3.8-27B-Q8_0.gguf"))
        self.assertEqual(c.note, "fits this model")
        self.assertEqual(c.base_name, "Qwen3.8-27B")
        self.assertEqual(self.hf.searches[-1], "Qwen3.8-27B")

    def test_check_rejects(self):
        info = gguf.model_info(swift_model())
        c = vision.Candidate("unsloth/Qwen3.8-27B-GGUF", "mmproj-F16.gguf", base=self.hf.url)
        ok, why = vision.check(c, info)
        self.assertFalse(ok)
        self.assertIn("different model size", why)

    def test_download(self):
        c = vision.Candidate("ggml-org/Qwen3.8-27B-GGUF", "mmproj-Qwen3.8-27B-Q8_0.gguf", base=self.hf.url)
        out_dir = Path(tempfile.mkdtemp())
        p = vision.download(c, out_dir)
        self.assertEqual(p, out_dir / "mmproj-Qwen3.8-27B-Q8_0.gguf")
        self.assertEqual(gguf.read_meta_bytes(p.read_bytes())["clip.vision.projection_dim"], 5120)
        self.assertFalse(list(out_dir.glob("*.part")))

    def test_download_gives_generic_names_the_model_name(self):
        out_dir = Path(tempfile.mkdtemp())
        (out_dir / "mmproj-F16.gguf").write_bytes(b"another model's vision file")
        c = vision.Candidate("unsloth/Qwen3.8-27B-GGUF", "mmproj-F16.gguf", base=self.hf.url, base_name="Qwen3.8-27B")
        p = vision.download(c, out_dir)
        self.assertEqual(p.name, "mmproj-Qwen3.8-27B-F16.gguf")
        self.assertEqual((out_dir / "mmproj-F16.gguf").read_bytes(), b"another model's vision file")
        c = vision.Candidate("ggml-org/Qwen3.8-27B-GGUF", "mmproj-Qwen3.8-27B-Q8_0.gguf", base_name="Qwen3.8-27B")
        self.assertEqual(vision.local_name(c), "mmproj-Qwen3.8-27B-Q8_0.gguf")    # already named after it

    def test_unknown_model(self):
        with self.assertRaises(RuntimeError):
            vision.find(None, Path("mystery.gguf"), base=self.hf.url)


if __name__ == "__main__":
    unittest.main()
