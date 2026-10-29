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

-- Same as CreativeBaseWorld's, but the new character faces down the track.
function NordsWorld.sv_e_spawnNewCharacter( self, params )
	local spawnRayBegin = sm.vec3.new( params.x, params.y, 1024 )
	local spawnRayEnd = sm.vec3.new( params.x, params.y, -1024 )
	local oldCharacter = params.player:getCharacter()
	local valid, result = sm.physics.spherecast( spawnRayBegin, spawnRayEnd, 0.3, oldCharacter )
	local pos
	if valid then
		pos = sm.vec3.lerp( spawnRayBegin, spawnRayEnd, result.fraction ) + sm.vec3.new( 0, 0, 0.4 )
	else
		pos = sm.vec3.new( params.x, params.y, 100 )
	end

	local character = sm.character.createCharacter( params.player, self.world, pos, NORDS_SPAWN.yaw or 0, 0 )
	params.player:setCharacter( character )
end
