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
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
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

LIVE_USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                   "cached_input_tokens", "output_tokens")


class LiveUsage:
    """Token usage read from a running worker's stream, before the runner records the final numbers.

    Claude repeats a message's usage on every content block, so usage is kept per message id and
    summed once. Codex reports usage on `turn.completed`; a `result` event's cost wins when present.
    """

    def __init__(self) -> None:
        self.offsets: Dict[Path, int] = {}
        self.messages: Dict[Path, Dict[str, Dict[str, int]]] = {}
        self.turns: Dict[Path, List[Dict[str, int]]] = {}
        self.costs: Dict[Path, float] = {}

    def read(self, path: Path) -> tuple:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offsets.get(path, 0))
                chunk = handle.read()
                end = chunk.rfind("\n") + 1
                self.offsets[path] = self.offsets.get(path, 0) + len(chunk[:end].encode("utf-8", errors="replace"))
        except OSError:
            return None, None
        messages = self.messages.setdefault(path, {})
        turns = self.turns.setdefault(path, [])
        for line in chunk[:end].splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                key = str(message.get("id") or len(messages))
                messages[key] = counts(message["usage"])
            elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                turns.append(counts(event["usage"]))
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                self.costs[path] = float(cost)
        parts = list(messages.values()) + turns
        if not parts:
            return None, self.costs.get(path)
        keys = [key for key in LIVE_USAGE_KEYS if any(key in part for part in parts)]
        return {key: sum(part.get(key, 0) for part in parts) for key in keys}, self.costs.get(path)


def counts(usage: Dict[str, Any]) -> Dict[str, int]:
    return {key: int(usage[key]) for key in LIVE_USAGE_KEYS
            if isinstance(usage.get(key), (int, float)) and not isinstance(usage.get(key), bool) and usage[key] >= 0}


