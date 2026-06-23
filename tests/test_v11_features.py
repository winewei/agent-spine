"""v1.1 新增功能测试：phase rotate / state repair / agent timeout 预算 / auto-decide / focus fixed history。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from npc import (
    agent as _agent,
    auto_decide as _auto_decide,
    events as _events,
    focus as _focus,
    git_chain as _git_chain,
    repair as _repair,
    state as _state,
)


# ============================================================
# 共享 bootstrap
# ============================================================


def _bootstrap(env_setup, capsys, make_args, *change_ids: str):
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def _make_commit(repo: Path, msg: str) -> str:
    (repo / f"_f_{msg}.txt").write_text(msg)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


# ============================================================
# phase rotate
# ============================================================


class TestPhaseRotate:
    def test_rotate_without_prev_inprogress(self, env_setup, capsys, make_args):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        _events.phase_rotate(
            make_args(seq=1, to_phase="fix-r1", prev_status="done", prev_extra="{}")
        )
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["ok"]
        assert payload["to_phase"] == "fix-r1"
        assert payload["prev_phases_closed"] == []
        s = json.loads(env_setup.state_json.read_text())
        ph = s["progress"][0]["phases"]["fix-r1"]
        assert ph["status"] == "in-progress"
        assert "started_at" in ph and "started_ms" in ph

    def test_rotate_closes_in_progress_phase(self, env_setup, capsys, make_args):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        _events.phase_enter(make_args(seq=1, phase="review-r0"))
        capsys.readouterr()
        _events.phase_rotate(
            make_args(seq=1, to_phase="fix-r1", prev_status="done", prev_extra="{}")
        )
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        closed = payload["prev_phases_closed"]
        assert len(closed) == 1
        assert closed[0]["phase"] == "review-r0"
        # state 验证：review-r0.done_at 应已落、fix-r1 已 in-progress
        s = json.loads(env_setup.state_json.read_text())
        assert s["progress"][0]["phases"]["review-r0"]["status"] == "done"
        assert s["progress"][0]["phases"]["review-r0"]["done_at"]
        assert s["progress"][0]["phases"]["fix-r1"]["status"] == "in-progress"

    def test_rotate_emits_phase_start_event(self, env_setup, capsys, make_args):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        _events.phase_rotate(
            make_args(seq=1, to_phase="fix-r2", prev_status="done", prev_extra="{}")
        )
        capsys.readouterr()
        per_change = env_setup.run_dir / "001-add-foo" / "events.jsonl"
        ev = json.loads(per_change.read_text().splitlines()[-1])
        assert ev["event"] == "phase.start"
        assert ev["phase"] == "fix-r2"


# ============================================================
# state repair
# ============================================================


class TestStateRepair:
    def _setup_progress_with_phantom_commits(self, env_setup, capsys, make_args, fake_repo):
        _bootstrap(env_setup, capsys, make_args, "add-foo", "add-bar")
        # 让 seq=1 假装已经 archived 但 commit 都不存在
        s = json.loads(env_setup.state_json.read_text())
        s["progress"][0].update(
            {
                "status": "archived",
                "implement_commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                "archive_commit": "cafebabecafebabecafebabecafebabecafebabe",
                "blocking_trend": [2, 0],
                "categories_seen": ["validation"],
                "total_rounds": 1,
                "phases": {
                    "implement": {
                        "status": "done",
                        "commit": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                        "duration_ms": 1000,
                    },
                    "archive": {"status": "done", "duration_ms": 100},
                },
            }
        )
        env_setup.state_json.write_text(json.dumps(s, indent=2))
        # 顺手在 base 目录里塞个文件，验证 mv 是否生效
        base_dir = Path(env_setup.run_dir / "001-add-foo")
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "implement.summary.md").write_text("stale summary")
        return fake_repo

    def test_drift_detection(self, env_setup, capsys, make_args, fake_repo):
        self._setup_progress_with_phantom_commits(env_setup, capsys, make_args, fake_repo)
        s = json.loads(env_setup.state_json.read_text())
        result = _git_chain.scan_state_drift(fake_repo, s)
        assert result["total_drifted"] == 1
        assert result["drifted_seqs"][0]["seq"] == 1
        assert "implement" in result["drifted_seqs"][0]["missing_kinds"]
        assert "archive" in result["drifted_seqs"][0]["missing_kinds"]

    def test_repair_resets_progress_and_moves_base(
        self, env_setup, capsys, make_args, fake_repo
    ):
        self._setup_progress_with_phantom_commits(env_setup, capsys, make_args, fake_repo)
        _repair.state_repair(make_args(seqs=None, auto=True))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["ok"]
        assert len(payload["repaired"]) == 1
        # progress 项被重置
        s = json.loads(env_setup.state_json.read_text())
        e = s["progress"][0]
        assert e["status"] == "pending"
        assert e["blocking_trend"] == []
        assert "implement_commit" not in e or not e.get("implement_commit")
        assert e["phases"] == {}
        # repair_log 已 append
        assert len(s.get("repair_log") or []) == 1
        # 旧 base 进 audit dir
        audit_root = env_setup.run_dir / ".repaired"
        assert audit_root.is_dir()
        archived_dirs = list(audit_root.iterdir())
        assert len(archived_dirs) == 1
        assert (archived_dirs[0] / "implement.summary.md").is_file()

    def test_repair_writes_run_event(self, env_setup, capsys, make_args, fake_repo):
        self._setup_progress_with_phantom_commits(env_setup, capsys, make_args, fake_repo)
        _repair.state_repair(make_args(seqs=None, auto=True))
        capsys.readouterr()
        run_ev_lines = env_setup.run_events.read_text().splitlines()
        repair_events = [json.loads(l) for l in run_ev_lines if "state.repair" in l]
        assert len(repair_events) == 1
        assert repair_events[0]["change_seq"] == 1

    def test_repair_with_no_drift_emits_message(
        self, env_setup, capsys, make_args, fake_repo
    ):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        _repair.state_repair(make_args(seqs=None, auto=True))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["ok"] and payload["repaired"] == []


# ============================================================
# Agent timeout budget
# ============================================================


class TestAgentTimeoutBudget:
    def test_budget_zero_retries(self, env_setup, capsys, make_args):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        _agent.timeout_budget(
            make_args(seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["timeout_sec"] == 1800
        assert payload["retries"] == 0
        assert payload["exhausted"] is False

    def test_record_increments_and_returns_new_budget(self, env_setup, capsys, make_args):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        _agent.record_timeout(
            make_args(seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["retries"] == 1
        # 1800 * 1.2 = 2160
        assert payload["next_timeout_sec"] == 2160

    def test_budget_caps_at_max(self, env_setup, capsys, make_args):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        # 强行写一个高 retries
        s = json.loads(env_setup.state_json.read_text())
        s["progress"][0]["phases"] = {"implement": {"timeout_retries": 10}}
        env_setup.state_json.write_text(json.dumps(s, indent=2))
        _agent.timeout_budget(
            make_args(seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["timeout_sec"] == 3600  # 1800 * 1.2^10 = 11146 → capped
        assert payload["exhausted"] is True

    def test_exhaustion_threshold(self, env_setup, capsys, make_args):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        s = json.loads(env_setup.state_json.read_text())
        s["progress"][0]["phases"] = {"implement": {"timeout_retries": 5}}
        env_setup.state_json.write_text(json.dumps(s, indent=2))
        _agent.timeout_budget(
            make_args(seq=1, phase="implement", base=None, mult=None, max_sec=None)
        )
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["exhausted"] is True


# ============================================================
# auto-decide
# ============================================================


class TestAutoDecide:
    def _seed(self, env_setup, capsys, make_args, **progress_patch):
        _bootstrap(env_setup, capsys, make_args, "add-foo")
        s = json.loads(env_setup.state_json.read_text())
        s["progress"][0].update(progress_patch)
        env_setup.state_json.write_text(json.dumps(s, indent=2))

    def test_stale_with_low_blocking_force_archive(self, env_setup, capsys, make_args):
        self._seed(
            env_setup,
            capsys,
            make_args,
            blocking_trend=[3, 2, 2, 2],
            categories_seen=["validation", "edge-case"],
        )
        _auto_decide.cli(make_args(seq=1, trigger="stale", apply=False))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["action"] == "force-archive"

    def test_stale_with_high_blocking_skips_oversized(self, env_setup, capsys, make_args):
        self._seed(
            env_setup,
            capsys,
            make_args,
            blocking_trend=[5, 5, 5],
            categories_seen=["race-condition"],
        )
        _auto_decide.cli(make_args(seq=1, trigger="stale", apply=False))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["action"] == "skip"
        assert payload["set_status"] == "skipped-auto"
        assert "oversized" in payload["reason"]

    def test_implementer_failed_first_retry(self, env_setup, capsys, make_args):
        self._seed(env_setup, capsys, make_args)
        _auto_decide.cli(make_args(seq=1, trigger="implementer-failed", apply=False))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["action"] == "continue-retry"

    def test_implementer_failed_second_time_skips(self, env_setup, capsys, make_args):
        self._seed(env_setup, capsys, make_args, auto_retry_implementer_failed=1)
        # 给字段加 dash key 兼容 trigger 命名
        s = json.loads(env_setup.state_json.read_text())
        s["progress"][0]["auto_retry_implementer-failed"] = 1
        env_setup.state_json.write_text(json.dumps(s, indent=2))
        _auto_decide.cli(make_args(seq=1, trigger="implementer-failed", apply=False))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["action"] == "skip"
        assert payload["set_status"] == "skipped-auto"

    def test_agent_timeout_exhausted_skip(self, env_setup, capsys, make_args):
        self._seed(env_setup, capsys, make_args)
        _auto_decide.cli(make_args(seq=1, trigger="agent-timeout-exhausted", apply=False))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["action"] == "skip"
        assert "oversized" in payload["reason"]

    def test_apply_writes_status(self, env_setup, capsys, make_args):
        self._seed(
            env_setup,
            capsys,
            make_args,
            blocking_trend=[5, 5, 5],
            categories_seen=["race-condition"],
        )
        _auto_decide.cli(make_args(seq=1, trigger="stale", apply=True))
        capsys.readouterr()
        s = json.loads(env_setup.state_json.read_text())
        assert s["progress"][0]["status"] == "skipped-auto"
        assert s["progress"][0]["reason"]


# ============================================================
# focus fixed history extraction
# ============================================================


class TestFocusFixedHistory:
    def test_extract_per_finding_resolution(self, tmp_path):
        sample = """# Fix Round 1 Summary — foo

