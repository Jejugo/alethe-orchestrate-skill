import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "alethe"
spec = importlib.util.spec_from_file_location("orchestrate_only", SKILL / "scripts" / "orchestrate.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

CLAUDE_ONLY = """  claude-only:
    name: Claude only
    summary: Every worker on Claude Code; haiku, sonnet and opus by tier.
    order: [claude, codex]
    only: [claude]
"""


class ProfileOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.policy = self.root / "policy.json"
        self.books = self.root / "playbooks.yaml"
        environment = mock.patch.dict(os.environ, {
            "ALETHE_ORCHESTRATE_POLICY": str(self.policy),
            "ALETHE_ORCHESTRATE_PLAYBOOKS": str(self.books),
            "ALETHE_ORCHESTRATE_HOME": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "codex"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        installed = mock.patch.object(r, "available_builtin_providers", return_value=["codex", "claude", "agy"])
        installed.start()
        self.addCleanup(installed.stop)

    def write_books(self, profile_block=CLAUDE_ONLY, active="claude-only"):
        text = (SKILL / "playbooks.yaml").read_text(encoding="utf-8")
        text = text.replace("profiles:\n", "profiles:\n" + profile_block, 1)
        text = text.replace("\nprofile: base\n", f"\nprofile: {active}\n", 1)
        self.books.write_text(text, encoding="utf-8")

    def resolve(self, **fields):
        worker = {"id": "one", "provider": "auto", "mode": "write", "cwd": str(self.root), "task": "Do it.", **fields}
        return r.validate_plan({"version": 1, "workers": [worker]}, self.root / "plan.json",
                               r.load_policy(self.policy), policy_path=self.policy)["workers"][0]

    def test_auto_workers_land_on_the_only_provider_at_their_tier(self):
        self.write_books()
        policy = r.load_policy(self.policy)
        policy["providerOrder"] = ["codex", "claude", "agy", "kimi"]
        r.save_policy(self.policy, policy)
        worker = self.resolve(tier="cheap")
        self.assertEqual(worker["provider"], "claude")
        self.assertEqual(worker["model"], "haiku")
        self.assertEqual(self.resolve(tier="strong", mode="read-only")["provider"], "claude")

    def test_a_pin_outside_the_profile_is_refused(self):
        self.write_books()
        with self.assertRaisesRegex(r.PlanError, "runs only on claude"):
            self.resolve(provider="codex")
        self.assertEqual(self.resolve(provider="claude")["provider"], "claude")

    def test_profiles_without_only_are_unchanged(self):
        self.write_books(active="base")
        self.assertEqual(self.resolve(provider="codex")["provider"], "codex")

    def test_only_must_name_known_providers(self):
        self.write_books(CLAUDE_ONLY.replace("only: [claude]", "only: [gemini]"))
        problems = r.playbook_structure_errors(r.parse_playbooks_yaml(self.books.read_text(encoding="utf-8")))
        self.assertTrue(any("only" in problem for problem in problems), problems)


if __name__ == "__main__":
    unittest.main()
