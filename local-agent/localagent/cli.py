"""Terminal version: python -m localagent --cli"""

from __future__ import annotations

import sys

from .agent import Agent, AgentUI
from .config import Settings
from .llm import ServerError
from .permissions import Decision, risk_text

DIM, RESET, CYAN, YELLOW, RED = "\033[2m", "\033[0m", "\033[36m", "\033[33m", "\033[31m"


class TerminalUI(AgentUI):
    def __init__(self):
        self._thinking = False

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

    def todos(self, todos):
        marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
        for t in todos:
            print(f"{DIM}  {marks.get(t['status'], '[ ]')} {t['content']}{RESET}")

    def approve(self, req):
        color = RED if req.dangerous else YELLOW
        print(f"{color}Allow it to {risk_text(req.risk)}?{RESET}\n  {req.summary}")
        ans = input("  [y]es / [a]lways this chat / [n]o (or type what to do instead): ").strip()
        if ans.lower() in ("y", "yes", ""):
            return Decision(True)
        if ans.lower() in ("a", "always"):
            return Decision(True, always=True)
        return Decision(False, note="" if ans.lower() in ("n", "no") else ans)

    def ask(self, question, options):
        print(f"{YELLOW}{question}{RESET}")
        for i, o in enumerate(options, 1):
            print(f"  {i}. {o}")
        ans = input("> ").strip()
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        return ans


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
        try:
            agent.run(text)
        except KeyboardInterrupt:
            agent.stop.set()
            print(f"\n{YELLOW}Stopped.{RESET}")
        except ServerError as e:
            print(f"{RED}{e}{RESET}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
