dofile( "$GAME_DATA/Scripts/game/worlds/CreativeBaseWorld.lua" )
dofile( "$CONTENT_DATA/Scripts/spawn.lua" )

NordsWorld = class( CreativeBaseWorld )
NordsWorld.terrainScript = "$CONTENT_DATA/Scripts/terrain.lua"
NordsWorld.enableSurface = true
NordsWorld.enableAssets = true
NordsWorld.enableClutter = true
NordsWorld.enableNodes = true
NordsWorld.enableCreations = true
NordsWorld.enableHarvestables = true
NordsWorld.enableKinematics = true
NordsWorld.enableVoxelTerrain = true
NordsWorld.cellMinX = NORDS_BOUNDS.xMin
NordsWorld.cellMaxX = NORDS_BOUNDS.xMax
NordsWorld.cellMinY = NORDS_BOUNDS.yMin
NordsWorld.cellMaxY = NORDS_BOUNDS.yMax
NordsWorld.worldBorder = true
