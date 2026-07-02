"""``npc watch`` scanner and lightweight terminal renderer.

The scanner is intentionally file-based and read-only.  It treats npc state
files and ``tasks/*.json`` as the canonical observation surface, with Claude
transcripts left as optional pointers inside task records.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import _io, paths as _paths, state as _state, status as _status
from . import task as _task


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(value: str | None, now: datetime) -> int | None:
    dt = _parse_iso(value)
    if dt is None:
        return None
    if dt.tzinfo is None and now.tzinfo is not None:
        dt = dt.replace(tzinfo=now.tzinfo)
    try:
        age = now - dt
    except TypeError:
        return None
    return max(0, int(age.total_seconds()))


def _safe_int(value: Any, default: int) -> int:
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def observe_task(task_doc: dict, *, now: datetime, stale_seconds_override: int | None = None) -> dict:
    """Add derived liveness fields to one task record."""
    out = dict(task_doc)
    status = task_doc.get("status")
    check = task_doc.get("check") if isinstance(task_doc.get("check"), dict) else {}
    stale_seconds = (
        stale_seconds_override
        if stale_seconds_override is not None
        else _safe_int(check.get("stale_seconds"), _task.DEFAULT_STALE_SECONDS)
    )
    heartbeat_at = task_doc.get("last_heartbeat_at") or task_doc.get("updated_at")
    age = _age_seconds(heartbeat_at, now)

    if status in _task.TERMINAL_STATUSES:
        observed = status
    elif age is None:
        observed = "unknown"
    elif age > stale_seconds:
        observed = "stale"
    else:
        observed = status if status in _task.VALID_STATUSES else "running"

    out["observed_status"] = observed
    out["heartbeat_age_seconds"] = age
    out["stale_seconds"] = stale_seconds
    return out


def scan_tasks(run_dir: Path, *, now: datetime, stale_seconds_override: int | None = None) -> list[dict]:
    """Read ``<run_dir>/tasks/*.json`` and derive liveness for each task."""
    root = _task.tasks_dir(run_dir)
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".tmp"):
            continue
        try:
            task_doc = _task.read_task(path)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            rows.append(
                {
                    "task_id": path.stem,
                    "observed_status": "unreadable",
                    "error": str(e),
                    "task_json": str(path),
                }
            )
            continue
        observed = observe_task(
            task_doc,
            now=now,
            stale_seconds_override=stale_seconds_override,
        )
        observed["task_json"] = str(path)
        observed["events"] = str(_task.task_events_path(run_dir, path.stem))
        rows.append(observed)
    rows.sort(key=lambda r: (r.get("observed_status") in _task.TERMINAL_STATUSES, r.get("task_id") or ""))
    return rows


def scan_run(p: _paths.Paths, *, now: datetime, stale_seconds_override: int | None = None) -> dict:
    """Return one run snapshot from npc state + watchable tasks."""
    state_summary: dict | None = None
    state_error: str | None = None
    try:
        state = _state.read_state(p.state_json)
        state_summary = _status.summarize_status(state)
    except (OSError, ValueError) as e:
        state_error = str(e)

    return {
        "proj_key": p.proj_key,
        "repo_root": str(p.repo_root),
        "task_log_dir": str(p.task_log_dir),
        "run_ts": p.run_ts,
        "run_dir": str(p.run_dir),
        "state_json": str(p.state_json),
        "state": state_summary,
        "state_error": state_error,
        "tasks": scan_tasks(
            p.run_dir,
            now=now,
            stale_seconds_override=stale_seconds_override,
        ),
    }


def _paths_for_project(project: str, run_ts: str | None) -> _paths.Paths:
    repo_root = _paths.detect_repo_root(Path(project))
    task_log_dir = _paths.task_log_dir_for(repo_root)
    ts = run_ts or _paths.read_active(task_log_dir)
    if not ts:
        raise _paths.PathsError(f"项目没有 active run：{task_log_dir}")
    run_json = _paths.run_json_path_for(task_log_dir, ts)
    if not run_json.is_file():
        raise _paths.PathsError(f"未找到 run.json：{run_json}")
    return _paths.read_run_json(run_json)


def _iter_active_paths(home: Path) -> list[_paths.Paths]:
    root = home / "task_log"
    if not root.is_dir():
        return []
    out: list[_paths.Paths] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name.startswith("_"):
            continue
        active_ts = _paths.read_active(child)
        if not active_ts:
            continue
        run_json = _paths.run_json_path_for(child, active_ts)
        if not run_json.is_file():
            continue
        try:
            out.append(_paths.read_run_json(run_json))
        except _paths.PathsError:
            continue
    return out


def resolve_targets(args: argparse.Namespace) -> list[_paths.Paths]:
    """Resolve watch scope into run path records."""
    run_ts = getattr(args, "run_ts", None)
    project = getattr(args, "project", None)
    if getattr(args, "all", False):
        return _iter_active_paths(Path.home())
    if project:
        return [_paths_for_project(project, run_ts)]
    return [_paths.load_paths(args)]


def build_snapshot(args: argparse.Namespace) -> dict:
    now = datetime.now().astimezone()
    try:
        targets = resolve_targets(args)
    except _paths.PathsError as e:
        raise WatchError(str(e)) from e
    stale_override = getattr(args, "stale_seconds", None)
    return {
        "ok": True,
        "schema_version": 1,
        "generated_at": _io.now_iso(),
        "scope": "all" if getattr(args, "all", False) else "project",
        "runs": [
            scan_run(p, now=now, stale_seconds_override=stale_override)
            for p in targets
        ],
    }


class WatchError(Exception):
    """Could not build watch snapshot."""


def _fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def render_text(snapshot: dict) -> str:
    lines = [f"npc watch  generated={snapshot.get('generated_at')}"]
    runs = snapshot.get("runs") or []
    if not runs:
        lines.append("No active runs found.")
        return "\n".join(lines)

    for run in runs:
        lines.append("")
        lines.append(f"{run.get('proj_key')}  run={run.get('run_ts')}")
        state = run.get("state") or {}
        if state:
            current = state.get("current")
            cur = "-"
            if isinstance(current, dict):
                cur = f"#{current.get('seq')} {current.get('change_id')} {current.get('status')}"
            lines.append(
                f"  state: {state.get('top_status')} total={state.get('total')} current={cur}"
            )
        elif run.get("state_error"):
            lines.append(f"  state: unreadable ({run.get('state_error')})")
        tasks = run.get("tasks") or []
        if not tasks:
            lines.append("  tasks: none")
            continue
        lines.append("  tasks:")
        for t in tasks:
            progress = t.get("progress") if isinstance(t.get("progress"), dict) else {}
            prog = ""
            if progress:
                cur = progress.get("current")
                total = progress.get("total")
                unit = progress.get("unit") or ""
                if cur is not None and total is not None:
                    prog = f" {cur}/{total}{unit}"
                elif cur is not None:
                    prog = f" {cur}{unit}"
            desc = t.get("description") or t.get("task_id")
            msg = t.get("message") or ""
            lines.append(
                "    "
                f"[{t.get('observed_status')}] {t.get('task_id')} "
                f"phase={t.get('phase') or '-'} age={_fmt_age(t.get('heartbeat_age_seconds'))}"
                f"{prog}  {desc} {msg}".rstrip()
            )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> None:
    """``npc watch``: snapshot once as JSON or refresh a terminal view."""
    try:
        snapshot = build_snapshot(args)
    except WatchError as e:
        _io.emit_error("watch_failed", str(e), exit_code=3)
        return

    if getattr(args, "once", False):
        _io.emit(snapshot)
        return

    interval = getattr(args, "interval", None) or 2.0
    try:
        while True:
            try:
                snapshot = build_snapshot(args)
            except WatchError as e:
                sys.stdout.write(f"\033[2J\033[Hnpc watch failed: {e}\n")
                sys.stdout.flush()
                time.sleep(interval)
                continue
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.write(render_text(snapshot) + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return
