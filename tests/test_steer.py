import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "alethe" / "scripts" / "orchestrate.py"
FAKE = Path(__file__).resolve().parent / "fake_cli.py"
spec = importlib.util.spec_from_file_location("orchestrate_steer", SCRIPT)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


class SteerFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "init")
        previous = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)
        self.policy = self.root / "policy.json"
        environment = mock.patch.dict(os.environ, {
            "ALETHE_ORCHESTRATE_POLICY": str(self.policy),
            "ALETHE_ORCHESTRATE_HOME": str(self.root / "home"),
            "ALETHE_ORCHESTRATE_PLAYBOOKS": str(self.root / "playbooks.yaml"),
            "CODEX_HOME": str(self.root / "codex"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.folder = self.root / "orch"
        self.folder.mkdir()
        (self.folder / r.ORCHESTRATION_FILE).write_text(json.dumps({"title": "Feature X", "playbook": "complete"}),
                                                       encoding="utf-8")

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = r.main([str(arg) for arg in args])
        return code, out.getvalue(), err.getvalue()

    def finished_wave(self, provider="claude", session="sess-1", cwd=None, permissions="network", status="succeeded"):
        wave = self.folder / "01-build"
        (wave / "workers" / "builder").mkdir(parents=True)
        (wave / "plan.json").write_text(json.dumps({
            "title": "Feature X", "playbook": "complete", "phase": "build",
            "workers": [{"id": "builder", "provider": provider, "model": "sonnet", "effort": "high", "mode": "write",
                         "permissions": permissions, "timeoutSeconds": 1800, "isolate": True}],
        }), encoding="utf-8")
        (wave / "state.json").write_text(json.dumps({"status": status, "workers": {"builder": {"status": status}}}),
                                         encoding="utf-8")
        (wave / "workers" / "builder" / "outcome.json").write_text(json.dumps({
            "id": "builder", "status": status, "finishedAt": "2026-11-13T10:00:00+00:00", "sessionId": session,
            "cwd": str(cwd or self.repo), "originCwd": str(self.repo),
        }), encoding="utf-8")
        return wave


class StopOneWorkerTests(SteerFixtures):
    def worker(self, worker_id, args):
        return {"id": worker_id, "provider": "custom", "mode": "write", "cwd": str(self.repo),
                "command": sys.executable, "args": [str(FAKE), *args], "outputFormat": "text",
                "task": f"Task for {worker_id}."}

    def test_stopping_one_worker_leaves_the_rest_of_the_wave_running(self):
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"version": 1, "maxConcurrent": 2, "strategy": "balanced", "workers": [
            self.worker("slow", ["sleep", "60"]), self.worker("quick", ["sleep", "3"])]}), encoding="utf-8")
        run_dir = self.folder / "01-scout"
        process = subprocess.Popen([sys.executable, str(SCRIPT), "run", str(plan), "--run-dir", str(run_dir),
                                    "--policy", str(self.policy)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env={**os.environ, "PYTHONPATH": ""})

        def finish():
            if process.poll() is None:
                (run_dir / "stop").write_text("stop\n", encoding="utf-8")
                try:
                    process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

        self.addCleanup(finish)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = r.read_optional_json(run_dir / "state.json")
            if isinstance(state, dict) and state["workers"].get("slow", {}).get("status") == "running":
                break
            time.sleep(0.2)
        else:
            self.fail(process.stderr.read().decode() if process.poll() is not None else "slow never started")
        r.stop_worker(self.folder, "slow", steer="only the API")
        process.communicate(timeout=40)
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["workers"]["slow"]["status"], "stopped")
        self.assertEqual(state["workers"]["slow"]["failureKind"], "steered")
        self.assertLess(state["workers"]["slow"]["durationSeconds"], 30)
        self.assertEqual(state["workers"]["quick"]["status"], "succeeded")
        self.assertFalse((run_dir / "stop").exists())
        self.assertFalse(r.stop_worker(self.folder, "slow")["stopped"])


