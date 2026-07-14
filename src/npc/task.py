"""Watchable task contract for npc-managed background work.

This module gives long-running work a small, explicit reporting surface under
``<run_dir>/tasks``.  ``npc watch`` can then observe tasks without guessing from
Claude Code's internal transcript format.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from . import _io, paths as _paths


SCHEMA_VERSION = 1
TASKS_DIRNAME = "tasks"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
LIVE_STATUSES = frozenset({"running", "waiting"})
VALID_STATUSES = TERMINAL_STATUSES | LIVE_STATUSES

DEFAULT_SOURCE = "manual"
DEFAULT_STALE_SECONDS = 900


def validate_task_id(task_id: str) -> str:
    """Validate a task id that is safe to use as a filename stem."""
    if not TASK_ID_RE.fullmatch(task_id or ""):
        raise ValueError(
            "task id 必须匹配 [A-Za-z0-9][A-Za-z0-9_.-]{0,127}，"
            "不能包含路径分隔符"
        )
    return task_id


def tasks_dir(run_dir: Path) -> Path:
    return run_dir / TASKS_DIRNAME


def task_json_path(run_dir: Path, task_id: str) -> Path:
    return tasks_dir(run_dir) / f"{validate_task_id(task_id)}.json"


def task_events_path(run_dir: Path, task_id: str) -> Path:
    return tasks_dir(run_dir) / f"{validate_task_id(task_id)}.events.jsonl"


def read_task(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"task json 不是对象：{path}")
    return data


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _append_event(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("ts", _io.now_iso())
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def _git_output(repo_root: Path, args: list[str]) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    text = out.stdout.strip()
    return text or None


def capture_worktree(repo_root: Path) -> dict[str, str | None]:
    """Capture git/worktree identity at task registration time."""
    worktree_root = _git_output(repo_root, ["rev-parse", "--show-toplevel"])
    common_dir = _git_output(repo_root, ["rev-parse", "--git-common-dir"])
    if common_dir:
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = (repo_root / common_path).resolve()
        common_dir = str(common_path)

    return {
        "repo_root": str(repo_root),
        "worktree_root": worktree_root or str(repo_root),
        "git_common_dir": common_dir,
        "branch": _git_output(repo_root, ["branch", "--show-current"]),
        "head": _git_output(repo_root, ["rev-parse", "HEAD"]),
        "head_short": _git_output(repo_root, ["rev-parse", "--short", "HEAD"]),
    }


def _pointer_from_args(args: argparse.Namespace, p: _paths.Paths) -> dict[str, str]:
    pointer: dict[str, str] = {
        "state_json": str(p.state_json),
        "run_events": str(p.run_events),
    }
    for arg_name, key in (
        ("log", "log"),
        ("summary", "summary"),
        ("transcript", "transcript"),
    ):
        value = getattr(args, arg_name, None)
        if value:
            pointer[key] = str(Path(value))
    return pointer


def _progress_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    current = getattr(args, "progress_current", None)
    total = getattr(args, "progress_total", None)
    unit = getattr(args, "progress_unit", None)
    if current is None and total is None and unit is None:
        return None
    progress: dict[str, Any] = {}
    if current is not None:
        progress["current"] = current
    if total is not None:
        progress["total"] = total
    if unit is not None:
        progress["unit"] = unit
    return progress


def _load_existing_task(p: _paths.Paths, task_id: str) -> tuple[Path, Path, dict]:
    try:
        validate_task_id(task_id)
    except ValueError as e:
        raise TaskInputError(str(e)) from e
    path = task_json_path(p.run_dir, task_id)
    events = task_events_path(p.run_dir, task_id)
    try:
        data = read_task(path)
    except FileNotFoundError as e:
        raise TaskInputError(f"task 不存在：{path}") from e
    except (json.JSONDecodeError, ValueError) as e:
        raise TaskInputError(f"task json 读取失败：{path}：{e}") from e
    return path, events, data


class TaskInputError(Exception):
    """Invalid user input or missing task state."""


def start(args: argparse.Namespace) -> None:
    """``npc task start``: register a watchable task under the active run."""
    try:
        task_id = validate_task_id(args.id)
        p = _paths.load_paths(args)
    except ValueError as e:
        _io.emit_error("invalid_task_id", str(e), exit_code=2)
        return
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    path = task_json_path(p.run_dir, task_id)
    events = task_events_path(p.run_dir, task_id)
    if path.exists() and not getattr(args, "replace", False):
        _io.emit_error("task_exists", f"task 已存在：{path}", exit_code=1)
        return

    now = _io.now_iso()
    stale_seconds = getattr(args, "stale_seconds", None) or DEFAULT_STALE_SECONDS
    progress = _progress_from_args(args)
    task_doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "description": args.description,
        "source": args.source or DEFAULT_SOURCE,
        "status": "running",
        "phase": args.phase,
        "message": args.message,
        "progress": progress or {},
        "check": {
            "type": "heartbeat",
            "stale_seconds": stale_seconds,
        },
        "proj_key": p.proj_key,
        "run_ts": p.run_ts,
        "session_id": args.session_id or os.environ.get("NPC_SESSION_ID") or None,
        "worktree": capture_worktree(p.repo_root),
        "pointer": _pointer_from_args(args, p),
        "created_at": now,
        "updated_at": now,
        "last_heartbeat_at": now,
    }
    _atomic_write_json(path, task_doc)
    _append_event(events, {"kind": "task.started", **_event_fields(task_doc)})
    _io.emit(
        {
            "ok": True,
            "task_id": task_id,
            "status": "running",
            "task_json": str(path),
            "events": str(events),
        }
    )


def _event_fields(task_doc: dict) -> dict:
    return {
        "task_id": task_doc.get("task_id"),
        "status": task_doc.get("status"),
        "phase": task_doc.get("phase"),
        "message": task_doc.get("message"),
        "progress": task_doc.get("progress") or {},
        "proj_key": task_doc.get("proj_key"),
        "run_ts": task_doc.get("run_ts"),
        "session_id": task_doc.get("session_id"),
    }


def _apply_update(args: argparse.Namespace, *, heartbeat: bool) -> tuple[str, Path, Path, dict]:
    p = _paths.load_paths(args)
    path, events, task_doc = _load_existing_task(p, args.id)

    now = _io.now_iso()
    status = getattr(args, "status", None)
    if status:
        task_doc["status"] = status
    phase = getattr(args, "phase", None)
    if phase is not None:
        task_doc["phase"] = phase
    message = getattr(args, "message", None)
    if message is not None:
        task_doc["message"] = message
    progress = _progress_from_args(args)
    if progress is not None:
        task_doc["progress"] = progress
    pointer_updates = _pointer_from_args(args, p)
    if pointer_updates:
        pointer = task_doc.setdefault("pointer", {})
        if isinstance(pointer, dict):
            pointer.update(pointer_updates)
    if heartbeat:
        task_doc["last_heartbeat_at"] = now
    task_doc["updated_at"] = now

    _atomic_write_json(path, task_doc)
    kind = "task.heartbeat" if heartbeat else "task.updated"
    _append_event(events, {"kind": kind, **_event_fields(task_doc)})
    return kind, path, events, task_doc


def update(args: argparse.Namespace) -> None:
    """``npc task update``: update task phase/message/progress."""
    try:
        kind, path, events, task_doc = _apply_update(args, heartbeat=False)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    except TaskInputError as e:
        _io.emit_error("task_update_failed", str(e), exit_code=1)
        return
    _io.emit(
        {
            "ok": True,
            "kind": kind,
            "task_id": task_doc.get("task_id"),
            "status": task_doc.get("status"),
            "task_json": str(path),
            "events": str(events),
        }
    )


def heartbeat(args: argparse.Namespace) -> None:
    """``npc task heartbeat``: refresh liveness for a running task."""
    try:
        kind, path, events, task_doc = _apply_update(args, heartbeat=True)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    except TaskInputError as e:
        _io.emit_error("task_heartbeat_failed", str(e), exit_code=1)
        return
    _io.emit(
        {
            "ok": True,
            "kind": kind,
            "task_id": task_doc.get("task_id"),
            "status": task_doc.get("status"),
            "last_heartbeat_at": task_doc.get("last_heartbeat_at"),
            "task_json": str(path),
            "events": str(events),
        }
    )


def finish(args: argparse.Namespace) -> None:
    """``npc task finish``: mark a task as terminal."""
    try:
        p = _paths.load_paths(args)
        path, events, task_doc = _load_existing_task(p, args.id)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    except TaskInputError as e:
        _io.emit_error("task_finish_failed", str(e), exit_code=1)
        return

    now = _io.now_iso()
    task_doc["status"] = args.status
    if args.phase is not None:
        task_doc["phase"] = args.phase
    if args.message is not None:
        task_doc["message"] = args.message
    if args.summary:
        pointer = task_doc.setdefault("pointer", {})
        if isinstance(pointer, dict):
            pointer["summary"] = str(Path(args.summary))
    if args.result is not None:
        task_doc["result"] = args.result
    task_doc["updated_at"] = now
    task_doc["last_heartbeat_at"] = now
    task_doc["finished_at"] = now

    _atomic_write_json(path, task_doc)
    _append_event(events, {"kind": "task.finished", **_event_fields(task_doc)})
    _io.emit(
        {
            "ok": True,
            "task_id": task_doc.get("task_id"),
            "status": task_doc.get("status"),
            "task_json": str(path),
            "events": str(events),
        }
    )
