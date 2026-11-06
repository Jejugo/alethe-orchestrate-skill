import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "alethe" / "scripts"


def load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


orchestrate = load("orchestrate", "orchestrate_watch")
watch = load("watch", "watch_module")


class SilentReporter:
    def __init__(self):
        self.calls = []

    def report(self, state, message):
        self.calls.append((state, message))


class Wave:
    def __init__(self, root):
        self.run = root / "run"
        (self.run / "workers" / "scout").mkdir(parents=True)
        (self.run / "workers" / "build").mkdir(parents=True)
        self.plan = {"title": "Demo", "workers": [
            {"id": "scout", "provider": "codex", "model": "mini", "mode": "read-only"},
            {"id": "build", "provider": "claude", "model": "sonnet", "mode": "write", "permissions": "full"},
        ]}
        (self.run / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")
        self.state({"status": "running", "workers": {"scout": {"status": "running"}, "build": {"status": "queued"}}})

    def state(self, value):
        (self.run / "state.json").write_text(json.dumps(value), encoding="utf-8")

    def log(self, worker, *events):
        with (self.run / "workers" / worker / "stdout.log").open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")


class EventTests(unittest.TestCase):
    def test_codex_events_become_readable_lines(self):
        lines = [line for event in (
            {"type": "item.started", "item": {"type": "command_execution", "command": "rg charge src"}},
            {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 2}},
            {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Found it."}},
            {"type": "error", "message": "boom"},
        ) for line in watch.event_lines("codex", event)]
        self.assertEqual(lines, ["$ rg charge src", "  ↳ exit 2", "✎ update a.py", "› Found it.", "! boom"])

    def test_claude_events_become_readable_lines(self):
        events = [
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Reading."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/x.ts"}},
            ]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "denied"}]}},
            {"type": "result", "subtype": "success", "total_cost_usd": 0.5},
        ]
        lines = [line for event in events for line in watch.event_lines("claude", event)]
        self.assertEqual(lines, ["› Reading.", "$ npm test", "Edit src/x.ts", "  ↳ error: denied", "result: success, $0.5000"])

    def test_unknown_events_and_text_are_ignored(self):
        self.assertEqual(watch.event_lines("codex", "not json"), [])
        self.assertEqual(watch.event_lines("kimi", {"role": "assistant", "content": "hi"}), ["› hi"])


class FollowAndSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wave = Wave(Path(self.tmp.name))

    def test_follow_prints_new_events_then_the_result(self):
        wave = self.wave
        wave.log("scout", {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})
        steps = iter([
            lambda: wave.log("scout", {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}}),
            lambda: ((wave.run / "workers" / "scout" / "result.md").write_text("SUMMARY: ok\n"),
                     wave.state({"status": "running", "workers": {"scout": {"status": "succeeded", "durationSeconds": 3}}})),
        ])
        out, reporter = io.StringIO(), SilentReporter()
        code = watch.follow(wave.run, "scout", out=out, reporter=reporter, sleep=lambda _: next(steps)())
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("== scout · codex · mini", text)
        self.assertIn("$ ls", text)
        self.assertIn("› Done.", text)
        self.assertIn("== succeeded in 3s", text)
        self.assertIn("SUMMARY: ok", text)
        self.assertEqual(reporter.calls[0][0], "working")
        self.assertEqual(reporter.calls[-1], ("idle", "scout: succeeded"))

    def test_follow_stops_when_the_runner_is_gone(self):
        self.wave.state({"status": "failed", "workers": {"build": {"status": "queued"}}})
        out = io.StringIO()
        self.assertEqual(watch.follow(self.wave.run, "build", out=out, reporter=SilentReporter(), sleep=lambda _: None), 0)
        self.assertIn("== queued", out.getvalue())

    def test_summary_lists_access_tokens_and_cost(self):
        self.wave.state({"status": "succeeded", "workers": {
            "scout": {"status": "succeeded", "durationSeconds": 65, "metrics": {"usage": {"input_tokens": 1000, "output_tokens": 200}}},
            "build": {"status": "failed", "durationSeconds": 5, "metrics": {"totalCostUsd": 0.25}},
        }})
        out = io.StringIO()
        watch.summary(self.wave.run, orchestrate.reported_total_tokens, out=out, once=True)
        text = out.getvalue()
        self.assertIn("Demo  [succeeded]", text)
        self.assertRegex(text, r"scout\s+codex · mini\s+read\s+succeeded\s+1m05s\s+1,200")
        self.assertRegex(text, r"build\s+claude · sonnet\s+full\s+failed\s+5s\s+-\s+\$0.2500")


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wave = Wave(Path(self.tmp.name))
        binary = Path(self.tmp.name) / "herdr"
        binary.write_text("", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w9", "HERDR_PANE_ID": "w9:p1",
                                                   "HERDR_BIN_PATH": str(binary)})
        environment.start()
        self.addCleanup(environment.stop)
        self.calls = []
        self.counter = iter(range(2, 100))

    def fake(self, args, **_):
        self.calls.append(args[1:])
        if args[1:3] == ["tab", "create"]:
            payload = {"result": {"tab": {"tab_id": "w9:t2"}, "root_pane": {"pane_id": "w9:p2"}}}
        elif args[1:3] == ["pane", "split"]:
            payload = {"result": {"pane": {"pane_id": f"w9:p{next(self.counter) + 1}"}}}
        else:
            payload = {"result": {"type": "ok"}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    def test_open_creates_a_background_tab_with_one_feed_per_worker(self):
        opened = watch.open_herdr(self.wave.run, Path("/x/orchestrate.py"), python="py", runner=self.fake)
        self.assertEqual(opened["tab"], "w9:t2")
        self.assertEqual(sorted(opened["workerPanes"]), ["build", "scout"])
        create = self.calls[0]
        self.assertIn("--no-focus", create)
        splits = [call for call in self.calls if call[:2] == ["pane", "split"]]
        self.assertEqual(len(splits), 2)
        self.assertTrue(all("--no-focus" in call for call in splits))
        runs = [call[3] for call in self.calls if call[:2] == ["pane", "run"]]
        self.assertTrue(runs[0].startswith("clear; py /x/orchestrate.py watch"))
        self.assertTrue(any(run.endswith(" follow " + str(self.wave.run) + " scout") for run in runs))
        self.assertFalse(any(call[:2] == ["pane", "close"] for call in self.calls))

    def test_reporter_names_the_worker_in_the_sidebar(self):
        reporter = watch.HerdrReporter("scout · codex", runner=self.fake)
        reporter.report("working", "scout: running")
        reporter.report("working", "scout: running")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][:3], ["pane", "report-agent", "w9:p1"])
        self.assertIn("scout · codex", self.calls[1])


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = Path(self.tmp.name) / "policy.json"

    def test_watch_defaults_off_and_toggles_without_finishing_onboarding(self):
        self.assertEqual(orchestrate.load_policy(self.policy)["watch"], "off")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(orchestrate.main(["policy", "set", "--watch", "on", "--file", str(self.policy)]), 0)
        policy = orchestrate.load_policy(self.policy)
        self.assertEqual(policy["watch"], "on")
        self.assertFalse(policy["onboarded"])
        with self.assertRaises(orchestrate.PlanError):
            orchestrate.validate_policy({"watch": "maybe"})

    def test_auto_watch_only_opens_when_on_and_never_raises(self):
        wave = Wave(Path(self.tmp.name))
        self.assertIsNone(orchestrate.auto_watch(wave.run, self.policy))
        orchestrate.save_policy(self.policy, {"watch": "on"})
        with mock.patch.dict(os.environ, {"HERDR_ENV": ""}):
            self.assertIsNone(orchestrate.auto_watch(wave.run, self.policy))
        broken = SimpleNamespace(in_herdr=lambda: True, open_herdr=mock.Mock(side_effect=RuntimeError("no server")))
        with mock.patch.object(orchestrate, "watch_module", return_value=broken):
            self.assertEqual(orchestrate.auto_watch(wave.run, self.policy), {"error": "no server"})


if __name__ == "__main__":
    unittest.main()
