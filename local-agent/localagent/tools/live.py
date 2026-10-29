"""Watch mode: the model looks at the screen about once a second and answers with a few short action
lines ("click 412 300", "key w hold 1"), which run at once, then it gets the next screenshot. No tool
call per action and no thinking, so a step costs about one picture read plus a few words; the normal
way (a tool call, a screenshot after it, thinking between) took about 3 s a step in the computer use
test. For games and anything that moves, or many quick steps on one screen.

It runs as a side conversation of its own, sent fresh each look: a short prompt, the goal, the last
few looks' action lines and results as text, and only the newest screenshot(s). So the server reads
about the same small amount every look (keeping the whole conversation, with older pictures turned
into a word, made it re-read more text each look: 1.0 s of reading at the first look, 1.45 s by the
third, in the computer use test), and the context doesn't fill. The chat gets back what was done and
the last screenshot.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque

from . import screen
from .base import CONTROL, ToolContext, ToolError, ToolResult, tool

MAX_ACTIONS = 6          # action lines run from one reply
REPLY_TOKENS = 160       # a reply is a few short lines
NO_ACTIONS_LIMIT = 3     # replies in a row without an action line before it gives up
CHECK_LOOKS = 10         # looks before the chat checks how it's going (it drifted into random moves after ~10)
STUCK_LOOKS = 4          # looks in a row with the same reply, or with the screen unchanged, before the chat rethinks
FRAME_GAP = 0.5          # seconds between the frames of one look when it is shown more than one
HISTORY = 4              # earlier looks it is told about, as text
# Strata reads a prompt in 1024-token pieces, and with this model a short prompt's start can't be kept from the last
# request (it keeps the conversation only at checkpoints, far apart), so every look is read whole: 963 tokens took
# 1.0 s, 1040 took 1.44 s in the computer use test. Fewer earlier looks are told about once a look goes over this.
PROMPT_TOKENS = 1000
SHORT_WAIT = 0.5         # a wait this short at the end of a reply is left out: the next look takes longer anyway
SIZES = {"small": 256, "normal": screen.AUTO_SQUARES, "sharp": screen.VISION_SQUARES}   # 32x32 squares a frame

PROMPT = """You are using the user's computer in watch mode. You are shown the newest screenshot about once a \
second; answer with a few action lines, which run at once, and then you see the next screenshot. Write only \
action lines: no explanations, no thinking out loud.

Actions, one per line:
click X Y            left click; add "right", "double", or "hold 0.5" (seconds held down)
click                click where the mouse already is (first-person games: where the crosshair is); "click hold 3"
move X Y             move the mouse pointer there
turn DX DY           move the mouse by an amount: turns the camera in games (try 100-300); DX > 0 turns right,
                     DX < 0 left, DY > 0 looks down, DY < 0 looks up
key KEYS             press a key or shortcut: "key space", "key ctrl+s"; "key w hold 1.5" holds it down
type TEXT            type text into what has focus
scroll N             scroll down N notches (negative: up)
wait S               do nothing for S seconds (only when something needs time: the next screenshot comes anyway)
done WHAT            the goal is reached: say what on the screen shows it
stop WHY             you're stuck or don't know what to do next: say why, and the chat thinks it over

