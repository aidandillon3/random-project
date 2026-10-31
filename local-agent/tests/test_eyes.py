"""Watch mode's own small vision model (localagent/eyes.py): which GPU it goes on, how it's started, and
that watch mode falls back to the chat's model when it isn't there."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent import eyes  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.gguf import Gpu  # noqa: E402

GPUS = [Gpu(0, "GPU-5080", "NVIDIA GeForce RTX 5080", 16.0, 15.0, 12.0, False),
        Gpu(1, "GPU-3080", "NVIDIA GeForce RTX 3080 Ti", 12.0, 2.5, 8.6, True)]


class EyesTest(unittest.TestCase):
    def test_it_goes_on_the_gpu_the_chat_doesnt_use(self):
        self.assertEqual(eyes.pick_gpu(Settings(gpus="GPU-5080"), GPUS).name, "NVIDIA GeForce RTX 3080 Ti")
        self.assertEqual(eyes.pick_gpu(Settings(gpus=""), GPUS).uuid, "GPU-3080")      # "" = the strongest (5080)
        self.assertEqual(eyes.pick_gpu(Settings(gpus="GPU-3080"), GPUS).uuid, "GPU-5080")
        self.assertEqual(eyes.pick_gpu(Settings(gpus="", watch_model_gpu="GPU-5080"), GPUS).uuid, "GPU-5080")
        self.assertEqual(eyes.pick_gpu(Settings(), GPUS[:1]).uuid, "GPU-5080")          # only one: that one
        self.assertIsNone(eyes.pick_gpu(Settings(), []))

    def test_it_starts_whole_on_the_gpu_with_its_vision_file(self):
        args = eyes.build_args(Settings(watch_model_port=8091), Path("llama-server"), Path("m.gguf"), Path("mm.gguf"),
                               "--jinja --flash-attn [on|off|auto] --fit")
        self.assertEqual(args[:5], ["llama-server", "-m", "m.gguf", "--mmproj", "mm.gguf"])
        for flag in ("--jinja", "-ngl", "99", "8091", "-fa", "on"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--fit") + 1], "off")

    def test_missing_files_are_said(self):
        with mock.patch.object(eyes, "files", lambda s: (None, None, ["Qwen3.5-4B-Q6_K.gguf"])), \
                mock.patch.object(eyes.Eyes, "_ours", lambda self, s: False):
            with self.assertRaises(RuntimeError) as e:
                eyes.Eyes().client(Settings())
        self.assertIn("isn't downloaded yet", str(e.exception))


if __name__ == "__main__":
    unittest.main()
