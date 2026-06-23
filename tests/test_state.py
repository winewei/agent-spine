"""state 模块测试。"""

from __future__ import annotations

import json

import pytest

from npc import paths as _paths, state as _state


# ----------------------------- 渲染纯函数 -----------------------------


def test_render_state_md_minimal():
    state = {
        "schema_version": 2,
        "run_ts": "2026-05-22-1545",
        "started_at": "2026-05-22T15:45:00+08:00",
        "last_updated_at": "2026-05-22T15:46:00+08:00",
        "mode": "interactive",
        "status": "in-progress",
        "project_root": "/repo",
        "proj_key": "-repo",
        "git_head_at_start": "abc1234",
        "cc_session": {"session_id": "sid1", "transcript_path": "/tx", "source": "hook"},
        "plan_order": ["add-foo", "add-bar"],
        "progress": [
            {
                "seq": 1,
                "change_id": "add-foo",
                "status": "archived",
                "implement_commit": "c1",
                "archive_commit": "c2",
                "total_rounds": 0,
                "blocking_trend": [],
                "categories_seen": [],
                "phases": {
                    "implement": {"status": "done", "duration_ms": 126000},
                    "review-r0": {"status": "done", "duration_ms": 75000},
                    "archive": {"status": "done", "duration_ms": 4000},
                },
                "base": "/run/001-add-foo",
            },
            {
                "seq": 2,
                "change_id": "add-bar",
                "status": "in-fix-loop",
                "blocking_trend": [5, 4, 4],
                "rounds_since_strict_decrease": 1,
                "categories_seen": ["validation"],
                "phases": {
                    "implement": {"status": "done", "duration_ms": 200000},
                    "review-r0": {"status": "done", "duration_ms": 90000},
                    "fix-r1": {"status": "done", "duration_ms": 150000},
                    "review-r1": {"status": "done", "duration_ms": 88000},
                    "fix-r2": {"status": "in-progress"},
                },
                "base": "/run/002-add-bar",
            },
        ],
    }
    md = _state.render_state_md(state)
    assert "Run 2026-05-22-1545" in md
    assert "### #1 add-foo — archived" in md
    assert "implement(2m 06s)" in md
    assert "### #2 add-bar — in-fix-loop (round 2)" in md
    assert "5 → 4 → 4" in md
    assert "Rounds Since Strict Decrease: 1" in md
    assert "validation" in md


def test_phase_sort_key_ordering():
    keys = [
        "archive",
        "review-r2",
        "fix-r1",
        "review-r0",
        "implement",
        "fix-r2",
        "review-r1",
    ]
    keys.sort(key=_state._phase_sort_key)
    assert keys == [
        "implement",
        "review-r0",
        "fix-r1",
        "review-r1",
        "fix-r2",
        "review-r2",
        "archive",
    ]


def test_fmt_duration_ms():
    assert _state._fmt_duration_ms(0) == "0s"
    assert _state._fmt_duration_ms(1500) == "1s"
    assert _state._fmt_duration_ms(60_000) == "1m 00s"
    assert _state._fmt_duration_ms(126_000) == "2m 06s"
    assert _state._fmt_duration_ms(3_661_000) == "1h 01m 01s"
    assert _state._fmt_duration_ms(None) == "?"


# ----------------------------- 原子写入 -----------------------------


def test_write_state_atomic(tmp_path):
    sj = tmp_path / "state.json"
    sm = tmp_path / "state.md"
    state = {
        "schema_version": 2,
        "run_ts": "2026-05-22-1545",
        "started_at": "2026-05-22T15:45:00+08:00",
        "mode": "interactive",
        "status": "in-progress",
        "plan_order": ["a"],
        "progress": [{"seq": 1, "change_id": "a", "status": "pending", "phases": {}}],
    }
    _state.write_state(sj, sm, state)
    assert sj.exists() and sm.exists()
    loaded = json.loads(sj.read_text())
    assert loaded["run_ts"] == "2026-05-22-1545"
    assert "last_updated_at" in loaded  # 自动注入
    assert "# New Plan Changes" in sm.read_text()


def test_update_state_mutator(tmp_path):
    sj = tmp_path / "s.json"
    sm = tmp_path / "s.md"
    _state.write_state(
        sj,
        sm,
        {
            "schema_version": 2,
            "run_ts": "x",
            "mode": "interactive",
            "status": "in-progress",
            "plan_order": ["a"],
            "progress": [{"seq": 1, "change_id": "a", "status": "pending", "phases": {}}],
        },
    )

    def mutator(s):
        s["progress"][0]["status"] = "archived"

    out = _state.update_state(sj, sm, mutator)
    assert out["progress"][0]["status"] == "archived"
    assert json.loads(sj.read_text())["progress"][0]["status"] == "archived"


