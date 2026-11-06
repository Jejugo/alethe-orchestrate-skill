"""Optional Jev classifier over OpenRouter's decisions API.

This is the only module that touches the network. The runner imports it only for the
`classify` command, and only after the user turned the classifier on in the saved policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "~typesafe/jev-latest"
KEY_FILE = "jev.key"
TIMEOUT_SECONDS = 30
RETRY_STATUSES = {429, 500, 502, 503, 524, 529}
RETRY_DELAYS = (1.0, 3.0)
MAX_TASK_CHARS = 60_000
# A yes/no answer counts only outside the middle band; inside it the lead decides.
YES_AT, NO_AT = 0.7, 0.3
CHOICE_AT = 0.6

KINDS = {
    "research": "Read and investigate code, docs or sources and report findings; it changes no files",
    "code": "Write or change source code, tests or configuration files",
    "review": "Review an existing diff, change or result and report findings",
    "command": "Run a command or tool, such as tests, a build or a script, and report its output",
    "scrap": "Throwaway or exploratory work whose output will not be kept, such as a spike or prototype",
    "docs": "Write or change documentation, comments or prose",
}
EFFORTS = ("light", "standard", "deep")
RISKS = {
    "api_contract": "changes a public or shared API contract: endpoints, request or response shapes, exported signatures",
    "schema": "changes a database schema or adds or runs a migration",
    "auth": "changes authentication or authorization logic",
    "secrets": "changes cryptography, key handling or secrets",
    "deletes_data": "deletes or irreversibly overwrites data, records or files that matter",
}


class JevError(RuntimeError):
    pass


def key_path(policy_path: Path) -> Path:
    configured = os.environ.get("ALETHE_JEV_KEY_FILE")
    if configured:
        return Path(configured).expanduser()
    return policy_path.with_name(KEY_FILE)


def key_status(policy_path: Path) -> Dict[str, Any]:
    path = key_path(policy_path)
    status: Dict[str, Any] = {"path": str(path), "present": path.is_file(), "private": None}
    if status["present"] and os.name != "nt":
        status["private"] = not (path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))
    return status


def read_key(policy_path: Path) -> str:
    status = key_status(policy_path)
    path = status["path"]
    if not status["present"]:
        raise JevError(f"no Jev key: put your OpenRouter API key in {path}")
    if status["private"] is False:
        raise JevError(f"{path} is readable by other users; run chmod 600 on it")
    key = Path(path).read_text(encoding="utf-8").strip()
    if not key or any(character.isspace() for character in key):
        raise JevError(f"{path} must hold only the OpenRouter API key")
    return key


def unit_questions() -> Dict[str, Any]:
    questions: Dict[str, Any] = {
        "kind": {
            "type": "choice",
            "instructions": "What kind of work does the unit in `unit` ask a coding agent to do?",
            "criteria": KINDS,
        },
        "narrow": {
            "type": "noul",
            "instructions": (
                "Is the unit in `unit` small and clear: a well-defined change or question that touches "
                "up to about 3 files and needs no design decisions?"
            ),
        },
        "effort": {
            "type": "score",
            "instructions": "How much reasoning effort does the unit in `unit` need from a coding agent?",
            "criteria": [
                "Light: mechanical or lookup work with an obvious approach",
                "Standard: ordinary engineering work that needs some reading and judgment",
                "Deep: hard, ambiguous or cross-cutting work that needs careful design or debugging",
            ],
        },
    }
    for name, operation in RISKS.items():
        questions[f"risk_{name}"] = {
            "type": "noul",
            "instructions": (
                f"Does carrying out the unit in `unit` {operation}? Judge by the operation the unit "
                "performs, not by words it merely mentions; reading or reviewing such code is not changing it."
            ),
        }
    return questions


def post(endpoint: str, key: str, body: Dict[str, Any], opener: Optional[Callable[..., Any]] = None,
         sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    opener = opener or urllib.request.urlopen
    data = json.dumps(body).encode("utf-8")
    for attempt in range(len(RETRY_DELAYS) + 1):
        request = urllib.request.Request(endpoint, data=data, method="POST", headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "alethe-orchestrate",
        })
        try:
            with opener(request, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay = min(float(retry_after), 10.0) if retry_after else RETRY_DELAYS[attempt]
                except ValueError:
                    delay = RETRY_DELAYS[attempt]
                sleep(delay)
                continue
            raise JevError(f"Jev request failed with HTTP {error.code}: {error_message(error)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise JevError(f"Jev request failed: {getattr(error, 'reason', error)}") from None
        except ValueError:
            raise JevError("Jev returned a response that is not JSON") from None
    raise JevError("Jev request failed after retries")


def error_message(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        return str(payload.get("error", {}).get("message") or payload)[:300]
    except Exception:
        return error.reason or "no details"


def yes_no(probability: Optional[float]) -> Optional[bool]:
    if probability is None:
        return None
    if probability >= YES_AT:
        return True
    if probability <= NO_AT:
        return False
    return None


def noul(answers: Dict[str, Any], name: str) -> Optional[float]:
    answer = answers.get(name) or {}
    value = answer.get("noul") if answer.get("type") == "noul" else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def interpret(answers: Dict[str, Any]) -> Dict[str, Any]:
    """Map Jev answers to the playbook conditions; None means "uncertain, the lead decides"."""
    kind_answer = answers.get("kind") or {}
    kind_probabilities = kind_answer.get("probabilities") or {}
    kind = kind_answer.get("choice") if kind_answer.get("type") == "choice" else None
    kind_p = kind_probabilities.get(kind) if kind else None
    effort_answer = answers.get("effort") or {}
    effort_probabilities = {
        EFFORTS[int(level)]: value for level, value in (effort_answer.get("probabilities") or {}).items()
        if str(level).isdigit() and int(level) < len(EFFORTS)
    }
    effort = max(effort_probabilities, key=effort_probabilities.get) if effort_probabilities else None
    risk_probabilities = {name: noul(answers, f"risk_{name}") for name in RISKS}
    known = [value for value in risk_probabilities.values() if value is not None]
    if any(value >= YES_AT for value in known):
        risky: Optional[bool] = True
    elif known and len(known) == len(RISKS) and all(value <= NO_AT for value in known):
        risky = False
    else:
        risky = None
    narrow_p = noul(answers, "narrow")
    conditions = {
        "kind": kind if kind in KINDS and isinstance(kind_p, (int, float)) and kind_p >= CHOICE_AT else None,
        "narrow": yes_no(narrow_p),
        "risky": risky,
        "effort": effort if effort and effort_probabilities[effort] >= CHOICE_AT else None,
    }
    return {
        "conditions": conditions,
        "uncertain": [name for name, value in conditions.items() if value is None],
        "probabilities": {
            "kind": kind_probabilities,
            "narrow": narrow_p,
            "effort": effort_probabilities,
            "risks": risk_probabilities,
        },
        "refused": sorted(name for name, answer in answers.items()
                          if isinstance(answer, dict) and answer.get("type") == "refusal"),
    }


def classify(task: str, key: str, model: str = DEFAULT_MODEL, endpoint: str = DEFAULT_ENDPOINT,
             opener: Optional[Callable[..., Any]] = None,
             sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    task = task.strip()
    if not task:
        raise JevError("the unit to classify is empty")
    body = {"model": model, "state": {"unit": task[:MAX_TASK_CHARS]}, "questions": unit_questions()}
    response = post(endpoint, key, body, opener, sleep)
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise JevError("Jev response has no answers")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return {
        **interpret(answers),
        "model": response.get("model") or model,
        "requestId": response.get("id"),
        "usage": {key_: usage.get(key_) for key_ in ("input_tokens", "output_tokens", "cost") if key_ in usage},
        "task": {"sha256": hashlib.sha256(task.encode("utf-8")).hexdigest()[:16], "chars": len(task),
                 "truncated": len(task) > MAX_TASK_CHARS},
    }
