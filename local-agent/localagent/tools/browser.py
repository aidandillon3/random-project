"""A web browser it drives directly (Playwright with Microsoft Edge, or Chrome, in a profile of its
own in the data folder, so your own browser and its logins are left alone). Pages come back as
text with numbered links, buttons and fields, which it clicks and fills by number: far faster and
more reliable on websites than clicking on screenshots. Offered with Computer use.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time

from .base import CONTROL, ToolContext, ToolError, ToolResult, tool

PACKAGES = ["playwright"]
WINDOWS = sys.platform == "win32"


def missing() -> str | None:
    import importlib.util

    if importlib.util.find_spec("playwright") is None:
        return ("Browser control needs the playwright add-on, which isn't installed. Turn Computer use off and on "
                "again in the app to install it.")
    return None


# Numbers the things to click and fill (data-la-ref), and returns them with the page's text.
SNAPSHOT_JS = r"""
(max) => {
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],' +
    '[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[role=combobox],[role=textbox],' +
    '[onclick],[contenteditable=""],[contenteditable=true]';
  document.querySelectorAll('[data-la-ref]').forEach(e => e.removeAttribute('data-la-ref'));
  const items = []; let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none' || e.type === 'hidden') continue;
    if (e.closest('[aria-hidden=true]')) continue;
    n++; e.setAttribute('data-la-ref', n);
    const tag = e.tagName, type = (e.type || '').toLowerCase();
    let role = e.getAttribute('role') || (tag === 'A' ? 'link' : tag === 'SELECT' ? 'select' :
      tag === 'TEXTAREA' ? 'textbox' : tag === 'INPUT' ?
      (['checkbox', 'radio', 'submit', 'button', 'range', 'file', 'reset'].includes(type) ? type : 'textbox') :
      tag.toLowerCase());
    // a field's label, without the text of the field itself (a list inside its label lists its options)
    const label = e.labels && e.labels[0] ? Array.from(e.labels[0].childNodes)
      .filter(c => c.nodeType === 3 || (c !== e && !c.contains(e) && c.tagName !== 'SELECT'))
      .map(c => c.textContent).join(' ') : '';
    const field = ['INPUT', 'SELECT', 'TEXTAREA'].includes(tag);
    let name = e.getAttribute('aria-label') || (['submit', 'button', 'reset'].includes(type) ? e.value : '') ||
      (field ? label : e.innerText) || '';
    name = name || e.getAttribute('placeholder') || e.getAttribute('title') || e.getAttribute('alt') || e.name || '';
    name = name.trim().replace(/\s+/g, ' ').slice(0, 80);
    let extra = '';
    if (role === 'textbox' && e.value && type !== 'password') extra = ` = "${String(e.value).slice(0, 60)}"`;
    if (role === 'checkbox' || role === 'radio') extra = e.checked ? ' (ticked)' : ' (not ticked)';
    if (role === 'select' && e.options) extra = ` = "${(e.options[e.selectedIndex] || {}).text || ''}"`;
    const seen = r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
    items.push({n, role, name, extra, seen});
    if (n >= max) break;
  }
  const text = (document.body ? document.body.innerText : '').replace(/[ \t]+/g, ' ').replace(/\n\s*\n+/g, '\n');
  return {title: document.title, url: location.href, items, text};
}
"""


class _Browser:
    """Playwright's objects belong to the thread that made them, and the agent's tools run on
    whichever thread the chat uses: so one thread of its own does all the browser work."""

    def __init__(self):
        self.jobs: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.pw = self.context = self.page = None

    def call(self, fn, timeout: float = 90):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="browser", daemon=True)
            self.thread.start()
        box: dict = {}
        done = threading.Event()
        self.jobs.put((fn, box, done))
        if not done.wait(timeout):
            raise ToolError("The browser didn't answer in time.")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _run(self):
        while True:
            fn, box, done = self.jobs.get()
            try:
                box["value"] = fn(self)
            except ToolError as e:
                box["error"] = e
            except Exception as e:  # noqa: BLE001 - handed to the tool as a message for the model
                box["error"] = ToolError(f"Browser: {str(e).splitlines()[0] if str(e) else type(e).__name__}")
                if "closed" in str(e).lower() or "disconnected" in str(e).lower():
                    self.context = self.page = None     # the user closed it: a fresh one next time
            finally:
                done.set()

    # -- on the browser thread
    def ensure(self, settings) -> "object":
        if self.page is not None and not self.page.is_closed():
            return self.page
        if self.context is not None:
            pages = [p for p in self.context.pages if not p.is_closed()]
            if pages:
                self.page = pages[-1]
                return self.page
            try:
                self.context.close()
            except Exception:  # noqa: BLE001
                pass
            self.context = None
        if self.pw is None:
            from playwright.sync_api import sync_playwright

            self.pw = sync_playwright().start()
        from ..config import DATA_DIR

        profile = DATA_DIR / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        args = []
        try:
            from .screen import monitors

            mons = monitors()
            m = mons[int(getattr(settings, "monitor", 1) or 1) - 1] if mons else None
            if m:     # open it on the screen it works on, so screenshots see it too
                args = [f"--window-position={m['left'] + 40},{m['top'] + 40}", "--start-maximized"]
        except Exception:  # noqa: BLE001 - it opens wherever the browser likes
            pass
        opts = dict(headless=os.environ.get("LOCALAGENT_BROWSER_HEADLESS") == "1", no_viewport=True, args=args)
        exe = os.environ.get("LOCALAGENT_BROWSER_EXE")
        tries = [dict(executable_path=exe)] if exe else \
            [dict(channel="msedge"), dict(channel="chrome"), {}] if WINDOWS else [{}, dict(channel="chrome")]
        errors = []
        for extra in tries:
            try:
                self.context = self.pw.chromium.launch_persistent_context(str(profile), **opts, **extra)
                break
            except Exception as e:  # noqa: BLE001 - try the next browser
                errors.append(str(e).splitlines()[0])
        else:
            raise ToolError("No browser could be started (Edge, Chrome or Playwright's own; for that one run "
                            "'python -m playwright install chromium'). " + " | ".join(errors[:2]))
        self.context.on("page", self._new_page)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self.page

    def _new_page(self, page):
        # a link or button on the page it is using that opens a new tab: carry on there. Other tabs (ones
        # Edge restores, or opens by itself) are left alone, so the numbers it just read stay right
        try:
            opener = page.opener()
        except Exception:  # noqa: BLE001
            opener = None
        if opener is not None and opener == self.page:
            self.page = page

    def close(self):
        try:
            if self.context is not None:
                self.context.close()
            if self.pw is not None:
                self.pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self.pw = self.context = self.page = None


BROWSER = _Browser()


def _settle(page, ms: int = 2500):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=ms * 4)
        page.wait_for_load_state("networkidle", timeout=ms)
    except Exception:  # noqa: BLE001 - busy pages never go idle: read what's there
        pass


def snapshot(page, items: int = 120, chars: int = 1500) -> str:
    data = page.evaluate(SNAPSHOT_JS, items)
    lines = [f"Page: {data['title'] or '(no title)'} - {data['url']}"]
    shown = [i for i in data["items"] if i["seen"]] + [i for i in data["items"] if not i["seen"]]
    if shown:
        lines.append("Things to click or fill (browser_click / browser_type with ref=N):")
        for i in shown:
            lines.append(f"[{i['n']}] {i['role']} '{i['name']}'{i['extra']}" + ("" if i["seen"] else " (scroll)"))
    text = data["text"].strip()
    if text:
        cut = text[:chars]
        lines.append("Text on the page:" + (f" (first {chars} of {len(text)} characters; browser_read with "
                                            "more_text=true for more)" if len(text) > chars else ""))
        lines.append(cut)
    return "\n".join(lines)


def _locator(page, a: dict):
    if a.get("ref") is not None:
        loc = page.locator(f'[data-la-ref="{int(a["ref"])}"]')
        if loc.count() == 0:
            raise ToolError(f"There is no [{a['ref']}] on the page now: it changed. Use browser_read for new numbers.")
        return loc.first
    if a.get("text"):
        loc = page.get_by_text(str(a["text"]), exact=False)
        if loc.count() == 0:
            loc = page.get_by_role("button", name=str(a["text"]))
        if loc.count() == 0:
            raise ToolError(f"Nothing on the page says '{a['text']}'. Use browser_read to see what's there.")
        return loc.first
    raise ToolError("Say which one: ref=N from browser_read, or its text.")


def _need(ctx: ToolContext):
    why = missing()
    if why:
        raise ToolError(why)


@tool("browser_open", """
Open a web page in the browser it controls (its own window, separate from the user's browser) and
read it: its links, buttons and fields come back numbered. url "back", "forward" or "reload" do
those. Prefer this to clicking on screenshots for anything on a website.""",
      {"url": {"type": "string"}}, required=["url"], risk=CONTROL, group="control",
      describe=lambda a: f"Open {a.get('url')} in the browser")
def browser_open(ctx: ToolContext, a: dict):
    _need(ctx)
    url = str(a.get("url") or "").strip()
    if not url:
        raise ToolError("Give a web address.")

    def go(b: _Browser):
        page = b.ensure(ctx.settings)
        if url in ("back", "forward", "reload"):
            getattr(page, {"back": "go_back", "forward": "go_forward", "reload": "reload"}[url])(timeout=30000)
        else:
            page.goto(url if "://" in url or url.startswith("about:") else "https://" + url, timeout=30000,
                      wait_until="domcontentloaded")
        _settle(page)
        page.bring_to_front()
        return snapshot(page)

    return BROWSER.call(go)


@tool("browser_read", """
Read the page open in the browser again: its numbered links, buttons and fields, and its text.
more_text=true gives much more of the text (for reading an article or results).""",
      {"more_text": {"type": "boolean"}}, group="control")
def browser_read(ctx: ToolContext, a: dict):
    _need(ctx)
    more = bool(a.get("more_text"))
    return BROWSER.call(lambda b: snapshot(b.ensure(ctx.settings), 250 if more else 120,
                                           min(int(ctx.settings.max_tool_output) - 4000, 15000) if more else 1500))


@tool("browser_click", """
Click a link, button or box on the page by its number from the last read (ref), or by the
words on it (text). Returns the page after the click.""",
      {"ref": {"type": "integer"}, "text": {"type": "string"}}, risk=CONTROL, group="control",
      describe=lambda a: f"Click [{a.get('ref')}] in the browser" if a.get("ref") is not None
      else f"Click '{a.get('text')}' in the browser")
def browser_click(ctx: ToolContext, a: dict):
    _need(ctx)

    def go(b: _Browser):
        page = b.ensure(ctx.settings)
        _locator(page, a).click(timeout=10000)
        time.sleep(0.2)
        _settle(b.page, 2000)       # b.page: a click that opened a tab carries on there
        return snapshot(b.page)

    return BROWSER.call(go)


@tool("browser_type", """
Fill in a text box on the page (ref from the last read): what was in it is replaced. For a list
(select), text picks the option with that label. submit=true presses Enter after.""",
      {"ref": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"}},
      required=["ref", "text"], risk=CONTROL, group="control",
      describe=lambda a: f"Type into [{a.get('ref')}] in the browser: {str(a.get('text') or '')[:100]}")
def browser_type(ctx: ToolContext, a: dict):
    _need(ctx)
    text = "" if a.get("text") is None else str(a["text"])

    def go(b: _Browser):
        page = b.ensure(ctx.settings)
        loc = _locator(page, a)
        tag = (loc.evaluate("e => e.tagName") or "").upper()
        if tag == "SELECT":
            try:
                loc.select_option(label=text, timeout=5000)
            except Exception:  # noqa: BLE001 - a value instead of the label
                loc.select_option(text, timeout=5000)
        else:
            loc.fill(text, timeout=10000)
        if a.get("submit"):
            loc.press("Enter")
            time.sleep(0.2)
            _settle(b.page, 2000)
        return snapshot(b.page)

    return BROWSER.call(go)


PW_KEYS = {"ctrl": "Control", "control": "Control", "alt": "Alt", "shift": "Shift", "win": "Meta", "meta": "Meta",
           "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab", "space": "Space",
           "backspace": "Backspace", "delete": "Delete", "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft",
           "right": "ArrowRight", "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End"}


def _pw_key(p: str) -> str:
    k = p.lower()
    if k in PW_KEYS:
        return PW_KEYS[k]
    return k.upper() if k[:1] == "f" and k[1:].isdigit() else p     # f5 -> F5


@tool("browser_press", 'Press keys in the browser page, e.g. "Enter", "ctrl+a", "pagedown", "alt+left".',
      {"keys": {"type": "string"}}, required=["keys"], risk=CONTROL, group="control",
      describe=lambda a: f"Press {a.get('keys')} in the browser")
def browser_press(ctx: ToolContext, a: dict):
    _need(ctx)
    parts = [p for p in str(a.get("keys") or "").replace(" ", "+").split("+") if p]
    if not parts:
        raise ToolError("Say which keys.")
    combo = "+".join(_pw_key(p) for p in parts)

    def go(b: _Browser):
        page = b.ensure(ctx.settings)
        page.keyboard.press(combo)
        time.sleep(0.2)
        _settle(b.page, 1500)
        return snapshot(b.page)

    return BROWSER.call(go)


@tool("browser_screenshot", """
A picture of the page in the browser, to look at its layout or images. To click, use
browser_click with a number from the text, not positions in this picture.""",
      {}, group="control")
def browser_screenshot(ctx: ToolContext, a: dict):
    _need(ctx)
    import io

    from PIL import Image

    from .screen import _png, fit_size

    png = BROWSER.call(lambda b: b.ensure(ctx.settings).screenshot(type="png", timeout=15000))
    img = Image.open(io.BytesIO(png)).convert("RGB")
    size = fit_size(ctx, *img.size)
    if size != img.size:
        img = img.resize(size, Image.LANCZOS)
    return ToolResult("The browser page is attached.", images=[_png(img)])