# ----------------------------- CLI handlers -----------------------------


def test_init_run_creates_files(env_setup, capsys, make_args):
    args = make_args(plan_order='["add-foo","add-bar"]')
    _state.init_run(args)
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["total_changes"] == 2

    s = json.loads(env_setup.state_json.read_text())
    assert s["schema_version"] == 2
    assert s["plan_order"] == ["add-foo", "add-bar"]
    assert len(s["progress"]) == 2
    assert s["progress"][0]["status"] == "pending"
    assert env_setup.state_md.exists()
    assert env_setup.run_events.exists()


def test_init_run_refuses_overwrite(env_setup, capsys, make_args):
    args = make_args(plan_order='["a"]')
    _state.init_run(args)
    capsys.readouterr()  # drain
    # 第二次应失败
    with pytest.raises(SystemExit):
        _state.init_run(make_args(plan_order='["b"]'))
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "state_already_exists"


def test_get_jq_path(env_setup, capsys, make_args):
    _state.init_run(make_args(plan_order='["add-foo","add-bar"]'))
    capsys.readouterr()
    _state.get(make_args(jq_path=".plan_order | length"))
    out = capsys.readouterr().out.strip()
    assert out == "2"


def test_add_change_sequential(env_setup, capsys, make_args):
    _state.init_run(make_args(plan_order='["add-foo","add-bar"]'))
    capsys.readouterr()
    _state.add_change(make_args(seq=1, change_id="add-foo", base=None))
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["base"].endswith("001-add-foo")
    from pathlib import Path

    assert Path(payload["base"]).is_dir()


def test_add_change_mismatch_rejected(env_setup, capsys, make_args):
    _state.init_run(make_args(plan_order='["add-foo"]'))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        _state.add_change(make_args(seq=1, change_id="WRONG-ID", base=None))
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["error"] == "seq_invalid"


def test_set_progress_updates_fields(env_setup, capsys, make_args):
    _state.init_run(make_args(plan_order='["a"]'))
    capsys.readouterr()
    _state.set_progress(
        make_args(
            seq=1,
            status="implementing",
            reason=None,
            implement_commit=None,
            archive_commit=None,
            total_rounds=None,
            stale_verdict=None,
        )
    )
    s = json.loads(env_setup.state_json.read_text())
    assert s["progress"][0]["status"] == "implementing"
    assert s["progress"][0].get("started_at") is not None


def test_set_progress_invalid_status_rejected(env_setup, capsys, make_args):
    _state.init_run(make_args(plan_order='["a"]'))
    capsys.readouterr()
    with pytest.raises(SystemExit):
        _state.set_progress(
            make_args(
                seq=1,
                status="bogus",
                reason=None,
                implement_commit=None,
                archive_commit=None,
                total_rounds=None,
                stale_verdict=None,
            )
        )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "invalid_status"


def test_finalize_all_archived(env_setup, capsys, make_args):
    _state.init_run(make_args(plan_order='["a","b"]'))
    capsys.readouterr()
    for seq in (1, 2):
        _state.set_progress(
            make_args(
                seq=seq,
                status="archived",
                reason=None,
                implement_commit=None,
                archive_commit=None,
                total_rounds=None,
                stale_verdict=None,
            )
        )
    capsys.readouterr()
    _state.finalize(make_args())
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["final_status"] == "completed"


def test_finalize_with_failed_yields_with_issues(env_setup, capsys, make_args):
    _state.init_run(make_args(plan_order='["a","b"]'))
    capsys.readouterr()
    _state.set_progress(
        make_args(
            seq=1,
            status="archived",
            reason=None,
            implement_commit=None,
            archive_commit=None,
            total_rounds=None,
            stale_verdict=None,
        )
    )
    _state.set_progress(
        make_args(
            seq=2,
            status="failed",
            reason=None,
            implement_commit=None,
            archive_commit=None,
            total_rounds=None,
            stale_verdict=None,
        )
    )
    capsys.readouterr()
    _state.finalize(make_args())
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["final_status"] == "completed-with-issues"


def test_finalize_blocked_by_needs_decision(env_setup, capsys, make_args):
    _state.init_run(make_args(plan_order='["a"]'))
    capsys.readouterr()
    _state.set_progress(
        make_args(
            seq=1,
            status="needs-user-decision",
            reason="stale",
            implement_commit=None,
            archive_commit=None,
            total_rounds=None,
            stale_verdict=None,
        )
    )
    capsys.readouterr()
    with pytest.raises(SystemExit):
        _state.finalize(make_args())
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "has_needs_decision"
