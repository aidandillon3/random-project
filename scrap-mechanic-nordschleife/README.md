# Nurburgring Nordschleife for Scrap Mechanic

Generates Scrap Mechanic Large tiles (256 m) with the Nordschleife centre line
(OpenStreetMap relation 38566, 20.75 km) paved with "Blank Road (Matte)" from the
local mod "Sugar Rush Matte Road", 20 m wide, at real-world layout scale (1:1). Ground
heights come from AWS terrarium elevation tiles (`dem/`, zoom 13); the road profile is
smoothed over +-60 m (max grade ~19%), pieces are pitched to follow it, and the ground
is flattened under the road and blended back to the hillside 45 m out (`--flat` for none).

- `smtile.py` reads/writes `.tile` files (format from QuestionableM's SM-Converter;
  round-trips the editor's own file byte for byte).
- `track.py` stitches the OSM ways into one loop and resamples it.
- `dem.py` samples the elevation tiles.
- `build.py --template <Tiles/<uuid> folder of a Large tile with one road piece>`
  writes all 500 tiles of the 25 x 20 area (`.tile`, `.tile.export`, `.tileson`, `description.json`, preview) and a
  `.world` placing them, into `out/Tiles` and `out/Worlds`. Copy those into
  `%APPDATA%\Axolot Games\Scrap Mechanic\User\User_<steamid>\`.

Road piece facts: mesh is 32 x 32 m at scale 1, Y-up; the editor stands it with
rotation (90, yaw, 0), i.e. quaternion Rz(yaw)*Rx(90). Assets go in cell asset list 1.
The full loop at 1:1 touches 99 Large tiles in a 25 x 20 tile area.

The .tileson files carry the road but flat ground (their terrain encoding is unknown), so
re-saving a tile in the Tile Editor would flatten it.
