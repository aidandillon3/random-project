-- BeamNG.Earth test drive: the same drive every time, recorded for comparing changes. Single player only.
--
-- Console (~), GE Lua:
--   bngeTest.run(200)          drive straight ahead at 200 mph for 60 s from where you are, the way the car faces
--   bngeTest.run(200, 90)      ... for 90 s
--   bngeTest.again()           teleport to the last run's start and drive it again (same heading, speed, time)
--   bngeTest.stop()            end the run now (it is still saved)
--   bngeTest.autoBudget()      experiment: raise BeamNG.Earth's streaming budget (2 ms per frame) up to 4 ms while
--                              road meshes or far tiles queue up, back to 2 when they have caught up;
--                              bngeTest.autoBudget(false) turns it off (budget 2 again). Works outside test drives too.
--   bngeTest.budget(4)         a fixed streaming budget instead (1 to 4 ms; turns autoBudget off)
--
-- A stock test car (vivace) is spawned at your car, which is frozen meanwhile and given back at the end (BeamNG.Earth's
-- tools/drive.py does the same: per-frame placement made a lifted car unstable). The car is moved along the line by
-- BeamNG.Earth's own test driver (beamng_earth_test/drive_driver.lua, from TecnaGamer/BeamNG.Earth tools/, MIT), so
-- the speed and the path never depend on driving; it hovers CLEARANCE_M above the highest ground or building on the
-- next 0.6 s of its path, so it rises over buildings before it gets to them. Every second this records the driver's frames and the mod's state
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
-- m the test car is held above the ground (the driver rays down onto the terrain and buildings): on the ground the car
-- was ground up at 200 mph and hit buildings (in-game 2026-10-21). Far below the mod's altitude hold (300 m).
local CLEARANCE_M = 11  -- (6 clipped trees and traffic lights)

local LAST = OUT .. 'last.json'  -- the last run's start, kept across game restarts (again() after switching versions)
-- autoBudget: the governor's target (beamngEarth_core.setFarBudget, 1-4 ms) from the streaming backlog, every EVERY_S:
-- LO ms at QLO queued units or fewer, HI at QHI or more, linear between (0.25 ms steps). A test drive at 200 mph
-- (2026-10-21): with 2 ms the road mesh queue (decks, sidewalks, building collision) grew to 391 in a minute; with 4 ms
-- it stayed under 40, at 48 instead of 55 fps.
local AUTO = { on = false, lo = 2, hi = 4, qLo = 20, qHi = 80, every = 0.5, wait = 0, ms = nil }
local run = nil     -- the run in progress
local last = nil    -- { mph, seconds, lat, lon, heading } of the last run started

local function loadLast()
  local ok, t = pcall(function() local s = readFile(LAST) return s and jsonDecode(s) end)
  return ok and type(t) == 'table' and t.lat and t or nil
end

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
    heading = r.heading, install = r.install, autoBudget = AUTO.on, budget = AUTO.on and 'auto' or AUTO.fixed or 2, reason = r.reason, error = r.error, started = os.date('%Y-%m-%d %H:%M:%S', r.startedAt),
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
  pcall(writeFile, LAST, jsonEncode(last))
  if lat then
    run.anchor0 = (coreState() or {}).anchorId
    beamngEarth_core.teleport(lat, lon, 'test drive start', { record = false })
    say(string.format('Teleporting to the test start, then %d s for the world to load', TELEPORT_SETTLE_S))
  end
  return true
end

function M.run(mph, seconds, heading) return start(mph, seconds, heading) end

function M.again()
  last = last or loadLast()
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
  r.profile = { points = { { s = 0, speed = r.speed, heading = r.heading } }, km = r.km, clearance = CLEARANCE_M, lookAhead = true }
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

local function backlog()
  local okR, rs = pcall(function() return require('beamngEarth/roads').status() end)
  local okF, fs = pcall(function() return require('beamngEarth/farfield').stats() end)
  return ((okR and rs and rs.walksQueued) or 0) + ((okF and fs and fs.queued) or 0)
end

local function updateBudget(dt)
  AUTO.wait = AUTO.wait - dt
  if AUTO.wait > 0 then return end
  AUTO.wait = AUTO.every
  local core = rawget(_G, 'beamngEarth_core')
  if not (core and core.setFarBudget) then return end
  local f = math.max(0, math.min(1, (backlog() - AUTO.qLo) / (AUTO.qHi - AUTO.qLo)))
  local ms = math.floor((AUTO.lo + (AUTO.hi - AUTO.lo) * f) * 4 + 0.5) / 4
  if ms ~= AUTO.ms then AUTO.ms = core.setFarBudget(ms) or ms end
end

function M.autoBudget(on, hi)
  AUTO.on = on ~= false
  AUTO.fixed = nil
  AUTO.hi = math.max(AUTO.lo, math.min(4, tonumber(hi) or 4))
  AUTO.ms, AUTO.wait = nil, 0
  local core = rawget(_G, 'beamngEarth_core')
  if not AUTO.on and core and core.setFarBudget then core.setFarBudget(AUTO.lo) end
  say(AUTO.on and string.format('Auto budget on: %g to %g ms per frame', AUTO.lo, AUTO.hi) or 'Auto budget off: 2 ms per frame')
  return AUTO.on
end

function M.budget(ms)
  ms = math.max(1, math.min(4, tonumber(ms) or 2))
  AUTO.on, AUTO.ms = false, nil
  local core = rawget(_G, 'beamngEarth_core')
  if not (core and core.setFarBudget) then say('BeamNG.Earth is not running') return nil end
  AUTO.fixed = core.setFarBudget(ms) or ms
  say(string.format('Budget: %g ms per frame (auto off)', AUTO.fixed))
  return AUTO.fixed
end

function M.onUpdate(dtReal)
  if AUTO.on then pcall(updateBudget, dtReal or 0) end
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
