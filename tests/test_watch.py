"""watchable task contract and ``npc watch`` tests."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from npc import paths as _paths, state as _state, task as _task, watch as _watch


def _read_emit(capsys) -> dict:
    lines = capsys.readouterr().out.strip().splitlines()
    return json.loads(lines[-1])


def _base_args(p: _paths.Paths, **kwargs) -> argparse.Namespace:
    ns = argparse.Namespace(
        state_json=None,
        run_ts=p.run_ts,
        task_log_dir=str(p.task_log_dir),
    )
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def _task_args(p: _paths.Paths, **kwargs) -> argparse.Namespace:
    defaults = {
        "id": "implement-001",
        "description": "Implement first change",
        "source": "npc",
        "session_id": "sid-1",
        "stale_seconds": 900,
        "replace": False,
        "phase": "implement",
        "message": "starting",
        "progress_current": None,
        "progress_total": None,
        "progress_unit": None,
        "log": None,
        "summary": None,
        "transcript": None,
    }
    defaults.update(kwargs)
    return _base_args(p, **defaults)


def _prepare_run(p: _paths.Paths) -> None:
    _paths.write_run_json(p)
    _paths.set_active(p.task_log_dir, p.run_ts)
    state = {
        "schema_version": 2,
        "run_ts": p.run_ts,
        "status": "in-progress",
        "plan_order": ["add-foo"],
        "progress": [
            {
                "seq": 1,
                "change_id": "add-foo",
                "status": "implementing",
                "blocking_trend": [],
                "phases": {},
            }
        ],
    }
    _state.write_state(p.state_json, p.state_md, state)


def test_task_start_writes_contract(computed_paths: _paths.Paths, capsys):
    _prepare_run(computed_paths)

    _task.start(
        _task_args(
            computed_paths,
            progress_current=1,
            progress_total=4,
            progress_unit="steps",
        )
    )
    payload = _read_emit(capsys)

    assert payload["ok"] is True
    assert payload["task_id"] == "implement-001"
    task_path = Path(payload["task_json"])
    data = json.loads(task_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["status"] == "running"
    assert data["check"] == {"type": "heartbeat", "stale_seconds": 900}
    assert data["progress"] == {"current": 1, "total": 4, "unit": "steps"}
    assert data["worktree"]["repo_root"] == str(computed_paths.repo_root)
    assert data["worktree"]["worktree_root"] == str(computed_paths.repo_root)
    assert data["pointer"]["state_json"] == str(computed_paths.state_json)
    assert Path(payload["events"]).read_text(encoding="utf-8").strip()


def test_task_start_rejects_unsafe_id(computed_paths: _paths.Paths, capsys):
    _prepare_run(computed_paths)

    with pytest.raises(SystemExit) as exc:
        _task.start(_task_args(computed_paths, id="../bad"))

    assert exc.value.code == 2
    payload = _read_emit(capsys)
    assert payload["error"] == "invalid_task_id"


def test_task_heartbeat_and_finish_update_contract(computed_paths: _paths.Paths, capsys):
    _prepare_run(computed_paths)
    _task.start(_task_args(computed_paths))
    capsys.readouterr()

    hb_args = _base_args(
        computed_paths,
        id="implement-001",
        status="running",
        phase="test",
        message="go test ./...",
        progress_current=2,
        progress_total=4,
        progress_unit="steps",
        log=None,
        summary=None,
        transcript=None,
    )
    _task.heartbeat(hb_args)
    hb = _read_emit(capsys)
    assert hb["kind"] == "task.heartbeat"

    fin_args = _base_args(
        computed_paths,
        id="implement-001",
        status="done",
        phase=None,
        message="finished",
        summary=str(computed_paths.run_dir / "summary.md"),
        result="RESULT: ok",
    )
    _task.finish(fin_args)
    payload = _read_emit(capsys)
    assert payload["status"] == "done"

    data = json.loads(Path(payload["task_json"]).read_text(encoding="utf-8"))
    assert data["status"] == "done"
    assert data["message"] == "finished"
    assert data["result"] == "RESULT: ok"
    assert data["pointer"]["summary"].endswith("summary.md")
    assert data["finished_at"]


def test_observe_task_marks_stale_when_heartbeat_expires():
    task_doc = {
        "task_id": "t",
        "status": "running",
        "check": {"type": "heartbeat", "stale_seconds": 60},
        "last_heartbeat_at": "2026-07-02T00:00:00+00:00",
    }
    now = datetime(2026, 7, 2, 0, 2, 0, tzinfo=timezone.utc)

    out = _watch.observe_task(task_doc, now=now)

    assert out["observed_status"] == "stale"
    assert out["heartbeat_age_seconds"] == 120


def test_watch_once_emits_active_run_snapshot(computed_paths: _paths.Paths, capsys):
    _prepare_run(computed_paths)
    _task.start(_task_args(computed_paths, phase="implement", message="working"))
    capsys.readouterr()

    args = _base_args(
        computed_paths,
        all=False,
        project=None,
        once=True,
        interval=2.0,
        stale_seconds=None,
    )
    _watch.run(args)
    payload = _read_emit(capsys)

    assert payload["ok"] is True
    assert payload["scope"] == "project"
    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["run_ts"] == computed_paths.run_ts
    assert run["state"]["current"] == {
        "seq": 1,
        "change_id": "add-foo",
        "status": "implementing",
    }
    assert run["tasks"][0]["task_id"] == "implement-001"
    assert run["tasks"][0]["observed_status"] == "running"


def test_watch_all_scans_only_active_runs(computed_paths: _paths.Paths, fake_home: Path, monkeypatch):
    _prepare_run(computed_paths)
    _task.start(_task_args(computed_paths))

    monkeypatch.setattr(_watch.Path, "home", lambda: fake_home)
    args = argparse.Namespace(
        state_json=None,
        run_ts=None,
        task_log_dir=None,
        all=True,
        project=None,
        once=True,
        interval=2.0,
        stale_seconds=None,
    )

    snapshot = _watch.build_snapshot(args)

    assert snapshot["scope"] == "all"
    assert [r["run_ts"] for r in snapshot["runs"]] == [computed_paths.run_ts]
