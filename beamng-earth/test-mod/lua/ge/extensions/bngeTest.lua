-- BeamNG.Earth test drive: the same drive every time, recorded for comparing changes. Single player only.
--
-- Console (~), GE Lua:
--   bngeTest.run(200)          drive straight ahead at 200 mph for 60 s from where you are, the way the car faces
--   bngeTest.run(200, 90)      ... for 90 s
--   bngeTest.again()           teleport to the last run's start and drive it again (same heading, speed, time)
--   bngeTest.stop()            end the run now (it is still saved)
--
-- A stock test car (vivace) is spawned at your car, which is frozen meanwhile and given back at the end (BeamNG.Earth's
-- tools/drive.py does the same: per-frame placement made a lifted car unstable). The car is moved along the line by
-- BeamNG.Earth's own test driver (beamng_earth_test/drive_driver.lua, from TecnaGamer/BeamNG.Earth tools/, MIT), so
-- the speed and the path never depend on driving. Every second this records the driver's frames and the mod's state
-- (as tools/drive.py does), plus how far the terrain block reaches ahead of the car each frame.
-- Saved at the end in <user folder>/beamngearth/tests/<date>_<time>_<mph>mph/: polls.jsonl and meta.json.

local M = {}

local DRIVER = '/beamng_earth_test/drive_driver.lua'
local OUT = 'beamngearth/tests/'
local POLL_S = 1.0
local SPAWN_SETTLE_S = 3.0      -- the test car spawned and on the ground
local ARRIVE_MAX_S = 300        -- again(): the teleport's new anchor within this
local TELEPORT_SETTLE_S = 45    -- ... then the world around it loads (tools/autodrive_perf.py --settle)
local MPH = 0.44704
local TEST_VEHICLE = 'vivace'

local run = nil     -- the run in progress
local last = nil    -- { mph, seconds, lat, lon, heading } of the last run started

local function say(msg, kind)
  log(kind == 'error' and 'E' or 'I', 'bngeTest', msg)
  pcall(guihooks.trigger, 'toastrMsg', { type = kind or 'info', title = 'Test drive', msg = msg, config = { timeOut = 6000 } })
end

local function coreStatus()
  local core = rawget(_G, 'beamngEarth_core')
  if not core or not core.status then return nil end
  local ok, s = pcall(function() return loadstring('return ' .. core.status())() end)
  return ok and s or nil
end

local function coreState()
  local core = rawget(_G, 'beamngEarth_core')
  return core and core._state and core._state() or nil
end

-- How far the terrain block's collidable ground reaches ahead of (x, y) along heading h (deg, 0 = +y, 90 = +x).
local lattice = nil
local function blockAhead(x, y, h)
  local st = coreState()
  local b = st and st.block
  if not b then return nil end
  lattice = lattice or require('beamngEarth/lattice')
  local x0, y0, x1, y1 = lattice.groundBox(b.cx, b.cy)
  local ux, uy = math.sin(math.rad(h)), math.cos(math.rad(h))
  local d = math.huge
  if ux > 1e-6 then d = math.min(d, (x1 - x) / ux) elseif ux < -1e-6 then d = math.min(d, (x0 - x) / ux) end
  if uy > 1e-6 then d = math.min(d, (y1 - y) / uy) elseif uy < -1e-6 then d = math.min(d, (y0 - y) / uy) end
  return d
end

local function restoreCar(r)
  local t = r.testId and scenetree.findObjectById(r.testId)
  if t then t:delete() end
  local car = r.carId and scenetree.findObjectById(r.carId)
  if car then
    car:setActive(1)
    be:enterVehicle(0, car)
  end
  pcall(commands.setGameCamera)
  local core = rawget(_G, 'beamngEarth_core')
  if car and core and r.lat then pcall(core.teleport, r.lat, r.lon, 'test drive start', { record = false }) end
end