{coords} Each look says where the mouse pointer is (it isn't drawn). At most {most} actions per reply; when unsure, give one and look. Things keep moving while you answer, so aim at \
where they are in the newest screenshot. {games}Say done only when the newest screenshot shows the goal \
reached."""

GAMES = ("In games your own character is the one the camera follows, usually near the middle; hold movement "
         "keys a short while (0.3-1 s) and look again. In first-person games aim with turn and use click without a "
         "position: a position moves the mouse, which turns the camera. Breaking or mining something takes the button "
         "held until it breaks, often several seconds, without moving or jumping meanwhile (that starts it over): if "
         "it didn't break, hold longer. In water, hold space (\"key space hold 2\") to swim up. Esc opens a game's "
         "pause menu: press it only to close a menu you see open. When the same actions didn't change anything, do "
         "something else. ")

ALIASES = {"press": "key", "pressed": "key", "keys": "key", "hold": "key", "mouse": "move", "look": "turn",
           "write": "type", "sleep": "wait", "finish": "done", "finished": "done", "fail": "stop", "quit": "stop",
           # what the results of earlier looks say, when a reply copies them
           "clicked": "click", "moved": "move", "turned": "turn", "typed": "type", "scrolled": "scroll",
           "waited": "wait"}
_NUM = r"-?\d+(?:\.\d+)?"


def parse(text: str) -> list[tuple[str, str]]:
    """(action, rest of the line) for each action line of a reply; other lines are left out. Forgives
    bullets, numbering, code fences and "Action:" in front."""
    out = []
    # several actions on one line, " / " or "; " between them (as the earlier looks are listed)
    pieces = [piece for raw in text.splitlines() for piece in re.split(r"\s+/\s+|;\s+", raw)]
    for raw in pieces:
        line = raw.strip().strip("`").strip()
        line = re.sub(r"^(?:[-*>]+|\d+[.)])\s*", "", line)
        line = re.sub(r"^action\s*:\s*", "", line, flags=re.I)
        if not line:
            continue
        word, _, rest = line.partition(" ")
        word = word.lower().rstrip(":")
        word = ALIASES.get(word, word)
        if word in ("click", "move", "turn", "key", "type", "scroll", "wait", "done", "stop"):
            out.append((word, rest.strip()))
    return out


def _numbers(rest: str, n: int, what: str) -> list[float]:
    found = re.findall(_NUM, rest)
    if len(found) < n:
        raise ToolError(f"{what} needs {n} number(s)")
    return [float(v) for v in found[:n]]


_HOLD = r"\(?\b(?:hold|held)\s+(" + _NUM + r")\s*s?\)?"


def _hold(rest: str) -> float:
    m = re.search(_HOLD, rest)
    return float(m.group(1)) if m else 0.0


def _captured() -> bool:
    from . import winput

    return screen.WINDOWS and winput.pointer_hidden()


def _let_go(note) -> str:
    m = re.search(r"let go after (" + _NUM + r") s", str(note or ""))
    return f" but it was let go after {m.group(1)}s" if m else ""


def _changed(before: bytes, after: bytes) -> float:
    """How much the screen changed between two looks: the mean brightness difference of small grey copies,
    0-255 (a still screen is ~0-1; moving the camera is tens)."""
    import io

    from PIL import Image, ImageChops, ImageStat

    try:
        a, b = (Image.open(io.BytesIO(x)).convert("L").resize((96, 54)) for x in (before, after))
        return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]
    except Exception:  # noqa: BLE001 - no note then
        return 99.0


STILL = 1.5         # under this the screen counts as unchanged


