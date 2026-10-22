# BeamNG.Earth test drive (single player)

Copy this folder to `<BeamNG user folder>/current/mods/unpacked/beamng_earth_test`. In single player with
BeamNG.Earth running, open the console (~) and type:

- `bngeTest.run(200)`: drive straight ahead at 200 mph for 60 s from where you are, the way the car faces
- `bngeTest.run(200, 90)`: the same for 90 s
- `bngeTest.again()`: teleport to the last start and drive it again (also after restarting the game)
- `bngeTest.stop()`: end the run now (it is still saved)
- `bngeTest.autoBudget()`: experiment, raises the streaming budget from 2 up to 4 ms per frame while road meshes or
  far tiles queue up (`bngeTest.autoBudget(false)` turns it off)

Results go to `<user folder>/current/beamngearth/tests/<date>_<time>_<mph>mph/` (polls.jsonl, meta.json).
`beamng_earth_test/drive_driver.lua` is BeamNG.Earth's own test driver (TecnaGamer/BeamNG.Earth tools/, MIT),
unchanged.