class SteerTests(SteerFixtures):
    def steer(self, message="Only touch the API; leave the UI alone.", **wave):
        self.finished_wave(**wave)
        started = {"status": "started", "runDir": "x", "pid": 1}
        with mock.patch.object(r, "detach_run", return_value=started) as detach:
            payload = r.steer_payload(self.folder, "builder", message, self.policy, wait=1)
        plan_path, run_dir = detach.call_args[0][:2]
        return payload, json.loads(Path(plan_path).read_text(encoding="utf-8")), run_dir

    def test_steer_resumes_the_same_session_in_the_same_place_with_the_instruction(self):
        payload, plan, run_dir = self.steer()
        worker = plan["workers"][0]
        self.assertEqual(plan["strategy"], "rescue")
        self.assertEqual((plan["playbook"], plan["phase"]), ("complete", "build"))
        self.assertEqual((worker["resume"], worker["cwd"], worker["provider"]), ("sess-1", str(self.repo), "claude"))
        self.assertEqual((worker["model"], worker["effort"], worker["permissions"]), ("sonnet", "high", "network"))
        self.assertNotIn("isolate", worker)
        self.assertIn("Only touch the API; leave the UI alone.", worker["task"])
        self.assertEqual(run_dir, self.folder / "02-build")
        self.assertFalse(payload["wasRunning"])
        journal = (self.folder / r.JOURNAL_FILE).read_text(encoding="utf-8")
        self.assertIn('"kind": "steer"', journal)
        r.validate_plan(plan, self.folder / "plan.json", r.load_policy(self.policy), policy_path=self.policy,
                        orchestration=self.folder)

    def test_steer_refuses_what_it_cannot_resume(self):
        with self.assertRaisesRegex(r.PlanError, "before its CLI reported a session"):
            self.steer(session=None)
        for args, pattern in ((("builder", ""), "needs the instruction"), (("../x", "go"), "not safe"),
                              (("ghost", "go"), "no wave")):
            with self.subTest(args=args), self.assertRaisesRegex(r.PlanError, pattern):
                r.steer_payload(self.folder, *args, self.policy, wait=1)

    def test_custom_workers_cannot_be_steered(self):
        self.finished_wave(provider="custom")
        with self.assertRaisesRegex(r.PlanError, "cannot resume"):
            r.steer_payload(self.folder, "builder", "go", self.policy, wait=1)

    def test_steer_from_the_command_line(self):
        self.finished_wave()
        with mock.patch.object(r, "detach_run", return_value={"status": "started"}):
            code, out, err = self.call("steer", "builder", "Use the new endpoint.", "--in", self.folder, "--policy", self.policy)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["sessionId"], "sess-1")


class OwnWorktreeTests(SteerFixtures):
    def resolve(self, cwd):
        r.save_policy(self.policy, {"trust": {"maxPermissions": {"claude": "full"}}})
        worker = {"id": "builder", "provider": "claude", "mode": "write", "cwd": str(cwd), "resume": "sess-1",
                  "permissions": "full", "task": "Continue."}
        return r.validate_plan({"version": 1, "workers": [worker]}, self.root / "plan.json",
                               r.load_policy(self.policy), policy_path=self.policy)["workers"][0]

    def test_full_may_resume_only_inside_its_own_alethe_worktree(self):
        own = self.repo / ".alethe" / "worktrees" / "skill-abc-builder"
        own.parent.mkdir(parents=True)
        git(self.repo, "worktree", "add", "-q", "--detach", str(own), "HEAD")
        worker = self.resolve(own)
        self.assertEqual((worker["permissions"], worker["isolate"]), ("full", False))
        with self.assertRaises(r.PlanError):
            self.resolve(self.repo)
        fake = self.repo / ".alethe" / "worktrees" / "skill-fake-builder"
        fake.mkdir()
        (fake / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n", encoding="utf-8")
        self.assertFalse(r.alethe_worktree(fake))
        with self.assertRaises(r.PlanError):
            self.resolve(fake)


if __name__ == "__main__":
    unittest.main()
