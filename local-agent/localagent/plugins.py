"""Your own tools, without touching the app: every .py file in data/plugins/ is loaded when the
app starts (and when you press Reload in Settings). A plugin registers tools with the same
decorator the built-in tools use:

    from localagent.tools.base import RUN, tool

    @tool("word_count", "Count the words in a text file in the work folder.",
          {"path": {"type": "string", "description": "The file"}}, required=["path"])
    def word_count(ctx, a):
        return f"{len(ctx.resolve(a['path']).read_text(encoding='utf-8').split())} words"

The function gets the tool context (work folder, settings, the Stop flag) and the model's
arguments, and returns text (or a ToolResult with images). Give it risk=EDIT, RUN or PUBLISH when
it changes something, so it asks first like the built-in tools. Files starting with _ are skipped.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .config import DATA_DIR
from .tools.base import REGISTRY

EXAMPLE = '''"""An example plugin: rename this file to end in .py to load it."""
from localagent.tools.base import tool


@tool("word_count", "Count the words in a text file.",
      {"path": {"type": "string", "description": "The file, relative to the work folder"}}, required=["path"])
def word_count(ctx, a):
    text = ctx.resolve(a["path"]).read_text(encoding="utf-8", errors="replace")
    return f"{len(text.split())} words"
'''

_loaded: dict[str, list[str]] = {}      # plugin file -> the tool names it registered
errors: dict[str, str] = {}


def plugins_dir() -> Path:
    return DATA_DIR / "plugins"


def load(folder: Path | None = None) -> list[str]:
    """(Re)load every plugin file; returns the tool names they registered. Problems go to `errors`."""
    folder = folder or plugins_dir()
    for names in _loaded.values():        # a reload drops tools whose plugin changed or went away
        for n in names:
            REGISTRY.pop(n, None)
    _loaded.clear()
    errors.clear()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        example = folder / "example_word_count.py.txt"
        if not any(folder.iterdir()):
            example.write_text(EXAMPLE, encoding="utf-8")
    except OSError:
        return []
    out = []
    for f in sorted(folder.glob("*.py")):
        if f.name.startswith("_"):
            continue
        before = dict(REGISTRY)
        mod_name = f"localagent_plugin_{f.stem}"
        try:
            spec = importlib.util.spec_from_file_location(mod_name, f)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001 - a broken plugin mustn't stop the app
            errors[f.name] = f"{type(e).__name__}: {e}"
            for n in set(REGISTRY) - set(before):
                REGISTRY.pop(n, None)
            _restore(before)            # it may have swapped out a built-in before failing
            continue
        replaced = _restore(before)
        if replaced:
            errors[f.name] = f"Skipped tools named like built-in ones: {', '.join(replaced)}"
        names = sorted(set(REGISTRY) - set(before))
        _loaded[f.name] = names
        out += names
    return out


def _restore(before: dict) -> list[str]:
    """A plugin can add tools, not swap out the built-in ones: put back any it replaced."""
    replaced = [n for n, t in before.items() if REGISTRY.get(n, t) is not t]   # MCP tools can go meanwhile
    for n in replaced:
        REGISTRY[n] = before[n]
    return replaced


def summary() -> str:
    n_tools = sum(len(v) for v in _loaded.values())
    parts = [f"{len(_loaded)} plugin file(s), {n_tools} tool(s)"] if _loaded else []
    parts += [f"{name}: {err}" for name, err in errors.items()]
    return "; ".join(parts) or "No plugins yet."
