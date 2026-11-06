import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "alethe" / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


orchestrate = load("orchestrate")
jev = load("jev")

KEY = "sk-or-test-secret"


def answers(kind="code", kind_p=0.9, narrow=0.9, effort=None, risks=None):
    effort = effort or {"0": 0.8, "1": 0.15, "2": 0.05}
    risks = risks or {}
    result = {
        "kind": {"type": "choice", "choice": kind, "probabilities": {kind: kind_p, "docs": 1 - kind_p}, "confidence": 0.8},
        "narrow": {"type": "noul", "noul": narrow},
        "effort": {"type": "score", "score": 0.3, "probabilities": effort, "confidence": 0.7},
    }
    for name in jev.RISKS:
        result[f"risk_{name}"] = {"type": "noul", "noul": risks.get(name, 0.05)}
    return result


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode("utf-8"))


def http_error(code, body=b'{"error": {"message": "nope"}}', headers=None):
    return urllib.error.HTTPError("https://example.test", code, "error", headers or {}, io.BytesIO(body))


class InterpretTests(unittest.TestCase):
    def test_confident_answers_become_playbook_conditions(self):
        result = jev.interpret(answers())
        self.assertEqual(result["conditions"], {"kind": "code", "narrow": True, "risky": False, "effort": "light"})
        self.assertEqual(result["uncertain"], [])

    def test_one_likely_risk_makes_the_unit_risky(self):
        result = jev.interpret(answers(risks={"schema": 0.92}))
        self.assertIs(result["conditions"]["risky"], True)
        self.assertAlmostEqual(result["probabilities"]["risks"]["schema"], 0.92)

    def test_middle_band_answers_are_left_to_the_lead(self):
        result = jev.interpret(answers(kind_p=0.5, narrow=0.5, effort={"0": 0.4, "1": 0.35, "2": 0.25},
                                       risks={"auth": 0.5}))
        self.assertEqual(result["conditions"], {"kind": None, "narrow": None, "risky": None, "effort": None})
        self.assertEqual(sorted(result["uncertain"]), ["effort", "kind", "narrow", "risky"])

    def test_refused_questions_are_listed_and_uncertain(self):
        raw = answers()
        raw["narrow"] = {"type": "refusal"}
        result = jev.interpret(raw)
        self.assertEqual(result["refused"], ["narrow"])
        self.assertIsNone(result["conditions"]["narrow"])


