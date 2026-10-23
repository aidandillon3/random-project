"""Scrap Mechanic importer: turn a creation (blueprint) into a drivable BeamNG vehicle.

  game.py       finding the game, its creations and what each shape is
  blueprint.py  reading blueprint.json
  analyze.py    chassis, wheels, suspension, steering and drive of a creation
  voxel.py      the node lattice that follows the blocks
  smmesh.py     the 3D model in the creation's colours
  convert.py    the vehicle: settings, axles, engine, parts
"""
import os
import threading

from . import analyze, blueprint, convert, game
from .convert import DESIGN_FILE, clean, schema

__all__ = ["DESIGN_FILE", "Importer", "clean", "schema"]


class Importer:
    """Remembers what it has read so moving a slider does not read the creation again."""

    def __init__(self):
        self.lock = threading.RLock()
        self._cache = {}

    def creations(self, game_folder, user_folder=""):
        return game.list_creations(game_folder, user_folder)

    def analysis(self, path, game_folder, name="", user_folder=""):
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            raise ValueError("the creation's blueprint.json is missing: " + path)
        key = (os.path.normcase(os.path.abspath(path)), game_folder, user_folder)
        with self.lock:
            hit = self._cache.get(key)
            if hit and hit[0] == stamp:
                return hit[1]
            db = game.shape_db(game_folder, user_folder)
            bp = blueprint.load(path, db)
            an = analyze.Analysis(bp, name or _name_of(path))
            if len(self._cache) > 8:
                self._cache.clear()
            self._cache[key] = (stamp, an)
            return an

    def generate(self, path, game_folder, settings, author="", source=None, name="", user_folder=""):
        with self.lock:
            an = self.analysis(path, game_folder, name, user_folder)
            return convert.generate(an, settings, author, source)

    def forget(self):
        with self.lock:
            self._cache.clear()
        game.forget()


def _name_of(path):
    folder = os.path.dirname(path)
    e = game.creation_entry(folder, "file")
    return e["name"] if e else os.path.basename(folder)
