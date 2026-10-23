# BeamNG.Earth BeamMP server files

What runs on the Misty BeamMP server (BeamMP-Server v3.9.3), without ServerConfig.toml (it holds the server key).

- `Resources/Client/beamng_earth_mp/`: the mod BeamMP sends to players, BeamNG.Earth 1.5.5-beta (TecnaGamer, MIT,
  see LICENSE-BeamNG.Earth.txt) with this server's changes. `python make_zip.py` packs it into
  `Resources/Client/beamng_earth_mp.zip`, byte for byte the zip on the server.
- `Resources/Server/BeamNGEarthSync/main.lua`: the server side of the position sync.
- `changes-from-upstream.diff`: everything changed from upstream 1.5.5-beta:
  - core.lua: the position sync (players near each other share one world centre), the camera fix after an adopt, and
    far players' cars put to sleep while they are on another world centre (they showed up in the wrong place);
  - camshift.lua: the camera shift's minimum radius (the third-person camera broke after a teleport);
  - rebase.lua: the world centre moves every 25 km (fixed);
  - follow.lua: the terrain block's fast mode from 80 m/s (179 mph) instead of 150.
- `READ ME.txt`: the notes for the friends.

The zip's name must not be `beamng_earth.zip`: BeamMP keeps a player's own mod of the same name switched on, and
that copy has no sync.
