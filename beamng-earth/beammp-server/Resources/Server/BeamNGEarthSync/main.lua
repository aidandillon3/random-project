-- BeamNG.Earth multiplayer sync (goes with the BeamNG.Earth copy in Resources/Client).
-- BeamNG.Earth moves the world's centre (its floating origin) as you drive. Each game used to pick its own
-- centre, so players' positions stopped matching after a move. Now a game announces each new centre here;
-- this numbers it (newer = bigger) and passes it to every player, and games whose car is near it switch
-- to the same centre.

local seq = 0
local anchors = {} -- [player id] = { lat, lon, seq }: each player's last announced centre

local function line(a, from, mine)
  return string.format('%d|%d|%d|%s|%s', a.seq, from, mine and 1 or 0, a.lat, a.lon)
end

function bngEarthAnnounce(pid, data)
  local lat, lon = string.match(tostring(data), '^([^|]+)|([^|]+)$')
  if not (tonumber(lat) and tonumber(lon)) then return end
  seq = math.max(seq + 1, os.time()) -- keeps growing across server restarts
  local a = { lat = lat, lon = lon, seq = seq }
  anchors[pid] = a
  for id in pairs(MP.GetPlayers() or {}) do
    MP.TriggerClientEvent(id, 'bngEarthAnchor', line(a, pid, id == pid))
  end
end

-- a player switched to someone's centre: from now on that centre is where this player is
function bngEarthOn(pid, data)
  local n, lat, lon = string.match(tostring(data), '^(%d+)|([^|]+)|([^|]+)$')
  n = tonumber(n)
  if not (n and n <= seq and tonumber(lat) and tonumber(lon)) then return end
  local a = { lat = lat, lon = lon, seq = n }
  anchors[pid] = a
  for id in pairs(MP.GetPlayers() or {}) do
    if id ~= pid then MP.TriggerClientEvent(id, 'bngEarthAnchor', line(a, pid, false)) end
  end
end

function bngEarthHello(pid, data)
  for from, a in pairs(anchors) do
    MP.TriggerClientEvent(pid, 'bngEarthAnchor', line(a, from, from == pid))
  end
end

function bngEarthLeft(pid)
  if anchors[pid] then
    anchors[pid] = nil
    MP.TriggerClientEvent(-1, 'bngEarthLeft', tostring(pid))
  end
end

MP.RegisterEvent('bngEarthAnnounce', 'bngEarthAnnounce')
MP.RegisterEvent('bngEarthHello', 'bngEarthHello')
MP.RegisterEvent('bngEarthOn', 'bngEarthOn')
MP.RegisterEvent('onPlayerDisconnect', 'bngEarthLeft')
print('BeamNG.Earth sync loaded')
