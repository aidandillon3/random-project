import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LOCALAGENT_DATA"] = tempfile.mkdtemp()

from localagent.agent import Agent, AgentUI  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.llm import extract_text_tool_calls, parse_args, ThinkSplitter  # noqa: E402
from localagent.permissions import Decision, Policy, is_dangerous  # noqa: E402
from localagent.tools import ToolResult  # noqa: E402
from tests.mockserver import MockServer, calls, text  # noqa: E402


class RecUI(AgentUI):
    def __init__(self, allow=True, always=False, note=""):
        self.allow, self.always, self.note = allow, always, note
        self.approvals, self.texts, self.tools, self.notices, self.todo_lists = [], [], [], [], []

    def text(self, s):
        self.texts.append(s)

    def approve(self, req):
        self.approvals.append(req)
        return Decision(self.allow, self.always, self.note)

    def tool_end(self, call_id, name, result):
        self.tools.append((name, result))

    def notice(self, s):
        self.notices.append(s)

    def todos(self, t):
        self.todo_lists.append(t)


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def make(self, replies, ui=None, **kw):
        self.srv = MockServer(replies, **{k: kw.pop(k) for k in ("n_ctx", "reject_tools") if k in kw})
        self.addCleanup(self.srv.close)
        s = Settings(base_url=self.srv.url, workspace=str(self.ws), enable_control=False, **kw)
        ui = ui or RecUI()
        return Agent(s, ui), ui


