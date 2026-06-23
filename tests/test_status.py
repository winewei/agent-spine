"""status 模块测试。

纯函数 summarize_status 直接喂构造好的 STATE_JSON dict；handler 层通过
monkeypatch load_paths / read_state 验证 emit + 退出码。
"""

from __future__ import annotations

import json

import pytest

from npc import paths as _paths, status as _status


# ----------------------------- 构造工具 -----------------------------


def _entry(seq, change_id, status, **extra):
    e = {"seq": seq, "change_id": change_id, "status": status}
    e.update(extra)
    return e


def _state(progress, *, run_ts="2026-05-22-1545", top_status="in-progress", plan_order=None):
    return {
        "run_ts": run_ts,
        "status": top_status,
        "plan_order": plan_order if plan_order is not None else [p["change_id"] for p in progress],
        "progress": progress,
    }


# ----------------------------- summarize_status -----------------------------


def test_summarize_empty_plan():
    out = _status.summarize_status(_state([], plan_order=[]))
    assert out["run_ts"] == "2026-05-22-1545"
    assert out["top_status"] == "in-progress"
    assert out["total"] == 0
    assert out["by_status"] == {}
    assert out["current"] is None
    assert out["changes"] == []


def test_summarize_all_pending_current_is_first():
    out = _status.summarize_status(
        _state(
            [
                _entry(1, "add-foo", "pending"),
                _entry(2, "add-bar", "pending"),
            ]
        )
    )
    assert out["total"] == 2
    assert out["by_status"] == {"pending": 2}
    assert out["current"] == {"seq": 1, "change_id": "add-foo", "status": "pending"}
    assert out["changes"] == [
        {"seq": 1, "change_id": "add-foo", "status": "pending", "rounds": 0},
        {"seq": 2, "change_id": "add-bar", "status": "pending", "rounds": 0},
    ]


def test_summarize_mixed_current_is_first_non_terminal():
    out = _status.summarize_status(
        _state(
            [
                _entry(1, "a", "archived"),
                _entry(2, "b", "reviewing"),
                _entry(3, "c", "failed"),
                _entry(4, "d", "pending"),
            ]
        )
    )
    assert out["total"] == 4
    assert out["by_status"] == {"archived": 1, "reviewing": 1, "failed": 1, "pending": 1}
    # 第一个非终态是 #2 reviewing
    assert out["current"] == {"seq": 2, "change_id": "b", "status": "reviewing"}


def test_summarize_all_terminal_current_none():
    out = _status.summarize_status(
        _state(
            [
                _entry(1, "a", "archived"),
                _entry(2, "b", "failed"),
                _entry(3, "c", "skipped-auto"),
            ],
            top_status="completed-with-issues",
        )
    )
    assert out["current"] is None
    assert out["by_status"] == {"archived": 1, "failed": 1, "skipped-auto": 1}
    assert out["top_status"] == "completed-with-issues"


def test_summarize_rounds_from_total_rounds():
    out = _status.summarize_status(
        _state([_entry(1, "a", "archived", total_rounds=3)])
    )
    assert out["changes"][0]["rounds"] == 3


def test_summarize_rounds_from_blocking_trend_length():
    # 无 total_rounds，从 blocking_trend 长度推：[5,4,4] → 3
    out = _status.summarize_status(
        _state([_entry(1, "a", "in-fix-loop", blocking_trend=[5, 4, 4])])
    )
    assert out["changes"][0]["rounds"] == 3
    assert out["current"] == {"seq": 1, "change_id": "a", "status": "in-fix-loop"}


def test_summarize_total_rounds_takes_priority_over_trend():
    out = _status.summarize_status(
        _state([_entry(1, "a", "archived", total_rounds=1, blocking_trend=[5, 4, 4])])
    )
    assert out["changes"][0]["rounds"] == 1


def test_summarize_rounds_default_zero():
    out = _status.summarize_status(_state([_entry(1, "a", "pending")]))
    assert out["changes"][0]["rounds"] == 0


def test_summarize_missing_top_fields_safe():
    # 极简 dict，缺 run_ts/status/plan_order
    out = _status.summarize_status({"progress": [_entry(1, "a", "pending")]})
    assert out["run_ts"] is None
    assert out["top_status"] is None
    assert out["total"] == 1
    assert out["current"]["change_id"] == "a"


def test_summarize_needs_user_decision_is_non_terminal():
    out = _status.summarize_status(
        _state(
            [
                _entry(1, "a", "archived"),
                _entry(2, "b", "needs-user-decision"),
            ]
        )
    )
    # needs-user-decision 不在终态集，应为 current
    assert out["current"] == {"seq": 2, "change_id": "b", "status": "needs-user-decision"}


# ----------------------------- handler: run -----------------------------


class _FakePaths:
    def __init__(self, state_json):
        self.state_json = state_json


def test_run_emits_summary_and_exit_zero(monkeypatch, capsys, tmp_path):
    sj = tmp_path / "state.json"
    state = _state(
        [
            _entry(1, "a", "archived", total_rounds=0),
            _entry(2, "b", "reviewing", blocking_trend=[5, 4]),
        ]
    )
    sj.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(_status._paths, "load_paths", lambda args: _FakePaths(sj))

    import argparse

    _status.run(argparse.Namespace())
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["total"] == 2
    assert payload["by_status"] == {"archived": 1, "reviewing": 1}
    assert payload["current"] == {"seq": 2, "change_id": "b", "status": "reviewing"}
    assert payload["run_ts"] == "2026-05-22-1545"
    assert payload["changes"][1]["rounds"] == 2


def test_run_no_active_run_exit_three(monkeypatch, capsys):
    def _boom(args):
        raise _paths.PathsError("未能定位当前 run")

    monkeypatch.setattr(_status._paths, "load_paths", _boom)

    import argparse

    with pytest.raises(SystemExit) as exc:
        _status.run(argparse.Namespace())
    assert exc.value.code == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "env_missing"


def test_run_state_file_missing_exit_three(monkeypatch, capsys, tmp_path):
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(_status._paths, "load_paths", lambda args: _FakePaths(missing))

    import argparse

    with pytest.raises(SystemExit) as exc:
        _status.run(argparse.Namespace())
    assert exc.value.code == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "env_missing"


def test_run_is_read_only(monkeypatch, capsys, tmp_path):
    sj = tmp_path / "state.json"
    state = _state([_entry(1, "a", "pending")])
    original = json.dumps(state)
    sj.write_text(original, encoding="utf-8")
    before = sj.stat().st_mtime_ns

    monkeypatch.setattr(_status._paths, "load_paths", lambda args: _FakePaths(sj))

    import argparse

    _status.run(argparse.Namespace())
    capsys.readouterr()
    # 文件内容与 mtime 不变
    assert sj.read_text(encoding="utf-8") == original
    assert sj.stat().st_mtime_ns == before