def run_action(ctx: ToolContext, action: str, rest: str) -> str:
    """Do one action line with the screen tools (without their screenshot after); what happened, short.
    Raises ToolError for a line it can't do."""
    if action == "click":
        low = rest.lower()
        button = "right" if "right" in low else "middle" if "middle" in low else "left"
        clicks, hold = 2 if "double" in low else 1, _hold(low)
        held = f" (held {hold:g}s)" if hold else ""
        if not re.findall(_NUM, re.sub(_HOLD, "", low)) or _captured():
            # no position, or a first-person game holds the mouse (moving it would turn the camera): click in place
            note = screen._click_here(ctx, screen._gui(), button, clicks, hold)
            return f"clicked here{' ' + button if button != 'left' else ''}{held}{_let_go(note)}"
        x, y = _numbers(re.sub(_HOLD, "", low), 2, "click")
        r = screen.click(ctx, {"x": x, "y": y, "button": button, "clicks": clicks, "hold_seconds": hold})
        return f"clicked {x:g} {y:g}{held}{_let_go(getattr(r, 'text', r))}"
    if action == "move":
        x, y = _numbers(rest, 2, "move")
        screen.move_mouse(ctx, {"x": x, "y": y})
        return f"moved to {x:g} {y:g}"
    if action == "turn":
        dx, dy = _numbers(rest, 2, "turn")
        screen.move_mouse_by(ctx, {"dx": int(dx), "dy": int(dy)})
        return f"turned {dx:g} {dy:g}"
    if action == "key":
        hold = _hold(rest.lower())
        keys = re.sub(_HOLD, "", rest, flags=re.I).strip().strip("\"'")
        if not keys:
            raise ToolError("key needs a key name")
        screen.press_keys(ctx, {"keys": keys, "hold_seconds": hold})
        return f"pressed {keys}" + (f" (held {hold:g}s)" if hold else "")
    if action == "type":
        text = rest.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1]
        if not text:
            raise ToolError("type needs the text")
        screen.type_text(ctx, {"text": text})
        return f"typed {len(text)} characters"
    if action == "scroll":
        (n,) = _numbers(rest, 1, "scroll")
        screen.scroll(ctx, {"amount": int(n)})
        return f"scrolled {int(n)}"
    if action == "wait":
        (s,) = _numbers(rest, 1, "wait")
        ctx.stop.wait(max(0.0, min(5.0, s)))
        return f"waited {min(5.0, s):g}s"
    raise ToolError(f"unknown action {action}")


class _Frames:
    """Screenshots taken in the background every FRAME_GAP / 2 seconds, for showing a look's earlier
    frames (what moved). Only runs when more than one frame is shown per look."""

    def __init__(self, monitor: int):
        self.monitor, self.frames = monitor, deque(maxlen=12)
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="watch-frames")
        self.thread.start()

    def _run(self):
        while not self.done.is_set():
            try:
                img, _, _ = screen._grab(self.monitor)
                self.frames.append((time.time(), img))
            except Exception:  # noqa: BLE001 - the look then shows only its newest frame
                pass
            self.done.wait(FRAME_GAP / 2)

    def before(self, ago: float):
        """The frame taken closest to `ago` seconds back (None if there is none near it)."""
        want = time.time() - ago
        best = min(list(self.frames), key=lambda f: abs(f[0] - want), default=None)
        return best[1] if best and abs(best[0] - want) < FRAME_GAP else None

    def stop(self):
        self.done.set()
        self.thread.join(1)


def _frame(ctx: ToolContext, squares: int) -> bytes:
    return screen._shot(ctx, ctx.screen.get("monitor"), squares=squares, ring=False)


def _older(ctx: ToolContext, frames: _Frames | None, n: int) -> list[bytes]:
    """The look's earlier frames, oldest first, at the newest frame's size."""
    from PIL import Image

    out = []
    for k in range(n - 1, 0, -1):
        img = frames.before(k * FRAME_GAP) if frames else None
        if img is not None:
            out.append(screen._png(img.resize((ctx.screen["img_w"], ctx.screen["img_h"]), Image.LANCZOS)))
    return out


def _image_parts(pngs: list[bytes]) -> list[dict]:
    from ..agent import png_part

    return [png_part(p) for p in pngs]


def _messages(system: str, goal: str, history: list[str], pngs: list[bytes], text: str,
              keep: int = HISTORY) -> list[dict]:
    """One look: the prompt, the goal, the last few looks as text, the screenshot(s), then how things stand.
    Everything after the goal changes every look, so it is kept short."""
    head = f"Goal: {goal}"
    if history and keep > 0:
        head += "\nWhat your last looks did, oldest first:\n" + "\n".join(history[-keep:])
    return [{"role": "system", "content": system},
            {"role": "user", "content": [{"type": "text", "text": head}] + _image_parts(pngs)
             + [{"type": "text", "text": text}]}]


def _history_line(reply: str, did: list[str]) -> str:
    if did:
        return "- " + "; ".join(did)[:160]
    said = " / ".join(ln.strip() for ln in reply.splitlines() if ln.strip())
    return f"- you wrote: {said[:120] or '(nothing)'} (nothing done)"


