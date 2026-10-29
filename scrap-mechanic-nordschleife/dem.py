"""Ground height from Mapzen/AWS "terrarium" elevation tiles (dem/<z>_<x>_<y>.png)."""

import glob
import math
import os

import numpy as np
from PIL import Image

URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"


class Dem:
    def __init__(self, folder):
        files = glob.glob(os.path.join(folder, "*.png"))
        keys = [tuple(map(int, os.path.basename(f)[:-4].split("_"))) for f in files]
        self.z = keys[0][0]
        xs = sorted({k[1] for k in keys}); ys = sorted({k[2] for k in keys})
        self.x0, self.y0 = xs[0], ys[0]
        h = np.zeros((len(ys) * 256, len(xs) * 256), dtype=np.float64)
        for f, (_, x, y) in zip(files, keys):
            a = np.asarray(Image.open(f).convert("RGB"), dtype=np.float64)
            tile = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
            h[(y - self.y0) * 256:(y - self.y0 + 1) * 256, (x - self.x0) * 256:(x - self.x0 + 1) * 256] = tile
        self.h = h

    def pixel(self, lat, lon):
        n = 2 ** self.z * 256
        px = (lon + 180) / 360 * n - self.x0 * 256
        py = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n - self.y0 * 256
        return px - 0.5, py - 0.5

    def height(self, lat, lon):
        px, py = self.pixel(lat, lon)
        x0, y0 = int(math.floor(px)), int(math.floor(py))
        fx, fy = px - x0, py - y0
        h = self.h
        return ((h[y0, x0] * (1 - fx) + h[y0, x0 + 1] * fx) * (1 - fy)
                + (h[y0 + 1, x0] * (1 - fx) + h[y0 + 1, x0 + 1] * fx) * fy)
