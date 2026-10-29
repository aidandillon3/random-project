-- Nordschleife custom game: the normal creative game, in our own big world.
dofile( "$GAME_DATA/Scripts/game/CreativeGame.lua" )
dofile( "$CONTENT_DATA/Scripts/spawn.lua" )

NordsGame = class( CreativeGame )
NordsGame.worldScriptFilename = "$CONTENT_DATA/Scripts/World.lua"
NordsGame.worldScriptClass = "NordsWorld"

function NordsGame.server_onCreate( self )
	self.data = self.data or {}
	self.data.seed = self.data.seed or 0
	CreativeGame.server_onCreate( self )
end

function NordsGame.server_onPlayerJoined( self, player, newPlayer )
	WeatherManager.Sv_PlayerJoined( player )
	if newPlayer then
		local cellX = math.floor( NORDS_SPAWN.x / 64 )
		local cellY = math.floor( NORDS_SPAWN.y / 64 )
		self.sv.saved.world:loadCell( cellX, cellY, player, "sv_createNewPlayer" )
	end
end

function NordsGame.sv_createNewPlayer( self, world, x, y, player )
	local params = { player = player, x = NORDS_SPAWN.x, y = NORDS_SPAWN.y }
	sm.event.sendToWorld( self.sv.saved.world, "sv_e_spawnNewCharacter", params )
end