Commit: abc123
Findings Addressed: F1, F2

## Per-Finding Resolution

- F1 (并发 flush 会落盘旧快照): 在 taskEntry 中新增 dirtyGen 版本号
- F2 (Advance 在 Close 之后): Store 新增 lifecycle RWMutex
- 这一行不是 finding 格式，应忽略

## Locations Scanned

- category=race-condition:
  - foo.go:42 (修)
"""
        base = tmp_path
        (base / "round-1.fix.summary.md").write_text(sample, encoding="utf-8")
        items = _focus.extract_fixed_history(base, up_to_round_exclusive=2)
        assert len(items) == 2
        assert items[0]["id"] == "1"
        assert items[0]["round"] == 1
        assert "并发 flush" in items[0]["title"]
        assert "dirtyGen" in items[0]["resolution"]

    def test_history_section_rendering(self):
        items = [
            {"round": 1, "id": "1", "title": "题目 A", "resolution": "做了 X"},
            {"round": 2, "id": "1", "title": "题目 B", "resolution": "做了 Y"},
        ]
        md = _focus.render_fixed_history_section(items)
        assert "Already-Fixed History" in md
        assert "[r1 F1] 题目 A → 做了 X" in md
        assert "[r2 F1] 题目 B → 做了 Y" in md

    def test_empty_history_returns_empty(self):
        assert _focus.render_fixed_history_section([]) == ""

    def test_extract_handles_missing_files(self, tmp_path):
        # 只放 round-2 的 summary，round-1 缺失
        (tmp_path / "round-2.fix.summary.md").write_text(
            "## Per-Finding Resolution\n\n- F1 (t): r\n", encoding="utf-8"
        )
        items = _focus.extract_fixed_history(tmp_path, up_to_round_exclusive=3)
        assert len(items) == 1
        assert items[0]["round"] == 2


# ============================================================
# record_fix 兜底（漏调 phase enter）
# ============================================================


class TestRecordFixPhaseEnterFallback:
    def test_record_fix_when_phase_never_entered(
        self, env_setup, capsys, make_args, fake_repo
    ):
        """模拟 v1.0 实测回归：主 session 直接 record_fix 没调 phase enter。

        record_fix 应自动补 phase enter（started_at 从 review-r(N-1).done_at 派生），
        最终 duration_ms 非 null。
        """
        from npc import pipeline as _pipeline

        _bootstrap(env_setup, capsys, make_args, "add-foo")
        # 1) implement done
        commit_impl = _make_commit(fake_repo, "implement")
        s = json.loads(env_setup.state_json.read_text())
        s["progress"][0].update(
            {
                "status": "in-fix-loop",
                "implement_commit": commit_impl,
                "phases": {
                    "implement": {"status": "done", "commit": commit_impl, "duration_ms": 1000},
                    "review-r0": {
                        "status": "done",
                        "duration_ms": 500,
                        "done_at": "2026-05-22T17:00:00+08:00",
                        "blocking": 1,
                    },
                },
            }
        )
        env_setup.state_json.write_text(json.dumps(s, indent=2))

        # 2) 写 fix.summary.md（pipeline.record_fix 会校验存在）
        base = env_setup.run_dir / "001-add-foo"
        base.mkdir(parents=True, exist_ok=True)
        summary = base / "round-1.fix.summary.md"
        summary.write_text("# Fix Round 1 Summary\n", encoding="utf-8")
        commit_fix = _make_commit(fake_repo, "fix-round-1")

        # 3) 调 record_fix（注意：没调 phase enter fix-r1）
        from npc import paths as _paths

        p = _paths.compute_paths(fake_repo, run_ts=env_setup.run_ts, home=env_setup.task_log_dir.parent.parent)
        result = _pipeline.record_fix(
            p,
            1,
            1,
            f"RESULT: commit={commit_fix} fixed=1 tests=pass summary={summary} categories_scanned=validation regressions_added=- notes=ok",
        )
        assert result["ok"], f"record_fix failed: {result}"

        # 验证 fix-r1 phase 有 started_at（自动补 enter 生效）
        s2 = json.loads(env_setup.state_json.read_text())
        fix_phase = s2["progress"][0]["phases"]["fix-r1"]
        assert fix_phase.get("started_at"), "phase 兜底 enter 失败：started_at 仍缺失"
        # duration_ms 非 None（关键回归点）
        assert fix_phase.get("duration_ms") is not None
