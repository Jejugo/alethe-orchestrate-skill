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
        code = watch.follow(wave.run, "scout", out=out, reporter=reporter, sleep=lambda _: next(steps)(), feed="all")
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("== scout · codex · mini", text)
        self.assertIn("$ ls", text)
        self.assertIn("› Done.", text)
        self.assertIn("== succeeded in 3s", text)
        self.assertIn("SUMMARY: ok", text)
        self.assertEqual(reporter.calls[0][0], "working")
        self.assertEqual(reporter.calls[-1], ("idle", "scout: succeeded"))

    def test_the_default_feed_shows_only_what_the_agent_says(self):
        wave, now = self.wave, [0.0]
        wave.log("scout",
                 {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'cd src && rg x'"}},
                 {"type": "item.completed", "item": {"type": "agent_message", "text": "Achados consolidados; registrando."}},
                 {"type": "item.started", "item": {"type": "command_execution", "command": "cd src && ls"}},
                 {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}})

        def step(_):
            now[0] += 61
            if now[0] > 100:
                wave.state({"status": "running", "workers": {"scout": {"status": "succeeded", "durationSeconds": 3}}})

        out = io.StringIO()
        watch.follow(wave.run, "scout", out=out, reporter=SilentReporter(), sleep=step, timer=lambda: now[0])
        text = out.getvalue()
        self.assertIn("› Achados consolidados; registrando.", text)
        self.assertNotIn("cd src", text)
        self.assertNotIn("✎", text)
        self.assertIn("… working, 2 steps since the last message", text)
        self.assertEqual(watch.event_lines("claude", {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading."}, {"type": "tool_use", "name": "Bash", "input": {"command": "cd x"}}]}},
            actions=False), ["› Reading."])

    def test_in_a_terminal_actions_pass_on_one_line_and_messages_stay(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        wave = self.wave
        wave.log("scout",
                 {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'cd /repo && rg x src'"}},
                 {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
                 {"type": "item.completed", "item": {"type": "agent_message", "text": "Achados consolidados."}})
        wave.state({"status": "running", "workers": {"scout": {"status": "succeeded", "durationSeconds": 3}}})
        out = Terminal()
        watch.follow(wave.run, "scout", out=out, reporter=SilentReporter(), sleep=lambda _: None)
        text = out.getvalue()
        self.assertIn("\r\x1b[2K\x1b[2m", text)
        self.assertIn("⋯ $ rg x src\x1b[0m\r\x1b[2K", text)
        permanent = text.split("\r\x1b[2K")[-1]
        self.assertIn("› Achados consolidados.\n", permanent)
        self.assertNotIn("rg x", permanent)
        self.assertNotIn("cd /repo", text)
        self.assertNotIn("working,", text)

    def test_shell_wrappers_and_leading_cd_are_dropped(self):
        self.assertEqual(watch.readable_command("/bin/zsh -lc 'cd /a/b && rg x'"), "rg x")
        self.assertEqual(watch.readable_command('bash -lc "cd \'/a b\' && npm test"'), "npm test")
        self.assertEqual(watch.readable_command("cd src"), "cd src")
        self.assertEqual(watch.readable_command("git status"), "git status")

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

    def test_running_workers_show_live_usage_counted_once_per_message(self):
        self.wave.state({"status": "running", "workers": {"scout": {"status": "running"}, "build": {"status": "running"}}})
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 5}
        self.wave.log("build",
                      {"type": "assistant", "message": {"id": "m1", "usage": usage, "content": []}},
                      {"type": "assistant", "message": {"id": "m1", "usage": {**usage, "output_tokens": 50}, "content": []}},
                      {"type": "assistant", "message": {"id": "m2", "usage": usage, "content": []}})
        self.wave.log("scout", {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 400,
                                                                     "output_tokens": 100}})
        seen = []
        estimate = lambda provider, model, used: seen.append((provider, model, used)) or 1.5
        live = watch.LiveUsage()
        rows = {row[0]: row for row in watch.summary_rows(self.wave.run, orchestrate.reported_total_tokens, estimate, live)}
        self.assertEqual(rows["build"][5:], ["~275", "~$1.5000"])
        self.assertEqual(rows["scout"][5], "~600")
        self.assertIn(("claude", "sonnet", {"input_tokens": 20, "cache_creation_input_tokens": 200,
                                            "cache_read_input_tokens": 2000, "output_tokens": 55}), seen)
        self.wave.log("build", {"type": "assistant", "message": {"id": "m3", "usage": usage, "content": []}})
        rows = {row[0]: row for row in watch.summary_rows(self.wave.run, orchestrate.reported_total_tokens, estimate, live)}
        self.assertEqual(rows["build"][5], "~390")

    def test_final_metrics_win_over_the_live_stream(self):
        self.wave.log("build", {"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 9}}})
        self.wave.state({"status": "running", "workers": {"build": {"status": "succeeded", "metrics": {
            "usage": {"input_tokens": 1000, "output_tokens": 200}, "totalCostUsd": 0.25}}}})
        row = watch.summary_rows(self.wave.run, orchestrate.reported_total_tokens, lambda *_: 9.0)[1]
        self.assertEqual(row[5:], ["1,200", "$0.2500"])

    def test_live_cost_uses_the_price_table(self):
        estimate = orchestrate.live_cost_estimator(Path(self.tmp.name) / "policy.json")
        cost = estimate("claude", "opus", {"input_tokens": 1_000_000, "output_tokens": 0})
        prices = orchestrate.load_prices(Path(self.tmp.name) / "policy.json")["models"]
        self.assertAlmostEqual(cost, prices["claude/opus"]["input"])
        self.assertIsNone(estimate("kimi", "unknown-model", {"output_tokens": 5}))


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


PHASES = [
    {"id": "plan", "role": "planner", "who": "lead"},
    {"id": "build", "role": "builder"},
    {"id": "test", "role": "tester", "if": "risky"},
    {"id": "test-deep", "role": "tester", "with": "test", "if": "deep"},
    {"id": "review", "role": "reviewer"},
    {"id": "fix", "role": "fixer", "after": "review", "if": "checks-failed"},
]


class Orchestration:
    def __init__(self, root):
        self.folder = root / "orch"
        self.folder.mkdir()
        (self.folder / "orchestration.json").write_text(json.dumps(
            {"title": "Feature X", "playbook": "complete", "createdAt": "2026-11-08T10:00:00+00:00"}), encoding="utf-8")

    def wave(self, name, phase, workers, status="running"):
        wave = self.folder / name
        for worker in workers:
            (wave / "workers" / worker).mkdir(parents=True)
        (wave / "plan.json").write_text(json.dumps({"phase": phase, "workers": [
            {"id": worker, "provider": "claude", "model": "sonnet", "mode": "write"} for worker in workers]}), encoding="utf-8")
        state = {"status": status, "workers": {worker: {"status": "succeeded" if status == "succeeded" else "running",
                                                        "metrics": {"totalCostUsd": 0.5}} for worker in workers}}
        (wave / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return wave

    def note(self, kind, text):
        with (self.folder / "journal.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "text": text}) + "\n")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.orch = Orchestration(Path(self.tmp.name))

    def text(self):
        return watch.pipeline_text(self.orch.folder, PHASES, orchestrate.reported_total_tokens,
                                   now=watch.parse_time("2026-11-08T11:05:00+00:00"))

    def test_each_phase_shows_its_state_and_workers(self):
        self.orch.wave("01-build", "build", ["builder-1", "builder-2"], "succeeded")
        self.orch.wave("02-test", "test", ["tester"])
        text = self.text()
        self.assertIn("Feature X\ncomplete · 1h05m · $1.50 · 2 of 4 phases done", text)
        self.assertRegex(text, r"✓ plan\s+planner\s+lead, in your session")
        self.assertRegex(text, r"✓ build\s+builder\s+2 workers")
        self.assertIn("    builder-2  sonnet  succeeded", text)
        self.assertRegex(text, r"▶ test\s+tester\s+1 worker")
        self.assertRegex(text, r"– test-deep\s+tester\s+skipped: another path of this step ran")
        self.assertRegex(text, r"· review\s+reviewer\s+waiting")
        self.assertRegex(text, r"· fix\s+fixer\s+only if review fails")

    def test_the_lead_phase_ahead_is_marked_next(self):
        self.assertRegex(self.text(), r"▶ plan\s+planner\s+lead, in your session")

    def test_skipped_and_passed_over_phases_are_explained(self):
        self.orch.note("skip", "test: no important feature")
        self.orch.note("lead", "review: diff matches the plan")
        self.orch.wave("01-build", "build", ["builder"], "succeeded")
        text = self.text()
        self.assertRegex(text, r"– test\s+tester\s+skipped: no important feature")
        self.assertRegex(text, r"✓ review\s+reviewer\s+done by the lead: diff matches the plan")

    def test_waves_outside_the_playbook_are_listed(self):
        self.orch.wave("01-hotfix", "hotfix", ["patch"], "succeeded")
        self.assertRegex(self.text(), r"✓ hotfix\s+added\s+1 worker")


class OrchestrationPaneTests(HerdrTests):
    def test_one_tab_per_orchestration_is_reused(self):
        orch = Orchestration(Path(self.tmp.name))
        opened = watch.open_orchestration(orch.folder, Path("/x/orchestrate.py"), python="py", runner=self.fake)
        self.assertFalse(opened["reused"])
        runs = [call[3] for call in self.calls if call[:2] == ["pane", "run"]]
        self.assertTrue(runs[0].endswith("watch " + str(orch.folder) + " --here --panes"))
        self.assertEqual(json.loads((orch.folder / "herdr.json").read_text())["pipelinePane"], "w9:p2")
        self.calls.clear()
        self.assertTrue(watch.open_orchestration(orch.folder, Path("/x/orchestrate.py"), runner=self.fake)["reused"])
        self.assertFalse(any(call[:2] == ["tab", "create"] for call in self.calls))

    def test_worker_panes_follow_the_current_wave(self):
        orch = Orchestration(Path(self.tmp.name))
        first = orch.wave("01-build", "build", ["builder-1", "builder-2"], "succeeded")
        panes = watch.WavePanes("w9:p1", watch.launcher("py", Path("/x/o.py")), runner=self.fake)
        panes.sync(first)
        self.assertEqual(sorted(panes.panes), ["builder-1", "builder-2"])
        self.assertIn(["pane", "rename", panes.panes["builder-1"], "build · builder-1"], self.calls)
        old = list(panes.panes.values())
        self.calls.clear()
        panes.sync(first)
        self.assertEqual(self.calls, [])
        panes.sync(orch.wave("02-test", "test", ["tester"]))
        closed = [call[2] for call in self.calls if call[:2] == ["pane", "close"]]
        self.assertEqual(sorted(closed), sorted(old))
        self.assertEqual(list(panes.panes), ["tester"])


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TabCloseTests(HerdrTests):
    def closes(self):
        return [call for call in self.calls if call[:2] == ["tab", "close"]]

    def test_a_finished_orchestration_closes_its_tab_after_the_grace_period(self):
        orch = Orchestration(Path(self.tmp.name))
        orch.wave("01-build", "build", ["builder"], "succeeded")
        (orch.folder / "herdr.json").write_text(json.dumps({"tab": "w9:t7", "pipelinePane": "w9:p2"}), encoding="utf-8")
        tab = json.loads((orch.folder / "herdr.json").read_text())["tab"]
        clock, out = Clock(), io.StringIO()
        closer = watch.TabCloser(tab, 120, runner=self.fake, timer=clock)
        self.assertEqual(watch.pipeline(orch.folder, PHASES, orchestrate.reported_total_tokens, out=out, poll=30,
                                        sleep=clock.sleep, closer=closer), 0)
        self.assertEqual(self.closes(), [["tab", "close", "w9:t7"]])
        self.assertGreaterEqual(clock.now, 120)
        self.assertIn("this tab closes in 2m00s", out.getvalue())
        self.assertTrue((orch.folder / "01-build" / "state.json").is_file())

    def test_a_new_wave_during_the_grace_period_cancels_the_close(self):
        orch = Orchestration(Path(self.tmp.name))
        orch.wave("01-build", "build", ["builder"], "succeeded")
        clock = Clock()
        closer = watch.TabCloser("w9:t7", 120, runner=self.fake, timer=clock)
        self.assertTrue(closer.update(watch.orchestration_finished(orch.folder)).startswith("finished"))
        clock.now = 60
        orch.wave("02-test", "test", ["tester"])
        self.assertEqual(closer.update(watch.orchestration_finished(orch.folder)), "")
        clock.now = 500
        self.assertEqual(closer.update(watch.orchestration_finished(orch.folder)), "")
        self.assertEqual(self.closes(), [])

    def test_zero_seconds_never_closes(self):
        orch = Orchestration(Path(self.tmp.name))
        orch.wave("01-build", "build", ["builder"], "succeeded")
        clock = Clock()
        closer = watch.TabCloser("w9:t7", 0, runner=self.fake, timer=clock)
        for clock.now in (0, 1000, 100000):
            self.assertEqual(closer.update(watch.orchestration_finished(orch.folder)), "")
        self.assertEqual(self.closes(), [])
        self.assertIsNone(orchestrate.watch_closer(watch, None))

    def test_a_failed_close_does_not_raise(self):
        def broken(args, **_):
            return SimpleNamespace(returncode=1, stdout="", stderr="tab not found")
        clock = Clock()
        closer = watch.TabCloser("w9:t7", 10, runner=broken, timer=clock)
        closer.update(True)
        clock.now = 10
        self.assertEqual(closer.update(True), "closed")

    def test_a_single_wave_tab_closes_after_the_wave_ends(self):
        opened = watch.open_herdr(self.wave.run, Path("/x/orchestrate.py"), python="py", runner=self.fake)
        runs = [call[3] for call in self.calls if call[:2] == ["pane", "run"]]
        self.assertTrue(runs[0].endswith("--here --close-tab " + opened["tab"]))
        self.wave.state({"status": "succeeded", "workers": {"scout": {"status": "succeeded"}, "build": {"status": "succeeded"}}})
        clock = Clock()
        closer = watch.TabCloser(opened["tab"], 120, runner=self.fake, timer=clock)
        watch.summary(self.wave.run, orchestrate.reported_total_tokens, out=io.StringIO(), poll=30,
                      sleep=clock.sleep, closer=closer)
        self.assertEqual(self.closes(), [["tab", "close", "w9:t2"]])

    def test_watch_here_by_hand_never_closes(self):
        orch = Orchestration(Path(self.tmp.name))
        orch.wave("01-build", "build", ["builder"], "succeeded")
        (orch.folder / "herdr.json").write_text(json.dumps({"tab": "w9:t7", "pipelinePane": "w9:p2"}), encoding="utf-8")
        seen = {}
        views = SimpleNamespace(in_herdr=lambda: True, HERDR_STATE="herdr.json",
                                pipeline=lambda *a, **k: seen.update(k) or 0, summary=lambda *a, **k: seen.update(k) or 0)
        with mock.patch.object(orchestrate, "watch_module", return_value=views):
            self.assertEqual(orchestrate.main(["watch", str(orch.folder), "--here"]), 0)
            self.assertIsNone(seen["closer"])
            self.assertEqual(orchestrate.main(["watch", str(self.wave.run), "--here"]), 0)
            self.assertIsNone(seen["closer"])


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

    def test_watch_feed_defaults_to_messages_and_can_show_everything(self):
        self.assertEqual(orchestrate.load_policy(self.policy)["watchFeed"], "messages")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(orchestrate.main(["policy", "set", "--watch-feed", "all", "--file", str(self.policy)]), 0)
        self.assertEqual(orchestrate.load_policy(self.policy)["watchFeed"], "all")
        with self.assertRaises(orchestrate.PlanError):
            orchestrate.validate_policy({"watchFeed": "verbose"})

    def test_watch_close_after_defaults_to_two_minutes_and_can_be_set(self):
        self.assertEqual(orchestrate.load_policy(self.policy)["watchCloseAfterSeconds"], 120)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(orchestrate.main(["policy", "set", "--watch-close-after", "0", "--file", str(self.policy)]), 0)
        self.assertEqual(orchestrate.load_policy(self.policy)["watchCloseAfterSeconds"], 0)
        for bad in (-1, "120", True):
            with self.assertRaises(orchestrate.PlanError):
                orchestrate.validate_policy({"watchCloseAfterSeconds": bad})

    def test_auto_watch_only_opens_when_on_and_never_raises(self):
        wave = Wave(Path(self.tmp.name))
        self.assertIsNone(orchestrate.auto_watch(wave.run, self.policy))
        orchestrate.save_policy(self.policy, {"watch": "on"})
        with mock.patch.dict(os.environ, {"HERDR_ENV": ""}):
            self.assertIsNone(orchestrate.auto_watch(wave.run, self.policy))
        orch = Orchestration(Path(self.tmp.name))
        orch_wave = orch.wave("01-build", "build", ["builder"])
        views = SimpleNamespace(in_herdr=lambda: True, open_orchestration=mock.Mock(return_value={"tab": "t"}))
        with mock.patch.object(orchestrate, "watch_module", return_value=views):
            self.assertEqual(orchestrate.auto_watch(orch_wave, self.policy), {"tab": "t"})
        views.open_orchestration.assert_called_once_with(orch.folder, mock.ANY)
        broken = SimpleNamespace(in_herdr=lambda: True, open_herdr=mock.Mock(side_effect=RuntimeError("no server")))
        with mock.patch.object(orchestrate, "watch_module", return_value=broken):
            self.assertEqual(orchestrate.auto_watch(wave.run, self.policy), {"error": "no server"})


if __name__ == "__main__":
    unittest.main()
