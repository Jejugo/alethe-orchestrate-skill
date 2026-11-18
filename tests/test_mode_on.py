import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "alethe" / "scripts" / "orchestrate.py"
spec = importlib.util.spec_from_file_location("orchestrate_mode_on", SCRIPT)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class ModeOnFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = self.root / "claude"
        self.config.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.settings = self.config / "settings.json"
        self.env = {
            "CLAUDE_CONFIG_DIR": str(self.config),
            "ALETHE_ORCHESTRATE_POLICY": str(self.root / "policy.json"),
            "ALETHE_ORCHESTRATE_HOME": str(self.root / "home"),
            "ALETHE_ORCHESTRATE_PLAYBOOKS": str(self.root / "playbooks.yaml"),
            "CODEX_HOME": str(self.root / "codex"),
        }
        environment = mock.patch.dict(os.environ, self.env)
        environment.start()
        self.addCleanup(environment.stop)

    def call(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = r.main([str(arg) for arg in args])
        return code, out.getvalue()


class ReminderTests(ModeOnFixtures):
    def test_a_project_mode_decides_each_request_and_starts_without_waiting(self):
        text = r.mode_reminder({"goal": "balanced"}, everywhere=False)
        self.assertIn("Before your first edit on each request", text)
        self.assertIn("say why in one line", text)
        self.assertIn("start without waiting for an ok", text)
        self.assertIn("API-billed", text)
        self.assertNotIn("do it yourself when that is cheaper", text)

    def test_everywhere_still_waits_for_an_ok(self):
        text = r.mode_reminder({"goal": "balanced"}, everywhere=True)
        self.assertIn("wait for the user's ok", text)
        self.assertNotIn("start without waiting", text)

    def test_freestyle_never_asks_to_orchestrate(self):
        text = r.mode_reminder({"goal": "freestyle"}, everywhere=False)
        self.assertNotIn("Before your first edit", text)
        self.assertNotIn("start without waiting", text)


class HookInstallTests(ModeOnFixtures):
    def test_install_keeps_other_settings_and_runs_once(self):
        other = {"hooks": [{"type": "command", "command": "other-tool"}]}
        self.settings.write_text(json.dumps({"model": "opus", "hooks": {"UserPromptSubmit": [other]}}), encoding="utf-8")
        self.assertTrue(r.install_claude_hook()["changed"])
        saved = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(saved["model"], "opus")
        self.assertEqual(saved["hooks"]["UserPromptSubmit"][0], other)
        self.assertEqual((len(saved["hooks"]["UserPromptSubmit"]), len(saved["hooks"]["PostToolUse"])), (2, 1))
        self.assertFalse(r.install_claude_hook()["changed"])
        self.assertEqual(len(json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]), 2)
        self.assertIsNone(r.hook_warning())

    @unittest.skipUnless(os.name == "posix", "needs symlinks and file modes")
    def test_install_keeps_a_symlinked_private_settings_file(self):
        real = self.root / "dotfiles.json"
        real.write_text("{}", encoding="utf-8")
        real.chmod(0o600)
        self.settings.symlink_to(real)
        r.install_claude_hook()
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)
        self.assertIn("PostToolUse", json.loads(real.read_text(encoding="utf-8"))["hooks"])

    def test_install_refuses_settings_it_cannot_read(self):
        self.settings.write_text("{broken", encoding="utf-8")
        with self.assertRaises(r.PlanError):
            r.install_claude_hook()
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{broken")

    def test_the_plugin_counts_unless_it_is_disabled(self):
        plugins = self.config / "plugins" / "installed_plugins.json"
        plugins.parent.mkdir()
        plugins.write_text(json.dumps({"plugins": {"alethe-orchestrate@alethe": []}}), encoding="utf-8")
        self.assertIsNone(r.hook_warning())
        self.settings.write_text(json.dumps({"enabledPlugins": {"alethe-orchestrate@alethe": False}}), encoding="utf-8")
        self.assertIn("hook --install", r.hook_warning())

    def test_mode_on_and_doctor_warn_only_when_claude_code_lacks_the_hook(self):
        code, out = self.call("mode", "on", "--cwd", self.project)
        self.assertEqual(code, 0)
        self.assertIn("hook --install", json.loads(out)["warning"])
        self.assertIn(r.hook_warning(), r.doctor_payload(self.root / "policy.json")["hints"])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "no-claude")}):
            self.assertNotIn("warning", json.loads(self.call("mode", "on", "--cwd", self.project)[1]))

    @unittest.skipUnless(os.name == "posix", "the hook command is run through sh")
    def test_the_installed_command_prints_the_reminder(self):
        self.assertEqual(self.call("hook", "--install")[0], 0)
        self.call("mode", "on", "--cwd", self.project)
        command = json.loads(self.settings.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        event = json.dumps({"hook_event_name": "UserPromptSubmit", "cwd": str(self.project), "session_id": "s"})
        done = subprocess.run(["sh", "-c", command], input=event, capture_output=True, text=True, timeout=30,
                              env={**os.environ, **self.env})
        self.assertIn("Before your first edit on each request", done.stdout)


if __name__ == "__main__":
    unittest.main()