class RequestTests(unittest.TestCase):
    def test_request_carries_model_state_questions_and_bearer_key(self):
        opener = FakeOpener({"id": "gen-dec-1", "model": "typesafe/jev-1.14", "answers": answers(),
                             "usage": {"input_tokens": 900, "output_tokens": 0, "cost": 0.00004}})
        result = jev.classify("Rename the helper in utils.py", KEY, opener=opener)
        request = opener.requests[0]
        self.assertEqual(request.full_url, jev.DEFAULT_ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {KEY}")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "~typesafe/jev-latest")
        self.assertEqual(body["state"], {"unit": "Rename the helper in utils.py"})
        self.assertEqual(set(body["questions"]), {"kind", "narrow", "effort", *(f"risk_{n}" for n in jev.RISKS)})
        self.assertEqual(result["usage"]["cost"], 0.00004)
        self.assertEqual(result["model"], "typesafe/jev-1.14")
        self.assertNotIn("Rename", json.dumps(result["task"]))

    def test_overload_is_retried_with_backoff(self):
        sleeps = []
        opener = FakeOpener(http_error(429, headers={"Retry-After": "2"}), http_error(529),
                            {"answers": answers()})
        jev.classify("Fix the typo", KEY, opener=opener, sleep=sleeps.append)
        self.assertEqual(sleeps, [2.0, 3.0])
        self.assertEqual(len(opener.requests), 3)

    def test_errors_never_echo_the_key(self):
        with self.assertRaises(jev.JevError) as caught:
            jev.classify("Fix the typo", KEY, opener=FakeOpener(http_error(401)))
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = Path(self.tmp.name) / "orchestrate.json"
        self.addCleanup(self.tmp.cleanup)

    def test_missing_key_names_the_file(self):
        with self.assertRaises(jev.JevError) as caught:
            jev.read_key(self.policy)
        self.assertIn(str(self.policy.with_name("jev.key")), str(caught.exception))

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_key_readable_by_others_is_refused(self):
        path = self.policy.with_name("jev.key")
        path.write_text(KEY + "\n", encoding="utf-8")
        path.chmod(0o644)
        with self.assertRaises(jev.JevError):
            jev.read_key(self.policy)
        path.chmod(0o600)
        self.assertEqual(jev.read_key(self.policy), KEY)


class ClassifyCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.policy = self.root / "orchestrate.json"
        environment = mock.patch.dict(os.environ, {
            "ALETHE_ORCHESTRATE_POLICY": str(self.policy),
            "ALETHE_ORCHESTRATE_HOME": str(self.root / "orchestrations"),
            "ALETHE_SESSION": "jev-tests",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = orchestrate.main([str(arg) for arg in args])
        return code, out.getvalue(), err.getvalue()

    def enable(self, mode="shadow"):
        self.assertEqual(self.call("policy", "set", "--classifier", mode, "--file", self.policy)[0], 0)
        key = self.policy.with_name("jev.key")
        key.write_text(KEY, encoding="utf-8")
        key.chmod(0o600)

    def test_classifier_is_off_until_the_user_turns_it_on(self):
        code, _, err = self.call("classify", "--task", "Fix the typo", "--policy", self.policy)
        self.assertEqual(code, 2)
        self.assertIn("classifier is off", err)

    def test_switching_jev_alone_keeps_onboarding_pending(self):
        self.assertEqual(self.call("policy", "set", "--classifier", "on", "--file", self.policy)[0], 0)
        self.assertFalse(orchestrate.load_policy(self.policy)["onboarded"])
        self.assertEqual(self.call("policy", "set", "--max-calls", "4", "--file", self.policy)[0], 0)
        policy = orchestrate.load_policy(self.policy)
        self.assertTrue(policy["onboarded"])
        self.assertEqual(policy["classifier"]["mode"], "on")

    def test_policy_rejects_a_plain_http_endpoint(self):
        self.policy.write_text(json.dumps({"classifier": {"endpoint": "http://example.test"}}), encoding="utf-8")
        with self.assertRaises(orchestrate.PlanError):
            orchestrate.load_policy(self.policy)

    def test_plan_units_are_classified_and_logged_without_task_text(self):
        self.enable()
        folder = self.root / "orchestrations" / "demo"
        folder.mkdir(parents=True)
        orchestrate.set_current(folder)
        (self.root / "brief.md").write_text("Add a migration that drops the legacy table", encoding="utf-8")
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"version": 1, "workers": [
            {"id": "typo", "provider": "codex", "task": "Fix the typo in README"},
            {"id": "drop", "provider": "codex", "taskFile": "brief.md"},
        ]}), encoding="utf-8")
        opener = FakeOpener({"answers": answers(), "usage": {"cost": 0.00001}},
                            {"answers": answers(risks={"schema": 0.95, "deletes_data": 0.9}), "usage": {"cost": 0.00002}})
        with mock.patch("urllib.request.urlopen", opener):
            code, out, err = self.call("classify", "--plan", plan, "--policy", self.policy)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual([unit["unit"] for unit in payload["units"]], ["typo", "drop"])
        self.assertEqual([unit["conditions"]["risky"] for unit in payload["units"]], [False, True])
        self.assertAlmostEqual(payload["reportedCost"], 0.00003)
        sent = json.loads(opener.requests[1].data)["state"]["unit"]
        self.assertEqual(sent, "Add a migration that drops the legacy table")
        log = (folder / "routing.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(log.splitlines()), 2)
        self.assertNotIn("legacy table", log)
        self.assertNotIn(KEY, log + out)

    def test_doctor_reports_the_missing_key(self):
        self.assertEqual(self.call("policy", "set", "--classifier", "shadow", "--file", self.policy)[0], 0)
        payload = orchestrate.doctor_payload(self.policy)
        self.assertEqual(payload["classifier"]["mode"], "shadow")
        self.assertFalse(payload["classifier"]["key"]["present"])
        self.assertTrue(any("no key" in hint for hint in payload["hints"]))

    def test_workers_never_inherit_an_openrouter_key(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": KEY}):
            for provider in ("claude", "codex", "agy", "kimi", "custom"):
                self.assertNotIn("OPENROUTER_API_KEY", orchestrate.worker_environment(provider))


if __name__ == "__main__":
    unittest.main()
