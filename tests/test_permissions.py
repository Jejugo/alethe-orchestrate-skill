import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "alethe" / "scripts" / "orchestrate.py"
spec = importlib.util.spec_from_file_location("orchestrate_permissions", SCRIPT)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

HELP = ("--no-daemon --disable --disallowedTools --allowedTools --strict-mcp-config --permission-prompts "
        "--setting-sources --disable-slash-commands --add-dir --dangerously-skip-permissions "
        "--dangerously-bypass-approvals-and-sandbox")


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
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
        self.help = HELP

    def ceilings(self, default="standard", **levels):
        r.save_policy(self.policy, {"trust": {"maxPermissions": levels, "defaultPermissions": default}})
        return r.load_policy(self.policy)

    def worker(self, provider="codex", **fields):
        return {"id": "one", "provider": provider, "mode": "write", "cwd": str(self.repo),
                "task": "Install the dependencies and run the tests.", **fields}

    def resolve(self, worker, policy=None):
        return r.validate_plan({"version": 1, "workers": [worker]}, self.root / "plan.json",
                               policy or r.load_policy(self.policy), policy_path=self.policy)

    def invocation(self, worker, policy=None):
        resolved = self.resolve(worker, policy)["workers"][0]
        output = lambda args, cache, lock: ("multi_agent stable true\n" if args[-2:] == ["features", "list"]
                                            else self.help)
        with mock.patch.object(r, "resolve_executable", side_effect=lambda name: name), \
                mock.patch.object(r, "launch_command", side_effect=lambda name: [name]), \
                mock.patch.object(r, "command_output", side_effect=output):
            return r.build_invocation(resolved, self.repo, self.root, {}, threading.Lock(), 30)[0]

    def test_standard_is_the_default_and_unchanged(self):
        codex = self.invocation(self.worker())
        self.assertEqual(codex[:2], ["codex", "--no-daemon"])
        self.assertEqual(codex[codex.index("exec"):][:3], ["exec", "--sandbox", "workspace-write"])
        self.assertIn("multi_agent", codex)
        claude = self.invocation(self.worker("claude"))
        self.assertIn("acceptEdits", claude)
        self.assertNotIn("--dangerously-skip-permissions", claude)

    def test_without_a_ceiling_the_worker_runs_at_standard_with_a_warning(self):
        plan = self.resolve(self.worker(permissions="full"))
        worker = plan["workers"][0]
        self.assertEqual((worker["requestedPermissions"], worker["permissions"]), ("full", "standard"))
        self.assertFalse(worker["isolate"])
        self.assertTrue(any("only the user can raise it" in warning.lower() for warning in r.plan_warnings(plan)))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", self.invocation(self.worker(permissions="full")))

    def test_the_lower_of_request_and_ceiling_wins(self):
        policy = self.ceilings(codex="network")
        self.assertEqual(self.resolve(self.worker(permissions="full"), policy)["workers"][0]["permissions"], "network")
        policy = self.ceilings(codex="full")
        self.assertEqual(self.resolve(self.worker(permissions="network"), policy)["workers"][0]["permissions"], "network")

    def test_full_uses_each_cli_bypass_flag_and_isolates(self):
        policy = self.ceilings(codex="full", claude="full", agy="full")
        codex = self.invocation(self.worker(permissions="full"), policy)
        self.assertEqual(codex[codex.index("exec"):][:2], ["exec", "--dangerously-bypass-approvals-and-sandbox"])
        self.assertIn("multi_agent", codex)
        self.assertNotIn("--sandbox", codex)
        claude = self.invocation(self.worker("claude", permissions="full"), policy)
        self.assertIn("--dangerously-skip-permissions", claude)
        self.assertNotIn("--permission-mode", claude)
        self.assertIn("Agent,Workflow", claude)
        agy = self.invocation(self.worker("agy", permissions="full"), policy)
        self.assertIn("--dangerously-skip-permissions", agy)
        self.assertNotIn("--mode", agy)
        self.assertTrue(self.resolve(self.worker(permissions="full"), policy)["workers"][0]["isolate"])

    def test_network_opens_the_network_but_keeps_the_sandbox(self):
        policy = self.ceilings(codex="network", claude="network", agy="network")
        codex = self.invocation(self.worker(permissions="network"), policy)
        self.assertIn("workspace-write", codex)
        self.assertIn("sandbox_workspace_write.network_access=true", codex)
        claude = self.invocation(self.worker("claude", permissions="network"), policy)
        self.assertIn("acceptEdits", claude)
        self.assertEqual(claude[claude.index("--allowedTools") + 1], "Bash,WebFetch,WebSearch")
        agy = self.invocation(self.worker("agy", permissions="network"), policy)
        self.assertEqual(agy[agy.index("--mode") + 1], "auto")

    def test_missing_bypass_flag_refuses_instead_of_downgrading_silently(self):
        policy = self.ceilings(claude="full")
        self.help = HELP.replace("--dangerously-skip-permissions", "")
        with self.assertRaises(r.PlanError):
            self.invocation(self.worker("claude", permissions="full"), policy)

    def test_invalid_combinations_are_refused(self):
        policy = self.ceilings(codex="full", claude="full")
        cases = [
            self.worker(permissions="root"),
            self.worker(permissions="full", mode="read-only"),
            self.worker(permissions="full", isolate=False),
            self.worker(permissions="full", resume="session-1"),
            self.worker("kimi", permissions="network"),
        ]
        for worker in cases:
            with self.subTest(worker=worker), self.assertRaises(r.PlanError):
                self.resolve(worker, policy)
        outside = self.root / "plain"
        outside.mkdir()
        with mock.patch.object(r, "in_git_worktree", return_value=False), self.assertRaises(r.PlanError):
            self.resolve({**self.worker(permissions="full"), "cwd": str(self.repo)}, policy)

    def test_full_workers_still_lose_unrelated_secrets(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "secret", "OPENAI_API_KEY": "codex-own"}):
            environment = r.worker_environment("codex", self.ceilings(codex="full")["trust"])
        self.assertNotIn("OPENROUTER_API_KEY", environment)
        self.assertIn("OPENAI_API_KEY", environment)

    def test_policy_set_records_ceilings(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = r.main(["policy", "set", "--max-permissions", "codex=full", "claude=network", "--file", str(self.policy)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["policy"]["trust"]["maxPermissions"],
                         {"codex": "full", "claude": "network"})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(r.main(["policy", "set", "--max-permissions", "kimi=full", "--file", str(self.policy)]), 2)

    def test_saved_default_full_applies_to_writers_that_do_not_ask(self):
        policy = self.ceilings("full", codex="full", claude="full", agy="full")
        worker = self.resolve(self.worker(), policy)["workers"][0]
        self.assertEqual((worker["permissions"], worker["permissionsSource"], worker["isolate"]), ("full", "default", True))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", self.invocation(self.worker(), policy))
        reader = self.resolve(self.worker(mode="read-only"), policy)["workers"][0]
        self.assertEqual(reader["permissions"], "standard")
        kimi = self.resolve(self.worker("kimi", isolate=True), policy)["workers"][0]
        self.assertEqual(kimi["permissions"], "standard")

    def test_asking_lower_wins_over_the_default(self):
        policy = self.ceilings("full", codex="full")
        worker = self.resolve(self.worker(permissions="standard"), policy)["workers"][0]
        self.assertEqual((worker["permissions"], worker["isolate"]), ("standard", False))
        self.assertIn("workspace-write", self.invocation(self.worker(permissions="standard"), policy))

    def test_default_full_steps_down_when_it_cannot_isolate(self):
        policy = self.ceilings("full", codex="full")
        for fields in ({"resume": "session-1"}, {"isolate": False}):
            with self.subTest(fields=fields):
                plan = self.resolve(self.worker(**fields), policy)
                worker = plan["workers"][0]
                self.assertEqual(worker["permissions"], "network")
                self.assertTrue(any("stepped down" in warning for warning in r.plan_warnings(plan)))
        with mock.patch.object(r, "in_git_worktree", return_value=False):
            self.assertEqual(self.resolve(self.worker(), policy)["workers"][0]["permissions"], "network")

    def test_default_capped_by_a_lower_ceiling_does_not_nag(self):
        policy = self.ceilings("full", codex="network")
        plan = self.resolve(self.worker(), policy)
        self.assertEqual(plan["workers"][0]["permissions"], "network")
        self.assertFalse(any("only the user can raise" in warning.lower() for warning in r.plan_warnings(plan)))

    def test_trust_settings_alone_keep_onboarding_pending(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(r.main(["policy", "set", "--default-permissions", "full", "--max-permissions",
                                     "codex=full", "--file", str(self.policy)]), 0)
        policy = r.load_policy(self.policy)
        self.assertFalse(policy["onboarded"])
        self.assertEqual(policy["trust"]["defaultPermissions"], "full")

    def test_report_mode_shows_elevated_permissions(self):
        self.assertEqual(r.mode_text({"mode": "write", "isolate": True, "permissions": "full"}), "write, worktree, full")
        self.assertEqual(r.mode_text({"mode": "write", "isolate": False}), "write")


if __name__ == "__main__":
    unittest.main()
