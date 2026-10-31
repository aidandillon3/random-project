"""pi-local.bat's llama.cpp router presets (localagent/pi_router.py)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent import catalog, pi_router  # noqa: E402
from localagent.config import Settings  # noqa: E402

HELP = "--jinja --flash-attn [on|off|auto] --fit --n-cpu-moe --models-preset --spec-type draft-mtp --spec-draft-n-max"


class PiRouterTest(unittest.TestCase):
    def test_command_becomes_preset_lines(self):
        lines = pi_router.to_ini(["-m", "E:\\m\\a.gguf", "--host", "127.0.0.1", "--port", "8068", "-c", "32768",
                                  "--jinja", "-fa", "on", "-ctk", "q8_0", "--fit", "on", "-ot", "blk\\.(0)\\.ffn=CPU"])
        self.assertEqual(lines, ["model = E:\\m\\a.gguf", "ctx-size = 32768", "jinja = true", "flash-attn = on",
                                 "cache-type-k = q8_0", "fit = on", "override-tensor = blk\\.(0)\\.ffn=CPU"])

    def test_llamacpp_models_on_the_pc_and_not_strata_ones(self):
        tmp = Path(tempfile.mkdtemp())
        glm = catalog.BY_KEY["glm53-flash-iq2xxs"]
        flash = next(p for p in catalog.PRESETS if p.strata)
        on_pc = {glm.key: tmp / glm.files[0], flash.key: tmp / flash.files[0]}

        def model_path(p, dirs):
            return (on_pc.get(p.key), None, [] if p.key in on_pc else ["x"])
        other = tmp / "My-Model-Q4_K_M.gguf"
        other.write_bytes(b"")
        with mock.patch.object(catalog, "model_path", model_path), \
                mock.patch.object(pi_router.launcher, "_safe_info", lambda m: None):
            items = dict(pi_router.entries(Settings(model_path=str(other)), Path("llama-server.exe"), HELP))
        self.assertEqual(list(items), [glm.key, "My-Model-Q4_K_M"])
        self.assertIn("ctx-size = 32768", items[glm.key])
        self.assertIn(f"model = {tmp / glm.files[0]}", items[glm.key])
        ini = pi_router.write_ini(list(items.items()), tmp / "p.ini").read_text()
        self.assertIn("[glm53-flash-iq2xxs]\nmodel = ", ini)

    def test_each_model_once_per_gpu_and_on_both(self):
        from localagent.gguf import Gpu
        gpus = [Gpu(1, "GPU-3080", "NVIDIA GeForce RTX 3080 Ti", 12.0, 2.5, 8.6, True),
                Gpu(0, "GPU-5080", "NVIDIA GeForce RTX 5080", 16.0, 15.0, 12.0, False)]
        items = pi_router.per_gpu([("swift-27b", ["ctx-size = 49152"])], gpus)
        self.assertEqual(items, [("swift-27b-5080", ["ctx-size = 49152", "device = CUDA0"]),
                                 ("swift-27b-3080ti", ["ctx-size = 49152", "device = CUDA1"]),
                                 ("swift-27b-both", ["ctx-size = 49152", "device = CUDA0,CUDA1"])])
        self.assertEqual(pi_router.per_gpu([("m", [])], gpus[:1]), [("m", [])])


if __name__ == "__main__":
    unittest.main()