def _look_text(ctx: ToolContext, left: float, older: int) -> str:
    frames = f" Frames {FRAME_GAP * older:g} s apart, the last is now." if older else ""
    where = (" The game has the mouse (no pointer): the crosshair in the middle is where you aim; turn to aim "
             "(DY > 0 looks down), and clicks happen at the crosshair." if _captured() else screen._pointer_note(ctx))
    return f"{left:.0f} s left. Screenshot {ctx.screen['img_w']}x{ctx.screen['img_h']}.{where}{frames}"


def _short_wait(action: str, rest: str) -> bool:
    if action != "wait":
        return False
    found = re.findall(_NUM, rest)
    return bool(found) and float(found[0]) <= SHORT_WAIT


def watch(ctx: ToolContext, goal: str, seconds: float) -> ToolResult:
    agent = ctx.agent
    if agent is None:
        raise ToolError("Watch mode needs the chat's model.")
    s = ctx.settings
    squares = SIZES.get(str(getattr(s, "watch_size", "normal")), screen.AUTO_SQUARES)
    per_look = max(1, min(3, int(getattr(s, "watch_frames", 1) or 1)))
    monitor = int(ctx.screen.get("monitor") or s.monitor or 1)
    chat_coords = screen.coord_mode(ctx)
    client, params, own, note = agent.client, None, False, ""
    if getattr(s, "watch_model", "chat") == "eyes":
        from .. import eyes

        try:
            client = eyes.EYES.client(s, agent.ui.status, ctx.stop)
            params, own = dict(eyes.PARAMS, max_tokens=REPLY_TOKENS), True
            note = f" It looked with its own model ({eyes.PRESET.title.split(' (')[0]}" + \
                (f" on the {eyes.EYES.gpu_name})." if eyes.EYES.gpu_name else ").")
        except RuntimeError as e:
            note = f" Its own model couldn't be used, so the chat's model looked: {e}"
    if params is None:
        params = agent._params(REPLY_TOKENS, thinking="off")
        params.pop("stop", None)
        params["max_tokens"] = REPLY_TOKENS      # _params fits it to the chat's context, which this isn't
    if own:
        ctx.screen["coords_for_now"] = "normalized"     # Qwen's vision models point on a 0-1000 grid
    system = PROMPT.format(coords=screen.coord_help(ctx), most=MAX_ACTIONS, games=GAMES)
    frames = _Frames(monitor) if per_look > 1 else None
    start = time.time()
    log: list[dict] = []
    did_all: list[str] = []
    end, ended_by, empty, last_png = "", "time", 0, b""
    still, same, last_reply = 0, 0, None
    capture_after, ctx.capture_after = ctx.capture_after, False     # its own looks instead of after-action shots
    try:
        if frames:
            time.sleep(FRAME_GAP * (per_look - 1))      # earlier frames to show with the first look
        chat_size = (ctx.screen.get("img_w"), ctx.screen.get("img_h"))
        last_png = _frame(ctx, squares)
        older = _older(ctx, frames, per_look)
        history: list[str] = []
        keep = HISTORY
        has_positions = bool(re.search(r"\d+\s*[, ]\s*\d+\)|\b[xy]\s*[=≈~]\s*\d", goal))
        if chat_size[0] and has_positions and chat_coords == "pixels" and screen.coord_mode(ctx) == "normalized":
            goal += (f"\n(Positions in this goal are pixels of a {chat_size[0]}x{chat_size[1]} screenshot, not "
                     "your 0-1000 grid: aim at what you see.)")
        elif (chat_size[0] and has_positions and chat_size != (ctx.screen["img_w"], ctx.screen["img_h"])
                and screen.coord_mode(ctx) == "pixels"):
            # the chat's goal may hold positions read off its own screenshot, which had another size
            f = ctx.screen["img_w"] / chat_size[0]
            goal += (f"\n(Positions in this goal are of a {chat_size[0]}x{chat_size[1]} screenshot; yours is "
                     f"{ctx.screen['img_w']}x{ctx.screen['img_h']}, so multiply them by {f:.2f}, or better, aim at "
                     f"what you see.)")
        messages = _messages(system, goal, history, older + [last_png], _look_text(ctx, seconds, len(older)))
        while True:
            left = seconds - (time.time() - start)
            if left <= 0:
                break
            if ctx.stop.is_set():
                ended_by = "stopped"
                break
            if not getattr(s, "computer_use", False):
                ended_by = "computer use turned off"
                break
            t0 = time.time()
            ctx.agent.ui.status(f"Watch mode: looking ({left:.0f} s left)")
            turn = client.stream(messages, None, params, stop=ctx.stop)
            model_s = time.time() - t0
            if turn.stopped:
                ended_by = "stopped"
                break
            reply = (turn.content or "").strip()
            actions = parse(reply)[:MAX_ACTIONS]
            did, t1 = [], time.time()
            for i, (action, rest) in enumerate(actions):
                if i == len(actions) - 1 and _short_wait(action, rest):
                    break
                if action in ("done", "stop"):
                    end, ended_by = rest or action, action
                    break
                if ctx.stop.is_set():
                    break
                try:
                    did.append(run_action(ctx, action, rest))
                    if action == "key" and re.sub(_HOLD, "", rest.lower()).strip(" \"'") in ("esc", "escape") \
                            and i < len(actions) - 1:
                        # Esc opens or closes menus (a game's pause menu): what follows needs a new look
                        did.append("the rest of that reply was left out: look after Esc")
                        break
                except ToolError as e:
                    did.append(f"'{action} {rest}' failed: {e}")
                except Exception as e:  # noqa: BLE001
                    if type(e).__name__ == "FailSafeException":
                        ctx.stop.set()
                        ended_by = "emergency stop (mouse in a screen corner)"
                        break
                    did.append(f"'{action} {rest}' failed: {type(e).__name__}: {e}")
            act_s = time.time() - t1
            did_all += did
            usage = turn.usage or {}
            if (usage.get("prompt_n") or 0) > PROMPT_TOKENS and keep > 0:
                keep -= 1
            log.append(dict(t=round(t0 - start, 2), model_s=round(model_s, 2), act_s=round(act_s, 2),
                            prompt_ms=usage.get("prompt_ms"), prompt_n=usage.get("prompt_n"),
                            cache_n=usage.get("cache_n"), reply=reply[:300], did=did))
            if did:
                ctx.agent.ui.status("Watch mode: " + "; ".join(did)[:150])
            if ended_by in ("done", "stop") or ended_by.startswith("emergency"):
                break
            if ctx.stop.is_set():
                ended_by = "stopped"
                break
            empty = 0 if actions else empty + 1
            if empty >= NO_ACTIONS_LIMIT:
                ended_by, end = "no actions", reply[:200]
                break
            if did:
                screen._settle(monitor, least=0.05, most=0.3)
            before = last_png
            last_png = _frame(ctx, squares)
            unchanged = bool(did) and _changed(before, last_png) < STILL
            if unchanged:
                did.append("the screen looks the same as before it")
            still = still + 1 if unchanged else 0
            said = " ".join(reply.lower().split())
            same = same + 1 if said == last_reply else 1
            last_reply = said
            if len(log) >= CHECK_LOOKS:
                ended_by, end = "check", f"{len(log)} looks"
                break
            if still >= STUCK_LOOKS or same >= STUCK_LOOKS:
                # going round in circles: back to the chat, which can think about it and start again
                ended_by = "stuck"
                end = (f"the screen didn't change for {still} looks in a row" if still >= STUCK_LOOKS else
                       f"it gave the same reply {same} times in a row ({reply[:80]!r})")
                break
            history.append(_history_line(reply, did))
            older = _older(ctx, frames, per_look)
            text = _look_text(ctx, seconds - (time.time() - start), len(older))
            if not actions:
                text = "No action lines in your last reply: answer only with action lines. " + text
            messages = _messages(system, goal, history, older + [last_png], text, keep)
        if chat_size[0] and chat_size != (ctx.screen["img_w"], ctx.screen["img_h"]):
            last_png = screen._shot(ctx, monitor)     # back at the chat's own size, so its positions stay its scale
    finally:
        ctx.capture_after = capture_after
        ctx.screen.pop("coords_for_now", None)
        if frames:
            frames.stop()
        ctx.agent.ui.status("")
        _save_log(ctx, goal, log, ended_by, client.model)
    took = time.time() - start
    steps = len(log)
    avg = (f" {steps} looks in {took:.0f} s ({took / steps:.1f} s each: model "
           f"{sum(x['model_s'] for x in log) / steps:.1f} s, actions {sum(x['act_s'] for x in log) / steps:.1f} s)."
           if steps else "")
    how = {"done": f"It says the goal is reached: {end}", "stop": f"It stopped: {end}",
           "time": f"Time ran out ({seconds:g} s).", "no actions": f"It gave no actions three times: {end}",
           "check": f"It handed back after {end} so you can check how it's going: look at the screenshot, then "
                    "start watch mode again with the next step as its goal, saying what to do differently if it "
                    "wasn't working.",
           "stuck": f"It looked stuck, so it handed back to you: {end}. Look at the screenshot, work out what went "
                    "wrong, and start watch mode again with a goal that says what to do differently."}
    text = f"Watch mode ended. {how.get(ended_by, ended_by.capitalize() + '.')}{avg}{note}"
    if did_all:
        text += "\nWhat it did (latest last): " + "; ".join(did_all[-40:])
    if last_png:
        text += f"\nA screenshot of how it ended is attached. {screen._geometry_note(ctx)}"
    return ToolResult(text, ok=ended_by in ("done", "time", "check"), images=[last_png] if last_png else [])


