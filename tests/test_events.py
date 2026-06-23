"""events 模块测试。"""

from __future__ import annotations

import json

import pytest

from npc import events as _events, state as _state


def _bootstrap(env_setup, capsys, make_args, *change_ids: str) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def test_phase_enter_writes_state_and_event(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _events.phase_enter(make_args(seq=1, phase="implement"))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["phase"] == "implement"

    s = json.loads(env_setup.state_json.read_text())
    ph = s["progress"][0]["phases"]["implement"]
    assert ph["status"] == "in-progress"
    assert "started_at" in ph
    assert "started_ms" in ph

    # 双流事件
    per_change = env_setup.run_dir / "001-add-foo" / "events.jsonl"
    assert per_change.exists()
    lines = per_change.read_text().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["event"] == "phase.start"
    assert ev["phase"] == "implement"
    assert ev["change_seq"] == 1

    run_lines = env_setup.run_events.read_text().splitlines()
    assert len(run_lines) == 1
    assert json.loads(run_lines[0])["change_id"] == "add-foo"


def test_phase_exit_computes_duration(env_setup, capsys, monkeypatch, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")

    # 注入可控时间
    from npc import _io

    times = iter([1000, 5500, 5500])  # enter_ms, exit_ms (state), exit_ms (event)
    monkeypatch.setattr(_io, "now_ms", lambda: next(times))

    _events.phase_enter(make_args(seq=1, phase="implement"))
    capsys.readouterr()
    _events.phase_exit(
        make_args(seq=1, phase="implement", status="done", extra='{"commit":"abc123","tasks":3}')
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["duration_ms"] == 4500

    s = json.loads(env_setup.state_json.read_text())
    ph = s["progress"][0]["phases"]["implement"]
    assert ph["status"] == "done"
    assert ph["duration_ms"] == 4500
    assert ph["commit"] == "abc123"
    assert ph["tasks"] == 3
    assert "started_ms" not in ph  # 清理掉

    # 事件流：phase.start + implement.done
    per_change = env_setup.run_dir / "001-add-foo" / "events.jsonl"
    lines = per_change.read_text().splitlines()
    assert len(lines) == 2
    done_ev = json.loads(lines[1])
    assert done_ev["event"] == "implement.done"
    assert done_ev["duration_ms"] == 4500
    assert done_ev["commit"] == "abc123"


def test_event_name_per_phase(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    cases = [
        ("review-r0", "review.done"),
        ("fix-r3", "fix.done"),
        ("archive", "archive.done"),
    ]
    for phase, expected_event in cases:
        _events.phase_enter(make_args(seq=1, phase=phase))
        capsys.readouterr()
        _events.phase_exit(make_args(seq=1, phase=phase, status="done", extra="{}"))
        capsys.readouterr()
    lines = (env_setup.run_dir / "001-add-foo" / "events.jsonl").read_text().splitlines()
    # 每个 phase 2 行（start + done）
    done_events = [json.loads(l) for l in lines if ".done" in json.loads(l)["event"]]
    assert {ev["event"] for ev in done_events} == {
        "review.done",
        "fix.done",
        "archive.done",
    }


def test_failed_phase_emits_failed_event(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _events.phase_enter(make_args(seq=1, phase="implement"))
    capsys.readouterr()
    _events.phase_exit(
        make_args(seq=1, phase="implement", status="failed", extra='{"reason":"impl-crash"}')
    )
    capsys.readouterr()
    lines = (env_setup.run_dir / "001-add-foo" / "events.jsonl").read_text().splitlines()
    fail_ev = json.loads(lines[-1])
    assert fail_ev["event"] == "implement.failed"
    assert fail_ev["reason"] == "impl-crash"


def test_invalid_phase_rejected(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    with pytest.raises(SystemExit):
        _events.phase_enter(make_args(seq=1, phase="bogus"))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "invalid_phase"
