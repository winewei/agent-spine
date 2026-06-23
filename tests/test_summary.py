"""summary 模块测试。"""

from __future__ import annotations

import json

from npc import state as _state, summary as _summary


def _stuffed_state(run_ts="2026-05-22-1430", status="completed"):
    return {
        "schema_version": 2,
        "run_ts": run_ts,
        "started_at": "2026-05-22T14:30:00+08:00",
        "last_updated_at": "2026-05-22T17:02:00+08:00",
        "mode": "auto",
        "status": status,
        "project_root": "/repo",
        "proj_key": "-repo",
        "cc_session": {"session_id": "sid-x", "transcript_path": "/tx", "source": "hook"},
        "plan_order": ["a", "b", "c"],
        "progress": [
            {
                "seq": 1,
                "change_id": "a",
                "status": "archived",
                "implement_commit": "c1",
                "archive_commit": "ca",
                "blocking_trend": [],
                "categories_seen": [],
                "phases": {
                    "implement": {"status": "done", "commit": "c1", "duration_ms": 60000},
                    "review-r0": {"status": "done", "duration_ms": 30000},
                    "archive": {"status": "done", "duration_ms": 2000},
                },
            },
            {
                "seq": 2,
                "change_id": "b",
                "status": "archived",
                "implement_commit": "c2",
                "archive_commit": "cb",
                "blocking_trend": [5, 2, 0],
                "categories_seen": ["validation", "concurrency"],
                "phases": {
                    "implement": {"status": "done", "commit": "c2", "duration_ms": 200000},
                    "review-r0": {"status": "done", "duration_ms": 90000},
                    "fix-r1": {"status": "done", "commit": "f1", "duration_ms": 150000},
                    "review-r1": {"status": "done", "duration_ms": 80000},
                    "fix-r2": {"status": "done", "commit": "f2", "duration_ms": 400000},
                    "review-r2": {"status": "done", "duration_ms": 80000},
                    "archive": {"status": "done", "duration_ms": 3000},
                },
            },
            {
                "seq": 3,
                "change_id": "c",
                "status": "failed",
                "reason": "commit-chain-broken",
                "implement_commit": "c3",
                "blocking_trend": [3, 2],
                "categories_seen": ["validation"],
                "phases": {
                    "implement": {"status": "done", "commit": "c3", "duration_ms": 70000},
                    "review-r0": {"status": "done", "duration_ms": 60000},
                    "fix-r1": {"status": "done", "commit": "f3", "duration_ms": 100000},
                    "review-r1": {"status": "done", "duration_ms": 70000},
                },
            },
        ],
    }


def test_render_summary_includes_key_sections():
    md = _summary.render_summary(_stuffed_state())
    assert "Run Summary — 2026-05-22-1430" in md
    assert "## Totals" in md
    assert "Total changes: 3" in md
    assert "Archived: 2" in md
    assert "Failed: 1 (#3 c — commit-chain-broken)" in md
    assert "## Phase Duration Top 5" in md
    # 最长 phase 是 fix-r2 400s = 6m 40s
    assert "fix-r2" in md
    assert "## Commit Chain" in md
    assert "#1 a: implement=c1, archive=ca" in md
    assert "#2 b: implement=c2, fix-r1=f1, fix-r2=f2, archive=cb" in md
    assert "## Failed / Needs Decision" in md
    assert "## Categories Distribution" in md
    assert "validation:" in md and "concurrency:" in md


def test_render_index_record_shape():
    rec = _summary.render_index_record(_stuffed_state())
    assert rec["status"] == "completed"
    assert rec["total_changes"] == 3
    assert rec["archived"] == 2
    assert rec["failed"] == 1
    assert rec["session_id"] == "sid-x"
    assert len(rec["changes"]) == 3
    c2 = rec["changes"][1]
    assert c2["change_id"] == "b"
    assert c2["rounds"] == 2  # 由 phases 中最大 fix/review-rN 派生
    assert c2["categories"] == ["validation", "concurrency"]


def test_render_handler_writes_file(env_setup, capsys, make_args):
    _state.write_state(env_setup.state_json, env_setup.state_md, _stuffed_state())
    _summary.render(make_args())
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    out_path = env_setup.run_dir / "run-summary.md"
    assert out_path.exists()
    assert "Run Summary" in out_path.read_text()


def test_index_append_handler(env_setup, capsys, make_args):
    _state.write_state(env_setup.state_json, env_setup.state_md, _stuffed_state())
    _summary.index_append(make_args())
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert env_setup.index_file.exists()
    line = env_setup.index_file.read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["status"] == "completed"
    assert rec["total_changes"] == 3