def _save_log(ctx: ToolContext, goal: str, log: list[dict], ended_by: str, model: str):
    """data/watch/latest.json: every look's timing, reply and result, for tuning."""
    try:
        from ..config import DATA_DIR

        d = DATA_DIR / "watch"
        d.mkdir(parents=True, exist_ok=True)
        (d / "latest.json").write_text(json.dumps(dict(goal=goal, ended_by=ended_by, model=model,
                                                       size=getattr(ctx.settings, "watch_size", "normal"),
                                                       frames=getattr(ctx.settings, "watch_frames", 1), looks=log),
                                                  indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001 - a missing log never breaks the run
        pass


@tool("watch_mode", """
Hand the screen to watch mode for a goal: it looks at the newest screenshot about once a second and acts
right away with short commands (click, keys, mouse moves) until the goal is reached or the time is up,
much faster than one tool call per action. Use it for games and anything that moves or needs many quick
steps on one screen; a single click or held click on something that stays put is quicker with click.
Give a clear goal it can see on screen, by what things look like rather than positions ("chop down the
tree to the right of the character", "get to the top of the hill"). It returns what it did and a
screenshot of how it ended.""",
      {"goal": {"type": "string", "description": "What to do, and what the screen shows when it's done"},
       "seconds": {"type": "number", "description": "Most time it may take (default 60)"}},
      required=["goal"], risk=CONTROL, group="control",
      describe=lambda a: f"Watch mode: {str(a.get('goal') or '')[:150]}")
def watch_mode(ctx: ToolContext, a: dict):
    goal = str(a.get("goal") or "").strip()
    if not goal:
        raise ToolError("Say what watch mode should do.")
    most = float(getattr(ctx.settings, "watch_max_seconds", 300) or 300)
    try:
        seconds = float(a.get("seconds") or 60)
    except (TypeError, ValueError):
        seconds = 60.0
    return watch(ctx, goal, max(5.0, min(most, seconds)))
