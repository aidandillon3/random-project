"""Terminal version: python -m localagent --cli"""

from __future__ import annotations

import sys
import threading

from .agent import Agent, AgentUI
from .config import Settings
from .llm import LOADING, STARTING, ServerError
from .permissions import Decision, risk_text

DIM, RESET, CYAN, YELLOW, RED = "\033[2m", "\033[0m", "\033[36m", "\033[33m", "\033[31m"


class TerminalUI(AgentUI):
    def __init__(self):
        self._thinking = False
        self._status = ""

    def text(self, s):
        if self._thinking:
            print(RESET + "\n", end="")
            self._thinking = False
        print(s, end="", flush=True)

    def reasoning(self, s):
        if not self._thinking:
            print(DIM + "[thinking] ", end="")
            self._thinking = True
        print(s, end="", flush=True)

    def assistant_end(self, turn):
        if self._thinking:
            print(RESET, end="")
            self._thinking = False
        print()

    def tool_start(self, call_id, name, args, summary):
        print(f"{CYAN}> {name}: {summary[:200]}{RESET}")

    def tool_end(self, call_id, name, result):
        first = result.text.strip().splitlines()[:6]
        color = DIM if result.ok else YELLOW
        print(color + "\n".join("  " + line[:200] for line in first) + RESET)
        if result.images:
            print(f"{DIM}  [{len(result.images)} image(s) sent to the model]{RESET}")

    def notice(self, s):
        print(f"{YELLOW}{s}{RESET}")

    def status(self, s):
        if s in (LOADING, STARTING) and s != self._status:   # the rest ("Thinking...") is just clutter
            print(f"{DIM}{s}{RESET}")
        self._status = s

    def todos(self, todos):
        marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        for t in todos:
            print(f"{DIM}  {marks.get(t['status'], '[ ]')} {t['content']}{RESET}")

    def approve(self, req):
        color = RED if req.dangerous else YELLOW
        print(f"{color}Allow it to {risk_text(req.risk)}?{RESET}\n  {req.summary}")
        _drop_typed_ahead()     # an Enter pressed earlier must not answer this
        if req.dangerous:       # never with a single key: it can delete data or change the system
            ans = input(f"  {RED}Type yes to allow it{RESET}, no to deny (or type what to do instead): ").strip()
            if ans.lower() == "yes":
                return Decision(True)
        else:
            ans = input("  [y]es / [a]lways this chat / [n]o (or type what to do instead): ").strip()
            if ans.lower() in ("y", "yes"):
                return Decision(True)
            if ans.lower() in ("a", "always"):
                return Decision(True, always=True)
        return Decision(False, note="" if ans.lower() in ("", "n", "no") else ans)

    def ask(self, question, options):
        print(f"{YELLOW}{question}{RESET}")
        for i, o in enumerate(options, 1):
            print(f"  {i}. {o}")
        ans = input("> ").strip()
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        return ans


def _drop_typed_ahead():
    """Throw away keys typed while the agent was working (Windows console only)."""
    try:
        import msvcrt
    except ImportError:
        return
    while msvcrt.kbhit():
        msvcrt.getwch()


def _run(agent: Agent, text: str):
    """Run one request on a worker thread: Ctrl+C then only sets Stop, which also cuts a request the
    server is still busy with (on Windows, Ctrl+C can't interrupt the main thread while it waits)."""
    errors = []

    def work():
        try:
            agent.run(text)
        except Exception as e:  # noqa: BLE001 - shown, and the next message can carry on
            errors.append(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    while t.is_alive():
        try:
            t.join(0.2)        # short waits: on Windows Ctrl+C is only seen between them
        except KeyboardInterrupt:
            if not agent.stop.is_set():
                agent.stop.set()
                print(f"\n{YELLOW}Stopping...{RESET}")
    for e in errors:
        print(f"{RED}{e if isinstance(e, ServerError) else f'{type(e).__name__}: {e}'}{RESET}")


def main(argv: list[str]) -> int:
    settings = Settings.load()
    if "--url" in argv:
        settings.base_url = argv[argv.index("--url") + 1]
    agent = Agent(settings, TerminalUI())
    if settings.index_files:
        from .memory import start_indexer

        start_indexer(settings)
    print(f"Local Agent (terminal). Server: {settings.base_url}. Work folder: {agent.workspace}")
    print("Type a message. /new starts over, /quit exits. Ctrl+C stops the current task.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text in ("/quit", "/exit"):
            return 0
        if text == "/new":
            agent.reset()
            continue
        _run(agent, text)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