def summary_rows(run_dir: Path, total_tokens: Callable[[Any], Optional[int]],
                 estimate: Optional[Callable[[str, str, Dict[str, int]], Optional[float]]] = None,
                 live: Optional[LiveUsage] = None) -> List[List[str]]:
    plan = read_json(run_dir / "plan.json") or {}
    state = read_json(run_dir / "state.json") or {}
    workers = state.get("workers") or {}
    live = live or LiveUsage()
    rows = []
    for entry in plan.get("workers", []):
        live_state = workers.get(entry.get("id")) or {}
        metrics = live_state.get("metrics") if isinstance(live_state.get("metrics"), dict) else {}
        tokens = total_tokens(metrics.get("usage"))
        cost = metrics.get("totalCostUsd")
        approx = ""
        if tokens is None and cost is None and live_state.get("status") not in (None, "queued"):
            usage, reported = live.read(run_dir / "workers" / str(entry.get("id")) / "stdout.log")
            if usage:
                tokens = total_tokens(usage)
                cost = reported
                if cost is None and estimate:
                    try:
                        cost = estimate(str(entry.get("provider") or ""), str(entry.get("model") or ""), usage)
                    except Exception:
                        cost = None
                approx = "~"
        elapsed = live_state.get("durationSeconds")
        if elapsed is None and live_state.get("startedAt"):
            started = parse_time(live_state["startedAt"])
            elapsed = time.time() - started if started else None
        idle = live_state.get("idleSeconds")
        status = live_state.get("status") or "queued"
        rows.append([
            entry.get("id", "?"),
            " · ".join(str(part) for part in (entry.get("provider"), entry.get("model")) if part),
            (entry.get("permissions") or "standard") if entry.get("mode") == "write" else "read",
            status + (f" (quiet {duration(idle)})" if status == "running" and idle and idle >= 30 else ""),
            duration(elapsed),
            f"{approx}{tokens:,}" if tokens else "-",
            f"{approx}${cost:.4f}" if isinstance(cost, (int, float)) else "-",
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
            poll: float = POLL_SECONDS, once: bool = False, sleep: Callable[[float], None] = time.sleep,
            estimate: Optional[Callable[[str, str, Dict[str, int]], Optional[float]]] = None) -> int:
    out = out or sys.stdout
    plan = read_json(run_dir / "plan.json") or {}
    title = plan.get("title") or run_dir.name
    interactive = hasattr(out, "isatty") and out.isatty() and not once
    live = LiveUsage()
    while True:
        state = read_json(run_dir / "state.json") or {}
        rows = summary_rows(run_dir, total_tokens, estimate, live)
        text = f"{title}  [{state.get('status', 'starting')}]  {clock()}\n\n" + render_table(rows)
        if any(str(cell).startswith("~") for row in rows for cell in row[5:]):
            text += "\n\n~ live, from the worker's stream; the cost is a list-price estimate until the CLI reports it"
        if interactive:
            out.write("\x1b[H\x1b[2J" + text + "\n")
        else:
            print(text, file=out)
        out.flush()
        if once or state.get("status") not in (None, "running"):
            return 0
        sleep(poll)


# ---- pipeline: every phase of an orchestration ----------------------------------------------

SYMBOLS = {"done": "✓", "running": "▶", "failed": "✗", "next": "▶", "waiting": "·", "skipped": "–"}


def wave_folders(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    return sorted(child for child in folder.iterdir()
                  if child.is_dir() and (child / "plan.json").is_file() and child.name[:2].isdigit())


def wave_phase(wave: Path) -> str:
    plan = read_json(wave / "plan.json") or {}
    phase = plan.get("phase")
    return str(phase) if phase else wave.name.split("-", 1)[-1]


def current_wave(folder: Path) -> Optional[Path]:
    waves = wave_folders(folder)
    running = [wave for wave in waves if (read_json(wave / "state.json") or {}).get("status") in (None, "running")]
    return (running or waves or [None])[-1]


def read_journal(folder: Path) -> List[Dict[str, Any]]:
    entries = []
    try:
        lines = (folder / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def phase_note(journal: List[Dict[str, Any]], phase_id: str, *kinds: str) -> Optional[str]:
    for entry in reversed(journal):
        text = str(entry.get("text") or "")
        if entry.get("kind") in kinds and text.split(":", 1)[0].strip() == phase_id:
            return text.split(":", 1)[1].strip() or entry.get("kind")
    return None


def wave_workers(wave: Path, total_tokens: Callable[[Any], Optional[int]], estimate: Any, live: LiveUsage) -> List[Dict[str, Any]]:
    plan = read_json(wave / "plan.json") or {}
    state = read_json(wave / "state.json") or {}
    rows = summary_rows(wave, total_tokens, estimate, live)
    workers = []
    for entry, row in zip(plan.get("workers", []), rows):
        live_state = (state.get("workers") or {}).get(entry.get("id")) or {}
        workers.append({"id": row[0], "runsOn": row[1], "status": live_state.get("status") or "queued",
                        "time": row[4], "tokens": row[5], "cost": row[6], "state": live_state})
    return workers


def worker_line(worker: Dict[str, Any]) -> str:
    model = worker["runsOn"].split(" · ")[-1]
    cost = worker["cost"]
    approx = "~" if cost.startswith("~") else ""
    if cost.lstrip("~").startswith("$"):
        value = float(cost.lstrip("~$"))
        cost = f"{approx}${value:.2f}" if value >= 0.01 else f"{approx}${value:.4f}"
    return f"    {worker['id']}  {model}  {worker['status']}  {worker['time']}  {worker['tokens']}  {cost}"


def money(cells: Iterable[str]) -> str:
    total, approx = 0.0, False
    for cell in cells:
        if cell.startswith("~"):
            approx, cell = True, cell[1:]
        if cell.startswith("$"):
            total += float(cell[1:])
    return f"{'~' if approx else ''}${total:.2f}" if total else "-"


def wave_status(workers: List[Dict[str, Any]], state: Dict[str, Any]) -> str:
    statuses = [worker["status"] for worker in workers]
    if state.get("status") in (None, "running") or any(status in ("running", "queued") for status in statuses):
        return "running"
    return "done" if all(status == "succeeded" for status in statuses) else "failed"


def pipeline_text(folder: Path, phases: List[Dict[str, Any]], total_tokens: Callable[[Any], Optional[int]],
                  estimate: Any = None, live: Optional[LiveUsage] = None, now: Optional[float] = None) -> str:
    """One line per playbook phase, with the workers of each phase underneath."""
    live = live or LiveUsage()
    meta = read_json(folder / "orchestration.json") or {}
    journal = read_journal(folder)
    waves = [(wave, wave_phase(wave)) for wave in wave_folders(folder)]
    order = [str(phase.get("id")) for phase in phases]
    started = {name for _, name in waves} | {str(entry.get("text", "")).split(":", 1)[0].strip()
                                             for entry in journal if entry.get("kind") == "lead"}
    reached = max([order.index(name) for name in started if name in order] or [-1])
    lines: List[str] = []
    costs: List[str] = []
    rows: List[tuple] = []
    for index, phase in enumerate(phases):
        name = str(phase.get("id"))
        mine = [wave for wave, wave_name in waves if wave_name == name]
        detail: List[str] = []
        if mine:
            per_wave = [wave_workers(wave, total_tokens, estimate, live) for wave in mine]
            workers = [worker for group in per_wave for worker in group]
            status = wave_status(per_wave[-1], read_json(mine[-1] / "state.json") or {})
            costs.extend(worker["cost"] for worker in workers)
            summary = f"{len(workers)} worker{'s' if len(workers) != 1 else ''}" + (f" · {len(mine)} waves" if len(mine) > 1 else "")
            detail.extend(worker_line(worker) for worker in workers)
        else:
            skipped = phase_note(journal, name, "skip")
            partners = {str(phase.get("with"))} | {str(other.get("id")) for other in phases if other.get("with") == name}
            if not skipped and any(partner in started for partner in partners - {"None"}):
                skipped = "another path of this step ran"
            lead_note = phase_note(journal, name, "lead")
            later = reached > index
            if skipped:
                status, summary = "skipped", f"skipped: {short(skipped, 60)}"
            elif phase.get("who") == "lead":
                status = "done" if (later or lead_note) else "waiting"
                summary = "lead, in your session" + (f": {short(lead_note, 60)}" if lead_note else "")
            elif lead_note:
                status, summary = "done", f"done by the lead: {short(lead_note, 60)}"
            elif phase.get("after"):
                status = "skipped" if later and not any(p.get("after") for p in phases[index + 1:reached + 1]) else "waiting"
                summary = f"only if {phase['after']} fails" if status == "waiting" else "not needed"
            else:
                condition = phase.get("if")
                condition = " and ".join(condition) if isinstance(condition, list) else condition
                status = "skipped" if later else "waiting"
                summary = ("did not run" if later else "waiting") + (f" (if {condition})" if condition else "")
        rows.append((name, phase.get("role") or "", status, summary, detail))
    for wave, wave_name in waves:
        if wave_name not in order:
            workers = wave_workers(wave, total_tokens, estimate, live)
            costs.extend(worker["cost"] for worker in workers)
            rows.append((wave_name, "added", wave_status(workers, read_json(wave / "state.json") or {}),
                         f"{len(workers)} worker{'s' if len(workers) != 1 else ''}",
                         [worker_line(worker) for worker in workers]))
    if not any(row[2] == "running" for row in rows):
        for position, row in enumerate(rows[:len(phases)]):
            if row[2] == "waiting" and not phases[position].get("after"):
                rows[position] = (row[0], row[1], "next", row[3], row[4])
                break
    done = sum(1 for row in rows if row[2] == "done")
    counted = sum(1 for position, row in enumerate(rows) if row[2] != "skipped"
                  and not (position < len(phases) and phases[position].get("after") and row[2] == "waiting"))
    width = max([len(row[0]) for row in rows] + [5])
    role_width = max([len(row[1]) for row in rows] + [4])
    for name, role, status, summary, detail in rows:
        lines.append(f"{SYMBOLS.get(status, '?')} {name.ljust(width)}  {role.ljust(role_width)}  {summary}")
        lines.extend(detail)
    created = parse_time(str(meta.get("createdAt") or ""))
    elapsed = duration((now or time.time()) - created) if created else "-"
    title = meta.get("title") or folder.name
    header = f"{title}\n{meta.get('playbook') or 'freeform'} · {elapsed} · {money(costs)} · {done} of {counted} phases done"
    return header + "\n\n" + "\n".join(lines)


def pipeline(folder: Path, phases: List[Dict[str, Any]], total_tokens: Callable[[Any], Optional[int]], out: Any = None,
             poll: float = POLL_SECONDS, once: bool = False, sleep: Callable[[float], None] = time.sleep,
             estimate: Any = None, panes: Optional["WavePanes"] = None) -> int:
    out = out or sys.stdout
    interactive = hasattr(out, "isatty") and out.isatty() and not once
    live = LiveUsage()
    while True:
        text = pipeline_text(folder, phases, total_tokens, estimate, live)
        if any("~$" in line or " ~" in line for line in text.splitlines()):
            text += "\n\n~ live estimate until the CLI reports"
        if interactive:
            width = shutil.get_terminal_size((100, 40)).columns
            fitted = "\n".join(line if len(line) <= width else line[: width - 1] + "…" for line in text.splitlines())
            out.write("\x1b[H\x1b[2J" + clock() + "\n" + fitted + "\n")
        else:
            print(text, file=out)
        out.flush()
        if panes is not None:
            try:
                panes.sync(current_wave(folder))
            except Exception as error:  # a pane problem must never stop the view
                print(f"! panes: {short(error, 200)}", file=out)
        if once:
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


def launcher(python: str, script: Path) -> Callable[..., str]:
    # `clear` hides the long launch line, so each pane opens on its own feed.
    return lambda *args: "clear; " + " ".join(shlex.quote(str(part)) for part in (python, script, *args))


def split_workers(root: str, run_dir: Path, workers: List[str], command: Callable[..., str],
                  runner: Callable[..., Any], prefix: str = "") -> Dict[str, str]:
    """Up to four rows in one column right of `root`; workers five to eight sit right of the first four."""
    shown, panes = workers[:MAX_HERDR_PANES], {}
    rows = min(len(shown), 4)
    for index, worker_id in enumerate(shown):
        if index == 0:
            target, direction, ratio = root, "right", "0.35" if not prefix else "0.5"
        elif index < rows:
            target, direction, ratio = panes[shown[index - 1]], "down", f"{1 / (rows - index + 1):.3f}"
        else:
            target, direction, ratio = panes[shown[index - 4]], "right", "0.5"
        split = herdr_json(["pane", "split", target, "--direction", direction, "--ratio", ratio,
                            "--cwd", str(run_dir), "--no-focus"], runner)
        pane = pane_id(split, "pane", "pane_id")
        panes[worker_id] = pane
        herdr_json(["pane", "rename", pane, f"{prefix}{worker_id}"[:60]], runner)
        herdr_json(["pane", "run", pane, command("follow", run_dir, worker_id)], runner)
    return panes


def open_herdr(run_dir: Path, script: Path, python: str = sys.executable,
               runner: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Open a background tab: the summary on the left, one feed per worker on the right."""
    if not in_herdr():
        raise RuntimeError("not running inside Herdr")
    plan = read_json(run_dir / "plan.json") or {}
    workers = [worker.get("id") for worker in plan.get("workers", []) if worker.get("id")]
    title = plan.get("title") or plan.get("phase") or run_dir.name
    command = launcher(python, script)
    created = herdr_json(["tab", "create", "--workspace", os.environ["HERDR_WORKSPACE_ID"], "--cwd", str(run_dir),
                          "--label", f"alethe: {short(title, 40)}", "--no-focus"], runner)
    tab = pane_id(created, "tab", "tab_id")
    summary_pane = pane_id(created, "root_pane", "pane_id")
    herdr_json(["pane", "rename", summary_pane, "summary"], runner)
    herdr_json(["pane", "run", summary_pane, command("watch", run_dir, "--here")], runner)
    panes = split_workers(summary_pane, run_dir, workers, command, runner)
    return {"tab": tab, "summaryPane": summary_pane, "workerPanes": panes,
            "notShown": workers[MAX_HERDR_PANES:]}


# ---- one tab per orchestration ---------------------------------------------------------------

HERDR_STATE = "herdr.json"


class WavePanes:
    """Keeps one feed per worker of the current wave beside the pipeline pane, replacing them per wave."""

    def __init__(self, root: str, command: Callable[..., str], runner: Callable[..., Any] = subprocess.run) -> None:
        self.root, self.command, self.runner = root, command, runner
        self.wave: Optional[Path] = None
        self.panes: Dict[str, str] = {}

    def sync(self, wave: Optional[Path]) -> None:
        if wave is None or wave == self.wave:
            return
        workers = [worker.get("id") for worker in (read_json(wave / "plan.json") or {}).get("workers", []) if worker.get("id")]
        if not workers:
            return
        for pane in self.panes.values():
            try:
                herdr_json(["pane", "close", pane], self.runner)
            except RuntimeError:
                pass  # the user already closed it
        self.wave = wave
        self.panes = split_workers(self.root, wave, workers, self.command, self.runner, prefix=f"{wave_phase(wave)} · ")


def pane_alive(pane: str, runner: Callable[..., Any] = subprocess.run) -> bool:
    try:
        herdr_json(["pane", "get", pane], runner)
        return True
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return False


def open_orchestration(folder: Path, script: Path, python: str = sys.executable,
                       runner: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """One background tab per orchestration: the pipeline on the left, the current wave's workers on the right.

    Reuses the tab while its pipeline pane is open; the pipeline pane swaps worker panes as waves start.
    """
    if not in_herdr():
        raise RuntimeError("not running inside Herdr")
    saved = read_json(folder / HERDR_STATE) or {}
    if saved.get("pipelinePane") and pane_alive(saved["pipelinePane"], runner):
        return {**saved, "reused": True}
    meta = read_json(folder / "orchestration.json") or {}
    title = meta.get("title") or folder.name
    created = herdr_json(["tab", "create", "--workspace", os.environ["HERDR_WORKSPACE_ID"], "--cwd", str(folder),
                          "--label", f"alethe: {short(title, 40)}", "--no-focus"], runner)
    tab = pane_id(created, "tab", "tab_id")
    root = pane_id(created, "root_pane", "pane_id")
    herdr_json(["pane", "rename", root, "pipeline"], runner)
    herdr_json(["pane", "run", root, launcher(python, script)("watch", folder, "--here", "--panes")], runner)
    state = {"tab": tab, "pipelinePane": root}
    (folder / HERDR_STATE).write_text(json.dumps(state), encoding="utf-8")
    return {**state, "reused": False}
