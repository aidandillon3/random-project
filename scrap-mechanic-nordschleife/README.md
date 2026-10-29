# Nurburgring Nordschleife for Scrap Mechanic

Generates Scrap Mechanic Large tiles (256 m) with the Nordschleife centre line
(OpenStreetMap relation 38566, 20.75 km) paved with "Blank Road (Matte)" from the
local mod "Sugar Rush Matte Road", 20 m wide, at real-world layout scale (1:1), on flat ground.

- `smtile.py` reads/writes `.tile` files (format from QuestionableM's SM-Converter;
  round-trips the editor's own file byte for byte).
- `track.py` stitches the OSM ways into one loop and resamples it.
- `build.py --template <Tiles/<uuid> folder of a Large tile with one road piece> --blocks 2x2`
  writes tiles (`.tile`, `.tile.export`, `.tileson`, `description.json`, preview) and a
  `.world` placing them, into `out/Tiles` and `out/Worlds`. Copy those into
  `%APPDATA%\Axolot Games\Scrap Mechanic\User\User_<steamid>\`.

Road piece facts: mesh is 32 x 32 m at scale 1, Y-up; the editor stands it with
rotation (90, yaw, 0), i.e. quaternion Rz(yaw)*Rx(90). Assets go in cell asset list 1.
The full loop at 1:1 touches 99 Large tiles in a 25 x 20 tile area.
