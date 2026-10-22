-- Our HUD as BeamNG UI apps (the user, 2026-10-15: "convert our current in game HUD elements to beam's ui app system and
-- create a .Earth preset based off the freeroam preset"). The apps (ui/modules/apps/BeamNGEarth*) are Angular
-- directives in the game's app layout: the player moves, resizes and removes them like any app, and the ".Earth"
-- layout (settings/ui_apps/originalLayouts/default/beamngEarth.uilayout.json) places them.
--
-- An app says when it is on screen (beamngEarth_core.hudApp(name, true / false) from its link / $destroy); while one
-- is, core leaves out the imgui drawing of the same element (statusbar.lua, credits.lua, banner.lua), so nothing shows
-- twice and the imgui HUD still works without the apps (another layout). Every SEND_S core hands update() the texts
-- and they go to the apps in one 'BeamNGEarthHud' hook, only while an app is mounted and only when something changed.
-- Pure apart from guihooks (runs under plain luajit with a stub).
local M = {}

M.NAMES = { status = true, credits = true, banner = true }
M.SEND_S = 0.25           -- s between updates to the apps (the status bar's fps changes every frame)
M.STALE_S = 5.0           -- an unchanged payload is sent again this often (an app opened since)

local mounted = {}        -- name -> true while that app is on screen
local nMounted = 0
local lastSend, lastKey, lastFull = -math.huge, nil, -math.huge
M.sends = 0

function M.setMounted(name, on)
  if not M.NAMES[name] then return false end
  on = on and true or false
  if (mounted[name] or false) ~= on then
    mounted[name] = on or nil
    nMounted = nMounted + (on and 1 or -1)
    lastKey = nil           -- a newly opened app gets the current state at the next update
  end
  return on
end

function M.mounted(name) return mounted[name] == true end
function M.any() return nMounted > 0 end

-- Pure: the payload for the apps. t: { status, credits, banner = { text, level, visible }, hidden }
function M.payload(t)
  local b = t.banner or {}
  return {
    status = t.status or '',
    credits = t.credits or '',
    banner = (b.visible and b.text ~= '' and b.text) or '',
    bannerLevel = b.level or 'info',
    hidden = t.hidden and true or false,
  }
end

local function key(p)
  return p.status .. '\1' .. p.credits .. '\1' .. p.banner .. '\1' .. p.bannerLevel .. '\1' .. tostring(p.hidden)
end

-- now: core's clock (s); get(): the texts (only called when a send is due, so its cost is paid at SEND_S at most).
-- Returns true if it sent.
function M.update(now, get)
  if nMounted == 0 or now - lastSend < M.SEND_S then return false end
  lastSend = now
  local p = M.payload(get())
  local k = key(p)
  if k == lastKey and now - lastFull < M.STALE_S then return false end
  lastKey, lastFull = k, now
  local gh = rawget(_G, 'guihooks')
  if gh and gh.trigger then gh.trigger('BeamNGEarthHud', p) end
  M.sends = M.sends + 1
  return true
end

function M._reset()
  mounted, nMounted, lastSend, lastKey, lastFull, M.sends = {}, 0, -math.huge, nil, -math.huge, 0
end

return M