class AgentLoopTest(Base):
    def test_write_then_read_then_answer(self):
        agent, ui = self.make([
            calls(("write_file", {"path": "notes/a.txt", "content": "hello\nworld\n"})),
            calls(("read_file", {"path": "notes/a.txt"})),
            text("All done."),
        ])
        out = agent.run("make a file")
        self.assertEqual(out, "All done.")
        self.assertEqual((self.ws / "notes/a.txt").read_text(), "hello\nworld\n")
        self.assertEqual(len(ui.approvals), 1)          # write asks, read does not
        self.assertEqual(ui.approvals[0].tool, "write_file")
        read_result = ui.tools[1][1].text
        self.assertIn("1| hello", read_result)
        # tool results reach the model as role=tool with matching ids
        last = self.srv.requests[-1]["messages"]
        tool_msgs = [m for m in last if m["role"] == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        ids = [c["id"] for m in last if m.get("tool_calls") for c in m["tool_calls"]]
        self.assertEqual([m["tool_call_id"] for m in tool_msgs], ids)
        self.assertEqual(last[0]["role"], "system")
        self.assertTrue(all(not k.startswith("_") for m in last for k in m))

    def test_denied_with_note(self):
        agent, ui = self.make([calls(("write_file", {"path": "x.txt", "content": "x"})), text("ok")],
                              ui=RecUI(allow=False, note="put it in docs/ instead"))
        agent.run("go")
        self.assertFalse((self.ws / "x.txt").exists())
        tool_msg = [m for m in self.srv.requests[-1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("denied", tool_msg["content"])
        self.assertIn("docs/ instead", tool_msg["content"])

    def test_always_allow_for_session(self):
        agent, ui = self.make([calls(("write_file", {"path": "a", "content": "1"})),
                               calls(("write_file", {"path": "b", "content": "2"})), text("ok")],
                              ui=RecUI(always=True))
        agent.run("go")
        self.assertEqual(len(ui.approvals), 1)
        self.assertTrue((self.ws / "b").exists())

    def test_read_only_mode_refuses(self):
        agent, ui = self.make([calls(("write_file", {"path": "a", "content": "1"})), text("ok")],
                              approval_mode="read_only")
        agent.run("go")
        self.assertFalse((self.ws / "a").exists())
        self.assertEqual(ui.approvals, [])

    def test_auto_edit_asks_outside_workspace(self):
        outside = Path(tempfile.mkdtemp()) / "o.txt"
        agent, ui = self.make([calls(("write_file", {"path": "in.txt", "content": "1"}),
                                     ("write_file", {"path": str(outside), "content": "2"})), text("ok")],
                              approval_mode="auto_edit")
        agent.run("go")
        self.assertEqual([r.args["path"] for r in ui.approvals], [str(outside)])

    def test_dangerous_command_asks_even_in_full_auto(self):
        agent, ui = self.make([calls(("run_command", {"command": "echo hi"})),
                               calls(("run_command", {"command": "rm -rf /"})), text("ok")],
                              approval_mode="auto", ui=RecUI(allow=False))
        agent.run("go")
        self.assertEqual(len(ui.approvals), 1)
        self.assertTrue(ui.approvals[0].dangerous)
        self.assertIn("hi", ui.tools[0][1].text)

    def test_bad_json_and_unknown_tool_are_reported(self):
        agent, ui = self.make([calls(("read_file", '{"path": "a.txt",,')), calls(("nope", {})), text("ok")])
        agent.run("go")
        msgs = [m["content"] for m in self.srv.requests[-1]["messages"] if m["role"] == "tool"]
        self.assertIn("Error", msgs[0])
        self.assertIn("no tool called 'nope'", msgs[1])

    def test_falls_back_to_prompt_tools(self):
        agent, ui = self.make([text('<tool_call>\n{"name": "list_dir", "arguments": {}}\n</tool_call>'),
                               text("fine")], reject_tools=True)
        out = agent.run("go")
        self.assertEqual(out, "fine")
        self.assertTrue(agent.prompt_mode)
        last = self.srv.requests[-1]
        self.assertNotIn("tools", last)
        self.assertIn("<tool_call>", last["messages"][0]["content"])
        self.assertIn("<tool_response", last["messages"][-1]["content"])
        self.assertTrue(ui.notices)

    def test_text_tool_calls_in_native_mode(self):
        agent, ui = self.make([text('<tool_call>{"name": "write_file", "arguments": {"path": "t.txt", "content": "x"}}</tool_call>'),
                               text("ok")])
        agent.run("go")
        self.assertTrue((self.ws / "t.txt").exists())

    def test_reasoning_and_think_tags(self):
        agent, ui = self.make([text("<think>hmm</think>Answer", reasoning="plan")])
        out = agent.run("q")
        self.assertEqual(out, "Answer")
        self.assertEqual(agent.messages[-1]["reasoning_content"], "planhmm")

    def test_todos(self):
        agent, ui = self.make([calls(("todo_write", {"todos": [{"content": "a", "status": "in_progress"},
                                                                {"content": "b", "status": "completed"}]})),
                               text("ok")])
        agent.run("go")
        self.assertEqual(ui.todo_lists[-1][1]["status"], "done")

    def test_repeat_warning(self):
        same = calls(("list_dir", {}))
        agent, ui = self.make([same, same, same, text("ok")])
        agent.run("go")
        self.assertIn("several times", ui.tools[2][1].text)

    def test_subagent(self):
        agent, ui = self.make([calls(("delegate_task", {"task": "count files"})),
                               text("there are 0 files"),       # the helper's answer
                               text("Helper says 0.")])
        out = agent.run("go")
        self.assertEqual(out, "Helper says 0.")
        self.assertIn("there are 0 files", ui.tools[0][1].text)
        helper_req = self.srv.requests[1]
        self.assertIn("helper agent", helper_req["messages"][0]["content"])
        self.assertNotIn("delegate_task", [t["function"]["name"] for t in helper_req["tools"]])

    def test_max_steps(self):
        agent, ui = self.make([calls(("list_dir", {"path": "."}))] * 5, max_steps=3)
        agent.run("go")
        self.assertIn("Paused", ui.notices[-1])


class ContextTest(Base):
    def test_images_pruned_in_batches(self):
        agent, _ = self.make([], keep_images=2)
        for i in range(4):
            agent._record_results([(type("C", (), {"id": f"i{i}", "name": "screenshot"})(),
                                    ToolResult("shot", images=[b"png"]))])
        count = lambda: sum(1 for m in agent.messages if isinstance(m.get("content"), list)
                            for p in m["content"] if p.get("type") == "image_url")
        self.assertEqual(count(), 4)       # 4 <= keep + 2: nothing rewritten yet
        agent._record_results([(type("C", (), {"id": "i5", "name": "screenshot"})(),
                                ToolResult("shot", images=[b"png"]))])
        self.assertEqual(count(), 2)

    def test_compaction_keeps_recent_steps(self):
        big = "x" * 4000
        replies = [calls(("write_file", {"path": f"f{i}.txt", "content": big})) for i in range(6)] + [text("done")]
        agent, ui = self.make(replies, n_ctx=8000, approval_mode="auto")
        out = agent.run("write many files")
        self.assertEqual(out, "done")
        self.assertTrue(any("Summarized" in n for n in ui.notices))
        self.assertEqual(agent.messages[0]["_kind"], "summary")
        self.assertIn("SUMMARY: earlier work", agent.messages[0]["content"])
        self.assertIn("write many files", agent.messages[0]["content"])
        # the wire format still pairs every tool message with its call
        wire = agent.wire_messages()
        ids = {c["id"] for m in wire if m.get("tool_calls") for c in m["tool_calls"]}
        self.assertTrue(all(m["tool_call_id"] in ids for m in wire if m["role"] == "tool"))


class MemoryTest(Base):
    def test_matching_notes_are_attached_to_the_request(self):
        from localagent.memory import get_memory

        get_memory().add("Aidan's dog is called Biscuit", "Dog name")
        get_memory().add("The router password is on the fridge", "Router")
        agent, ui = self.make([text("Biscuit!")])
        agent.run("what is my dog called")
        sent = self.srv.requests[0]["messages"][1]["content"]
        self.assertIn("Biscuit", sent)
        self.assertNotIn("router", sent.lower())
        self.assertEqual(agent.messages[0]["_shown"], "what is my dog called")

    def test_pinned_notes_are_in_the_system_prompt(self):
        from localagent.memory import get_memory

        get_memory().add("Always answer in British English", "Spelling", pinned=True)
        agent, _ = self.make([text("ok")])
        self.assertIn("Always answer in British English", agent.system_prompt())

    def test_saves_memories_after_a_bigger_task(self):
        from localagent.memory import get_memory

        todo = ("todo_write", {"todos": [{"content": "a", "status": "in_progress"}]})
        agent, ui = self.make([calls(todo), calls(todo), calls(todo), text("All done."),
                               text('Sure:\n```json\n[{"title": "Code editor", "note": "Aidan edits code in '
                                    'VS Code", "replaces": null}]\n```')])
        self.assertEqual(agent.run("set up my editor config"), "All done.")
        self.assertEqual(len(self.srv.requests), 5)
        ask = self.srv.requests[-1]
        self.assertIn("Memory check", ask["messages"][-1]["content"])
        # same tools and same conversation prefix as the last real step, so the prompt cache is reused
        self.assertEqual(ask["tools"], self.srv.requests[-2]["tools"])
        self.assertEqual(ask["messages"][:len(self.srv.requests[-2]["messages"])], self.srv.requests[-2]["messages"])
        self.assertEqual(get_memory().search_notes("code editor")[0].body, "Aidan edits code in VS Code")
        self.assertTrue(any(n.startswith("Remembered: Code editor") for n in ui.notices))
        # the extra question isn't part of the conversation
        self.assertNotIn("Memory check", json.dumps(agent.messages))

    def test_no_memory_check_for_small_tasks_or_when_off(self):
        agent, _ = self.make([calls(("todo_write", {"todos": []})), text("ok")])
        agent.run("hi")
        self.assertEqual(len(self.srv.requests), 2)
        todo = ("todo_write", {"todos": []})
        agent, _ = self.make([calls(todo), calls(todo), calls(todo), text("ok")], auto_memory=False)
        agent.run("hi")
        self.assertEqual(len(self.srv.requests), 4)


class ParsingTest(unittest.TestCase):
    def test_parse_args_forgiving(self):
        self.assertEqual(parse_args('{"a": 1,}'), {"a": 1})
        self.assertEqual(parse_args('"{\\"a\\": 2}"'), {"a": 2})
        self.assertEqual(parse_args('{"a": {"b": 1}'), {"a": {"b": 1}})
        self.assertEqual(parse_args(""), {})

    def test_extract_xml_style(self):
        s = "ok <tool_call><function=read_file><parameter=path>a.py</parameter><parameter=limit>5</parameter></function></tool_call>"
        clean, cs = extract_text_tool_calls(s)
        self.assertEqual(clean, "ok")
        self.assertEqual(cs[0].name, "read_file")
        self.assertEqual(json.loads(cs[0].arguments), {"path": "a.py", "limit": 5})

    def test_think_splitter_split_tags(self):
        sp = ThinkSplitter()
        out = []
        for chunk in ["<thi", "nk>abc</th", "ink>answer"]:
            out += sp.feed(chunk)
        out += sp.flush()
        r = "".join(s for k, s in out if k == "reasoning")
        t = "".join(s for k, s in out if k == "text")
        self.assertEqual((r, t), ("abc", "answer"))

    def test_dangerous(self):
        for c in ["format C:", "Remove-Item C:\\x -Recurse -Force", "rd /s /q C:\\", "shutdown /s",
                  "rm -rf ~", "git push --force origin main", "reg delete HKLM\\x"]:
            self.assertTrue(is_dangerous(c), c)
        for c in ["dir", "Get-ChildItem", "python main.py", "git push", "rm notes.txt"]:
            self.assertFalse(is_dangerous(c), c)

    def test_policy(self):
        p = Policy("ask")
        self.assertTrue(p.needs_approval("edit", False))
        self.assertFalse(p.needs_approval("safe", False))
        p.session_allowed.add("edit")
        self.assertFalse(p.needs_approval("edit", False))
        self.assertTrue(p.needs_approval("edit", True))
        self.assertIsNone(Policy("read_only").needs_approval("run", False))


if __name__ == "__main__":
    unittest.main()