local function save(r)
  local folder = OUT .. os.date('%Y%m%d_%H%M%S', r.startedAt) .. string.format('_%dmph/', r.mph)
  local lines = {}
  for i, p in ipairs(r.polls) do lines[i] = jsonEncode(p) end
  writeFile(folder .. 'polls.jsonl', table.concat(lines, '\n') .. '\n')
  local meta = { mph = r.mph, seconds = r.seconds, speed = r.speed, km = r.km, lat = r.lat, lon = r.lon,
    heading = r.heading, install = r.install, reason = r.reason, error = r.error, started = os.date('%Y-%m-%d %H:%M:%S', r.startedAt),
    profile = r.profile }
  writeFile(folder .. 'meta.json', jsonEncode(meta))
  return (FS:getUserPath() .. folder):gsub('\\', '/')
end

local function finish(reason, err)
  local r = run
  if not r then return end
  run = nil
  r.reason, r.error = reason, err
  local D = rawget(_G, 'BNGE_DRIVE')
  if D and D.installed then
    pcall(function() r.polls[#r.polls + 1] = D.pollTable() end)
    pcall(D.uninstall)
  end
  pcall(restoreCar, r)
  local ok, where = pcall(save, r)
  if ok then
    say(string.format('Test drive saved (%s): %s', reason, where), err and 'warning' or 'success')
  else
    say('Test drive could not be saved: ' .. tostring(where), 'error')
  end
end

local function start(mph, seconds, heading, lat, lon)
  if run then say('A test drive is already running (bngeTest.stop() ends it)', 'warning') return false end
  if rawget(_G, 'MPCoreNetwork') and MPCoreNetwork.isMPSession and MPCoreNetwork.isMPSession() then
    say('Test drives are for single player only', 'error') return false
  end
  if not rawget(_G, 'beamngEarth_core') then say('BeamNG.Earth is not loaded', 'error') return false end
  local car = getPlayerVehicle(0)
  if not car then say('No car to start from', 'error') return false end
  mph, seconds = tonumber(mph) or 200, tonumber(seconds) or 60
  local s = coreStatus() or {}
  local d = car:getDirectionVector()
  heading = tonumber(heading) or (math.deg(math.atan2(d.x, d.y)) % 360)
  run = { phase = lat and 'teleport' or 'spawn', mph = mph, seconds = seconds, speed = mph * MPH,
    km = mph * MPH * seconds / 1000, heading = heading, lat = lat or (s.player or {}).lat, lon = lon or (s.player or {}).lon,
    polls = {}, clock = 0, startedAt = os.time(), carId = car:getID() }
  last = { mph = mph, seconds = seconds, heading = heading, lat = run.lat, lon = run.lon }
  if lat then
    run.anchor0 = (coreState() or {}).anchorId
    beamngEarth_core.teleport(lat, lon, 'test drive start', { record = false })
    say(string.format('Teleporting to the test start, then %d s for the world to load', TELEPORT_SETTLE_S))
  end
  return true
end

function M.run(mph, seconds, heading) return start(mph, seconds, heading) end

function M.again()
  if not last then say('No test drive yet: start one with bngeTest.run(200)', 'warning') return false end
  return start(last.mph, last.seconds, last.heading, last.lat, last.lon)
end

function M.stop() if run then finish('stopped') end return true end

function M.status() return run and run.phase or 'idle' end

local function spawnTestCar(r)
  local car = scenetree.findObjectById(r.carId) or getPlayerVehicle(0)
  if not car then return 'no player vehicle' end
  r.carId = car:getID()
  local p = car:getPosition()
  local h = math.rad(r.heading)
  local dir = vec3(math.sin(h), math.cos(h), 0)
  local t = core_vehicles.spawnNewVehicle(TEST_VEHICLE, { pos = vec3(p.x, p.y, p.z + 2),
    rot = quatFromDir(dir, vec3(0, 0, 1)), autoEnterVehicle = true })
  if not t then return 'test vehicle spawn failed' end
  r.testId = t:getID()
  car:setActive(0)
end

local function installDriver(r)
  local code = readFile(DRIVER)
  if not code then return 'driver file missing: ' .. DRIVER end
  local fn, err = loadstring(code, 'drive_driver')
  if not fn then return 'driver: ' .. tostring(err) end
  fn()
  if not rawget(_G, 'beamngEarth_perfmon') then extensions.load('beamngEarth_perfmon') end
  r.profile = { points = { { s = 0, speed = r.speed, heading = r.heading } }, km = r.km }
  local res = jsonDecode(BNGE_DRIVE.install(jsonEncode(r.profile)))
  if res.error then return 'install: ' .. res.error end
  r.install = res
end

local function onDriveFrame(r, dt)
  local D = BNGE_DRIVE
  local s = D and D.state
  if not s then return end
  local a = blockAhead(s.x, s.y, s.cmd or r.heading)
  if a then
    r.aheadMin = math.min(r.aheadMin or math.huge, a)
    r.aheadSum, r.aheadN = (r.aheadSum or 0) + a, (r.aheadN or 0) + 1
    if a < 150 then r.aheadUnder150 = (r.aheadUnder150 or 0) + 1 end
  end
  r.sincePoll = (r.sincePoll or 0) + dt
  if r.sincePoll >= POLL_S then
    r.sincePoll = 0
    local p = D.pollTable()
    p.t = r.clock - (r.driveAt or 0)
    p.ahead = { min = r.aheadMin ~= math.huge and r.aheadMin or nil, mean = r.aheadN and r.aheadSum / r.aheadN or nil,
      under150 = r.aheadUnder150 or 0, frames = r.aheadN or 0 }
    local okR, rs = pcall(function() return require('beamngEarth/roads').status() end)
    if okR then p.roads = rs end
    p.mod = p.mod and { far = p.mod.far, governor = p.mod.governor, player = p.mod.player, anchorId = p.mod.anchorId,
      followWait = p.mod.followWait, commits = p.mod.commits, cells = p.mod.cells, serverBacklog = p.mod.serverBacklog,
      lastRebaseMs = p.mod.lastRebaseMs, lastFollowMs = p.mod.lastFollowMs, followCount = p.mod.followCount,
      terrainLatency = p.mod.terrainLatency, blockSize = p.mod.blockSize } or nil
    r.polls[#r.polls + 1] = p
    r.aheadMin, r.aheadSum, r.aheadN, r.aheadUnder150 = nil, nil, nil, nil
    if s.done or s.error then finish(s.error and 'error' or 'end', s.error) end
  end
end

function M.onUpdate(dtReal)
  local r = run
  if not r then return end
  r.clock = r.clock + (dtReal or 0)
  local ok, err = pcall(function()
    if r.phase == 'teleport' then
      local a = (coreState() or {}).anchorId
      if a ~= nil and a ~= r.anchor0 then r.phase, r.waitUntil = 'settle', r.clock + TELEPORT_SETTLE_S
      elseif r.clock > ARRIVE_MAX_S then finish('error', 'the teleport did not happen') end
    elseif r.phase == 'settle' then
      if r.clock >= r.waitUntil then r.phase = 'spawn' end
    elseif r.phase == 'spawn' then
      local e = spawnTestCar(r)
      if e then finish('error', e) return end
      r.phase, r.waitUntil = 'spawned', r.clock + SPAWN_SETTLE_S
    elseif r.phase == 'spawned' then
      if r.clock >= r.waitUntil then
        local e = installDriver(r)
        if e then finish('error', e) return end
        r.phase, r.driveAt = 'drive', r.clock
        say(string.format('Driving %d mph for %d s', r.mph, r.seconds))
      end
    elseif r.phase == 'drive' then
      onDriveFrame(r, dtReal or 0)
    end
  end)
  if not ok then finish('error', tostring(err)) end
end

function M.onExtensionUnloaded() if run then finish('unloaded') end end

return M
