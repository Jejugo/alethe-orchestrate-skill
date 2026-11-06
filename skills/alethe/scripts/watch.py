"""Live views of a running wave: a summary table, one readable feed per worker, and Herdr panes.

Nothing here starts or steers a worker; it only reads the run folder the runner writes.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

FINAL = ("succeeded", "failed", "timed_out", "stopped", "skipped")
POLL_SECONDS = 1.0
TEXT_LIMIT = 600
MAX_HERDR_PANES = 8
HERDR_SOURCE = "alethe"


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clock() -> str:
    return time.strftime("%H:%M:%S")


def short(text: Any, limit: int = TEXT_LIMIT) -> str:
    value = " ".join(str(text).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


# ---- one readable line per CLI event -------------------------------------------------------

def codex_lines(event: Dict[str, Any]) -> List[str]:
    kind = event.get("type")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    item_type = item.get("type")
    if kind == "item.started":
        if item_type == "command_execution":
            return [f"$ {short(item.get('command', ''))}"]
        if item_type == "web_search":
            return [f"search {short(item.get('query', ''))}"]
        if item_type == "mcp_tool_call":
            return [f"tool {item.get('server', '?')}.{item.get('tool', '?')}"]
        return []
    if kind == "item.completed":
        if item_type == "agent_message":
            return [f"› {short(item.get('text', ''))}"]
        if item_type == "reasoning":
            return [f"· {short(item.get('text', ''), 200)}"]
        if item_type == "command_execution":
            code = item.get("exit_code")
            return [f"  ↳ exit {code}"] if code not in (None, 0) else []
        if item_type == "file_change":
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            return [f"✎ {change.get('kind', 'edit')} {change.get('path', '?')}" for change in changes if isinstance(change, dict)]
        if item_type == "todo_list":
            items = item.get("items") if isinstance(item.get("items"), list) else []
            return [f"☐ {short(entry.get('text', ''), 120)}" for entry in items if isinstance(entry, dict)]
        return []
    if kind == "turn.completed":
        return ["turn done"]
    if kind in ("error", "turn.failed"):
        error = event.get("error") if isinstance(event.get("error"), dict) else event
        return [f"! {short(error.get('message', 'error'))}"]
    return []


CLAUDE_INPUT_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description")


def claude_lines(event: Dict[str, Any]) -> List[str]:
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return [f"session started, model {event.get('model', '?')}"]
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), list) else []
    if kind == "assistant":
        lines = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                lines.append(f"› {short(block['text'])}")
            elif block.get("type") == "tool_use":
                name = block.get("name", "tool")
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                detail = next((arguments[key] for key in CLAUDE_INPUT_KEYS if arguments.get(key)), "")
                lines.append(f"$ {short(arguments['command'])}" if name == "Bash" and arguments.get("command")
                             else f"{name} {short(detail, 200)}".rstrip())
        return lines
    if kind == "user":
        return [f"  ↳ error: {short(block.get('content', ''), 200)}" for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error")]
    if kind == "result":
        cost = event.get("total_cost_usd")
        return [f"result: {event.get('subtype', 'done')}" + (f", ${cost:.4f}" if isinstance(cost, (int, float)) else "")]
    return []


def generic_lines(event: Dict[str, Any]) -> List[str]:
    role = event.get("role") or event.get("type")
    lines = []
    for call in event.get("tool_calls") or []:
        if isinstance(call, dict):
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            lines.append(f"tool {function.get('name', '?')}")
    text = event.get("content") if isinstance(event.get("content"), str) else event.get("text")
    if isinstance(text, str) and text.strip() and role in ("assistant", "message", "text", "output"):
        lines.append(f"› {short(text)}")
    return lines


def event_lines(provider: str, event: Any) -> List[str]:
    if not isinstance(event, dict):
        return []
    if provider == "codex":
        return codex_lines(event)
    if provider == "claude":
        return claude_lines(event)
    return generic_lines(event)


# ---- follow one worker -----------------------------------------------------------------------

def worker_entry(run_dir: Path, worker_id: str) -> Dict[str, Any]:
    plan = read_json(run_dir / "plan.json") or {}
    for worker in plan.get("workers", []):
        if worker.get("id") == worker_id:
            return worker
    return {"id": worker_id}


def worker_label(entry: Dict[str, Any]) -> str:
    parts = [entry.get("id", "?"), entry.get("provider") or "?"]
    if entry.get("model"):
        parts.append(str(entry["model"]))
    if entry.get("permissions") not in (None, "standard"):
        parts.append(str(entry["permissions"]))
    return " · ".join(parts)


def worker_state(run_dir: Path, worker_id: str) -> Dict[str, Any]:
    state = read_json(run_dir / "state.json") or {}
    worker = (state.get("workers") or {}).get(worker_id) or {}
    return {**worker, "runStatus": state.get("status")}


class HerdrReporter:
    """Shows the followed worker in Herdr's sidebar; silent and harmless outside Herdr."""

    def __init__(self, label: str, runner: Callable[..., Any] = subprocess.run) -> None:
        self.pane = os.environ.get("HERDR_PANE_ID") if os.environ.get("HERDR_ENV") == "1" else None
        self.binary = herdr_binary() if self.pane else None
        self.label = label
        self.runner = runner
        self.last: Optional[tuple] = None

    def report(self, state: str, message: str) -> None:
        if not (self.pane and self.binary) or self.last == (state, message):
            return
        self.last = (state, message)
        self.call("pane", "report-agent", self.pane, "--source", HERDR_SOURCE, "--agent", "alethe",
                  "--state", state, "--message", message[:120])
        self.call("pane", "report-metadata", self.pane, "--source", HERDR_SOURCE, "--title", self.label[:80])

    def call(self, *args: str) -> None:
        try:
            self.runner([self.binary, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def follow(run_dir: Path, worker_id: str, out: Any = None, poll: float = POLL_SECONDS,
           reporter: Optional[HerdrReporter] = None, sleep: Callable[[float], None] = time.sleep) -> int:
    out = out or sys.stdout
    entry = worker_entry(run_dir, worker_id)
    provider = entry.get("provider") or ""
    label = worker_label(entry)
    reporter = reporter or HerdrReporter(label)
    log = run_dir / "workers" / worker_id / "stdout.log"
    print(f"== {label}", file=out)
    if entry.get("why"):
        print(f"   {short(entry['why'], 200)}", file=out)
    offset, buffer, announced, last_note = 0, "", False, time.monotonic()
    while True:
        state = worker_state(run_dir, worker_id)
        status = state.get("status") or "queued"
        if log.is_file():
            with log.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
            offset += len(chunk)
            buffer += chunk.decode("utf-8", errors="replace")
            *complete, buffer = buffer.split("\n")
            for raw in complete:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                for line in event_lines(provider, event):
                    print(f"{clock()} {line}", file=out)
        if status == "running":
            reporter.report("working", f"{entry.get('id')}: running")
            if provider == "agy" and not announced:
                print(f"{clock()} agy writes its output only when it finishes; waiting", file=out)
                announced = True
            if time.monotonic() - last_note >= 60 and provider in ("agy", "kimi"):
                print(f"{clock()} still running", file=out)
                last_note = time.monotonic()
        elif status == "queued":
            reporter.report("idle", f"{entry.get('id')}: queued")
        if status in FINAL or (status != "running" and state.get("runStatus") not in (None, "running")):
            finish(run_dir, worker_id, state, out)
            reporter.report("idle", f"{entry.get('id')}: {status}")
            out.flush()
            return 0
        out.flush()
        sleep(poll)


def finish(run_dir: Path, worker_id: str, state: Dict[str, Any], out: Any) -> None:
    status = state.get("status") or "unknown"
    detail = state.get("failureKind") or state.get("error")
    print(f"\n== {status}" + (f" ({detail})" if detail and status != "succeeded" else "")
          + f" in {duration(state.get('durationSeconds'))}", file=out)
    result = run_dir / "workers" / worker_id / "result.md"
    if result.is_file():
        lines = result.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        print("-- result.md", file=out)
        for line in lines[:40]:
            print(line, file=out)
        if len(lines) > 40:
            print(f"… {len(lines) - 40} more lines in {result}", file=out)


# ---- summary table ---------------------------------------------------------------------------

def summary_rows(run_dir: Path, total_tokens: Callable[[Any], Optional[int]]) -> List[List[str]]:
    plan = read_json(run_dir / "plan.json") or {}
    state = read_json(run_dir / "state.json") or {}
    workers = state.get("workers") or {}
    rows = []
    for entry in plan.get("workers", []):
        live = workers.get(entry.get("id")) or {}
        metrics = live.get("metrics") if isinstance(live.get("metrics"), dict) else {}
        tokens = total_tokens(metrics.get("usage"))
        cost = metrics.get("totalCostUsd")
        elapsed = live.get("durationSeconds")
        if elapsed is None and live.get("startedAt"):
            started = parse_time(live["startedAt"])
            elapsed = time.time() - started if started else None
        idle = live.get("idleSeconds")
        status = live.get("status") or "queued"
        rows.append([
            entry.get("id", "?"),
            " · ".join(str(part) for part in (entry.get("provider"), entry.get("model")) if part),
            (entry.get("permissions") or "standard") if entry.get("mode") == "write" else "read",
            status + (f" (quiet {duration(idle)})" if status == "running" and idle and idle >= 30 else ""),
            duration(elapsed),
            f"{tokens:,}" if tokens else "-",
            f"${cost:.4f}" if isinstance(cost, (int, float)) else "-",
        ])
    return rows


def parse_time(value: str) -> Optional[float]:
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def render_table(rows: Sequence[Sequence[str]]) -> str:
    header = ["worker", "runs on", "access", "status", "time", "tokens", "cost"]
    table = [header, *rows]
    widths = [max(len(str(row[index])) for row in table) for index in range(len(header))]
    return "\n".join("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
                     for row in table)


def summary(run_dir: Path, total_tokens: Callable[[Any], Optional[int]], out: Any = None,
            poll: float = POLL_SECONDS, once: bool = False, sleep: Callable[[float], None] = time.sleep) -> int:
    out = out or sys.stdout
    plan = read_json(run_dir / "plan.json") or {}
    title = plan.get("title") or run_dir.name
    interactive = hasattr(out, "isatty") and out.isatty() and not once
    while True:
        state = read_json(run_dir / "state.json") or {}
        text = f"{title}  [{state.get('status', 'starting')}]  {clock()}\n\n" + render_table(summary_rows(run_dir, total_tokens))
        if interactive:
            out.write("\x1b[H\x1b[2J" + text + "\n")
        else:
            print(text, file=out)
        out.flush()
        if once or state.get("status") not in (None, "running"):
            return 0
        sleep(poll)


# ---- Herdr panes -----------------------------------------------------------------------------

def herdr_binary() -> Optional[str]:
    configured = os.environ.get("HERDR_BIN_PATH")
    return configured if configured and Path(configured).is_file() else shutil.which("herdr")


def in_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_WORKSPACE_ID")) and herdr_binary() is not None


def herdr_json(args: Sequence[str], runner: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    result = runner([herdr_binary(), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args[:2])} failed: {short(result.stderr or result.stdout, 300)}")
    try:
        return json.loads(result.stdout or "{}")
    except ValueError as error:
        raise RuntimeError(f"herdr {' '.join(args[:2])} returned no JSON") from error


def pane_id(response: Dict[str, Any], *path: str) -> str:
    value: Any = response.get("result", response)
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    if not isinstance(value, str):
        raise RuntimeError("herdr returned no pane id")
    return value


def open_herdr(run_dir: Path, script: Path, python: str = sys.executable,
               runner: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Open a background tab: the summary on the left, one feed per worker on the right."""
    if not in_herdr():
        raise RuntimeError("not running inside Herdr")
    plan = read_json(run_dir / "plan.json") or {}
    workers = [worker.get("id") for worker in plan.get("workers", []) if worker.get("id")]
    title = plan.get("title") or plan.get("phase") or run_dir.name
    # `clear` hides the long launch line, so each pane opens on its own feed.
    command = lambda *args: "clear; " + " ".join(shlex.quote(str(part)) for part in (python, script, *args))
    created = herdr_json(["tab", "create", "--workspace", os.environ["HERDR_WORKSPACE_ID"], "--cwd", str(run_dir),
                          "--label", f"alethe: {short(title, 40)}", "--no-focus"], runner)
    tab = pane_id(created, "tab", "tab_id")
    summary_pane = pane_id(created, "root_pane", "pane_id")
    herdr_json(["pane", "rename", summary_pane, "summary"], runner)
    herdr_json(["pane", "run", summary_pane, command("watch", run_dir, "--here")], runner)
    # Up to four rows in one column; workers five to eight sit to the right of the first four.
    shown, panes = workers[:MAX_HERDR_PANES], {}
    rows = min(len(shown), 4)
    for index, worker_id in enumerate(shown):
        if index == 0:
            target, direction, ratio = summary_pane, "right", "0.35"
        elif index < rows:
            target, direction, ratio = panes[shown[index - 1]], "down", f"{1 / (rows - index + 1):.3f}"
        else:
            target, direction, ratio = panes[shown[index - 4]], "right", "0.5"
        split = herdr_json(["pane", "split", target, "--direction", direction, "--ratio", ratio,
                            "--cwd", str(run_dir), "--no-focus"], runner)
        pane = pane_id(split, "pane", "pane_id")
        panes[worker_id] = pane
        herdr_json(["pane", "rename", pane, worker_id], runner)
        herdr_json(["pane", "run", pane, command("follow", run_dir, worker_id)], runner)
    return {"tab": tab, "summaryPane": summary_pane, "workerPanes": panes,
            "notShown": workers[MAX_HERDR_PANES:]}
