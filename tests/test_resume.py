"""resume 模块测试。"""

from __future__ import annotations

import json

import pytest

from npc import resume as _resume, state as _state


def _base_state(progress_entries: list[dict]) -> dict:
    return {
        "schema_version": 2,
        "run_ts": "2026-05-22-1545",
        "status": "in-progress",
        "mode": "interactive",
        "plan_order": [e["change_id"] for e in progress_entries],
        "progress": progress_entries,
    }


# ----------------------------- next_phase 推断 -----------------------------


def test_next_phase_no_phases_yet():
    entry = {"seq": 1, "change_id": "a", "phases": {}}
    assert _resume._next_phase_for_entry(entry) == "implement"


def test_next_phase_implement_pending():
    entry = {"phases": {"implement": {"status": "in-progress"}}}
    assert _resume._next_phase_for_entry(entry) == "implement"


def test_next_phase_after_implement_done():
    entry = {"phases": {"implement": {"status": "done"}}}
    assert _resume._next_phase_for_entry(entry) == "review-r0"


def test_next_phase_review_r0_in_progress():
    entry = {
        "phases": {
            "implement": {"status": "done"},
            "review-r0": {"status": "in-progress"},
        }
    }
    assert _resume._next_phase_for_entry(entry) == "review-r0"


def test_next_phase_review_done_with_blocking_goes_fix_r1():
    entry = {
        "phases": {
            "implement": {"status": "done"},
            "review-r0": {"status": "done", "blocking": 3},
        }
    }
    assert _resume._next_phase_for_entry(entry) == "fix-r1"


def test_next_phase_review_done_no_blocking_goes_archive():
    entry = {
        "phases": {
            "implement": {"status": "done"},
            "review-r0": {"status": "done", "blocking": 0},
        }
    }
    assert _resume._next_phase_for_entry(entry) == "archive"


def test_next_phase_fix_r2_pending_after_review_r1():
    entry = {
        "phases": {
            "implement": {"status": "done"},
            "review-r0": {"status": "done", "blocking": 3},
            "fix-r1": {"status": "done", "commit": "f1"},
            "review-r1": {"status": "done", "blocking": 2},
        }
    }
    assert _resume._next_phase_for_entry(entry) == "fix-r2"


def test_next_phase_fix_in_progress():
    entry = {
        "phases": {
            "implement": {"status": "done"},
            "review-r0": {"status": "done", "blocking": 3},
            "fix-r1": {"status": "in-progress"},
        }
    }
    assert _resume._next_phase_for_entry(entry) == "fix-r1"


def test_next_phase_archive_pending():
    entry = {
        "phases": {
            "implement": {"status": "done"},
            "review-r0": {"status": "done", "blocking": 0},
            "archive": {"status": "in-progress"},
        }
    }
    assert _resume._next_phase_for_entry(entry) == "archive"


# ----------------------------- compute_resume 整体 -----------------------------


def test_compute_resume_skips_archived():
    state = _base_state(
        [
            {"seq": 1, "change_id": "a", "status": "archived", "phases": {}},
            {
                "seq": 2,
                "change_id": "b",
                "status": "implementing",
                "phases": {"implement": {"status": "in-progress"}},
                "blocking_trend": [],
            },
            {"seq": 3, "change_id": "c", "status": "pending", "phases": {}},
        ]
    )
    info = _resume.compute_resume(state)
    assert info["needs_resume"] is True
    assert info["completed_changes"] == 1
    assert info["next_seq"] == 2
    assert info["next_change_id"] == "b"
    assert info["next_phase"] == "implement"


def test_compute_resume_all_archived():
    state = _base_state(
        [
            {"seq": 1, "change_id": "a", "status": "archived", "phases": {}},
            {"seq": 2, "change_id": "b", "status": "archived", "phases": {}},
        ]
    )
    info = _resume.compute_resume(state)
    assert info["needs_resume"] is False
    assert info.get("all_done") is True


def test_compute_resume_current_round_derivation():
    state = _base_state(
        [
            {
                "seq": 1,
                "change_id": "a",
                "status": "in-fix-loop",
                "blocking_trend": [5, 4],
                "phases": {
                    "implement": {"status": "done"},
                    "review-r0": {"status": "done", "blocking": 5},
                    "fix-r1": {"status": "done"},
                    "review-r1": {"status": "done", "blocking": 4},
                    "fix-r2": {"status": "in-progress"},
                },
            }
        ]
    )
    info = _resume.compute_resume(state)
    assert info["next_phase"] == "fix-r2"
    assert info["current_round"] == 2


# ----------------------------- find_latest_in_progress -----------------------------


def test_find_latest_in_progress_picks_only_in_progress(tmp_path, computed_paths):
    tld = computed_paths.task_log_dir
    f_done = tld / "2026-05-20-1400-plan-state.json"
    f_in_prog = tld / "2026-05-21-0900-plan-state.json"
    f_aborted = tld / "2026-05-22-0900-plan-state.json"
    f_done.write_text(json.dumps({"status": "completed"}))
    f_in_prog.write_text(json.dumps({"status": "in-progress", "run_ts": "2026-05-21-0900"}))
    f_aborted.write_text(json.dumps({"status": "aborted"}))
    found = _resume.find_latest_in_progress(tld)
    assert found is not None
    assert found.name == "2026-05-21-0900-plan-state.json"


def test_find_latest_in_progress_none(tmp_path, computed_paths):
    found = _resume.find_latest_in_progress(computed_paths.task_log_dir)
    assert found is None
