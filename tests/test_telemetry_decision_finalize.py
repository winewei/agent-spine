"""telemetry-auto-decide-finalize 回归测试。

覆盖 tasks.md 中：
- 3.1 auto-decide 各 trigger 决策后事件落盘、字段齐全
- 3.2 finalize merged_back=true / false 两态事件落盘
- 3.3 telemetry 目录不可写时主流程不受影响
- 3.4 pytest 全绿（文件整体通过即验证）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import auto_decide as _ad, state as _state, telemetry as _telemetry


# ============================================================
# 辅助
# ============================================================


def _read_events(isolate_telemetry: Path) -> list[dict]:
    ep = _telemetry.events_path()
    if not ep.is_file():
        return []
    return [json.loads(line) for line in ep.read_text(encoding="utf-8").splitlines() if line.strip()]


def _filter_kind(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e.get("kind") == kind]


# ============================================================
# 任务 3.1：auto-decide 决策事件落盘（各 trigger）
# ============================================================


class TestAutoDecideTelemetry:
    """验证 auto_decide.cli 在每次决策后 emit auto_decide.decision 事件。"""

    def _invoke_auto_decide(
        self,
        env_setup,
        make_args,
        capsys,
        trigger: str,
        blocking_trend: list | None = None,
        categories_seen: list | None = None,
        retries: dict | None = None,
        progress_extras: list[dict] | None = None,
    ) -> dict:
        """初始化 state，设置 progress entry，调用 auto_decide.cli，返回 stdout JSON。"""
        import argparse

        # 初始化 state
        _state.init_run(make_args(plan_order='["change-a"]'))
        capsys.readouterr()

        # 读出 state，注入所需字段
        from npc import state as _state_mod
        state = _state_mod.read_state(env_setup.state_json)
        e = state["progress"][0]
        if blocking_trend is not None:
            e["blocking_trend"] = blocking_trend
        if categories_seen is not None:
            e["categories_seen"] = categories_seen
        if retries:
            for trig, count in retries.items():
                e[f"auto_retry_{trig}"] = count
        if progress_extras:
            for extra_entry in progress_extras:
                state["progress"].append(extra_entry)
        _state_mod.write_state(env_setup.state_json, env_setup.state_md, state)
        capsys.readouterr()

        # 调用 auto_decide.cli
        args = make_args(trigger=trigger, seq=1, apply=False)
        _ad.cli(args)
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        return payload

    def test_stale_trigger_emits_event(self, env_setup, make_args, capsys, isolate_telemetry):
        """stale trigger 决策后 auto_decide.decision 事件落盘，字段齐全。"""
        payload = self._invoke_auto_decide(
            env_setup, make_args, capsys,
            trigger="stale",
            blocking_trend=[5, 4, 3],
        )
        assert payload["ok"] is True
        assert payload["action"] in ("skip", "force-archive", "abort")

        events = _read_events(isolate_telemetry)
        decision_events = _filter_kind(events, "auto_decide.decision")
        assert len(decision_events) == 1

        ev = decision_events[0]
        assert ev["trigger"] == "stale"
        assert ev["action"] == payload["action"]
        assert ev["seq"] == 1
        assert ev["change_id"] == "change-a"
        assert "reason" in ev
        assert ev["applied"] is False
        assert ev["schema_version"] == 1
        assert "ts" in ev

    def test_implementer_failed_trigger_emits_event(self, env_setup, make_args, capsys, isolate_telemetry):
        """implementer-failed trigger 决策后事件落盘，字段完整。"""
        payload = self._invoke_auto_decide(
            env_setup, make_args, capsys,
            trigger="implementer-failed",
        )
        assert payload["ok"] is True

        events = _read_events(isolate_telemetry)
        decision_events = _filter_kind(events, "auto_decide.decision")
        assert len(decision_events) == 1

        ev = decision_events[0]
        assert ev["trigger"] == "implementer-failed"
        assert ev["action"] == "continue-retry"  # 第一次给 retry 机会
        assert ev["seq"] == 1
        assert ev["change_id"] == "change-a"
        assert "reason" in ev
        assert "proj_key" in ev
        assert "run_ts" in ev

    def test_stale_force_archive_path_emits_event(self, env_setup, make_args, capsys, isolate_telemetry):
        """blocking_trend 低 + 长度≥3 → force-archive，事件落盘。"""
        payload = self._invoke_auto_decide(
            env_setup, make_args, capsys,
            trigger="stale",
            blocking_trend=[2, 1, 2],
        )
        assert payload["action"] == "force-archive"

        events = _read_events(isolate_telemetry)
        decision_events = _filter_kind(events, "auto_decide.decision")
        assert decision_events[0]["action"] == "force-archive"

    def test_codex_failed_trigger_emits_event(self, env_setup, make_args, capsys, isolate_telemetry):
        """codex-failed trigger 事件落盘。"""
        payload = self._invoke_auto_decide(
            env_setup, make_args, capsys,
            trigger="codex-failed",
        )
        assert payload["action"] == "skip"

        events = _read_events(isolate_telemetry)
        decision_events = _filter_kind(events, "auto_decide.decision")
        assert len(decision_events) == 1
        assert decision_events[0]["trigger"] == "codex-failed"

    def test_agent_timeout_trigger_emits_event(self, env_setup, make_args, capsys, isolate_telemetry):
        """agent-timeout-exhausted trigger 事件落盘。"""
        self._invoke_auto_decide(
            env_setup, make_args, capsys,
            trigger="agent-timeout-exhausted",
        )
        events = _read_events(isolate_telemetry)
        decision_events = _filter_kind(events, "auto_decide.decision")
        assert len(decision_events) == 1
        assert decision_events[0]["trigger"] == "agent-timeout-exhausted"

    def test_apply_true_reflected_in_event(self, env_setup, make_args, capsys, isolate_telemetry):
        """--apply 时，事件中 applied=True。"""
        _state.init_run(make_args(plan_order='["change-a"]'))
        capsys.readouterr()

        args = make_args(trigger="stale", seq=1, apply=True)
        _ad.cli(args)
        capsys.readouterr()

        events = _read_events(isolate_telemetry)
        decision_events = _filter_kind(events, "auto_decide.decision")
        assert len(decision_events) == 1
        assert decision_events[0]["applied"] is True

    def test_stdout_json_contract_unchanged(self, env_setup, make_args, capsys, isolate_telemetry):
        """telemetry emit 不污染 stdout JSON 契约：ok/action/trigger/seq/change_id 均在。"""
        payload = self._invoke_auto_decide(
            env_setup, make_args, capsys,
            trigger="stale",
            blocking_trend=[5, 4, 3],
        )
        assert payload["ok"] is True
        assert "action" in payload
        assert "trigger" in payload
        assert payload["seq"] == 1
        assert payload["change_id"] == "change-a"


# ============================================================
# 任务 3.2：finalize merged_back=true / false 两态事件落盘
# ============================================================


class TestFinalizeRunTelemetry:
    """验证 state.finalize 在两态下 emit run.finalize 事件。"""

    def _setup_state_all_archived(self, env_setup, make_args, capsys) -> None:
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

    def test_finalize_completed_emits_run_finalize(
        self, env_setup, make_args, capsys, isolate_telemetry
    ):
        """finalize completed 路径 emit run.finalize，merged_back=False（非 worktree 模式）。"""
        self._setup_state_all_archived(env_setup, make_args, capsys)
        _state.finalize(make_args())
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["final_status"] == "completed"

        events = _read_events(isolate_telemetry)
        finalize_events = _filter_kind(events, "run.finalize")
        assert len(finalize_events) == 1

        ev = finalize_events[0]
        assert ev["status"] == "completed"
        assert ev["merged_back"] is False  # 非 worktree 模式无 spine_branch
        assert "worktree_removed" in ev
        assert ev["archived_count"] == 2
        assert ev["total_count"] == 2
        assert ev["schema_version"] == 1
        assert "ts" in ev

    def test_finalize_with_issues_emits_run_finalize(
        self, env_setup, make_args, capsys, isolate_telemetry
    ):
        """finalize completed-with-issues 路径也 emit run.finalize。"""
        _state.init_run(make_args(plan_order='["a","b"]'))
        capsys.readouterr()
        _state.set_progress(
            make_args(
                seq=1, status="archived",
                reason=None, implement_commit=None,
                archive_commit=None, total_rounds=None, stale_verdict=None,
            )
        )
        _state.set_progress(
            make_args(
                seq=2, status="failed",
                reason=None, implement_commit=None,
                archive_commit=None, total_rounds=None, stale_verdict=None,
            )
        )
        capsys.readouterr()
        _state.finalize(make_args())
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["final_status"] == "completed-with-issues"

        events = _read_events(isolate_telemetry)
        finalize_events = _filter_kind(events, "run.finalize")
        assert len(finalize_events) == 1
        assert finalize_events[0]["status"] == "completed-with-issues"

    def test_finalize_incomplete_emits_run_finalize_with_status_incomplete(
        self, env_setup, make_args, capsys, isolate_telemetry
    ):
        """仍有非终态 change 时，incomplete 路径 emit run.finalize status=incomplete。"""
        _state.init_run(make_args(plan_order='["a"]'))
        capsys.readouterr()
        # progress[0] 保持 pending（非终态）

        with pytest.raises(SystemExit):
            _state.finalize(make_args())
        capsys.readouterr()

        events = _read_events(isolate_telemetry)
        finalize_events = _filter_kind(events, "run.finalize")
        assert len(finalize_events) == 1
        assert finalize_events[0]["status"] == "incomplete"
        assert finalize_events[0]["merged_back"] is False

    def test_finalize_merged_back_false_via_mock_runner(
        self, env_setup, make_args, capsys, isolate_telemetry, monkeypatch
    ):
        """worktree 模式 ff-merge 失败 → merged_back=False 事件落盘。"""
        # 注入 spine_branch 到 paths
        from npc import paths as _paths

        original_load = _paths.load_paths

        def mock_load(args):
            p = original_load(args)
            # 使用 replace 注入 spine_branch（Paths 是 frozen dataclass）
            import dataclasses
            return dataclasses.replace(
                p,
                spine_branch="spine/test-branch",
                canonical_repo_root=p.repo_root,
                base_branch="main",
                canonical_proj_key=p.proj_key,
            )

        monkeypatch.setattr(_paths, "load_paths", mock_load)

        self._setup_state_all_archived(env_setup, make_args, capsys)

        # runner 模拟 ff-merge 失败
        def fake_runner(cmd, **kwargs):
            import subprocess
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="ff fail")

        _state.finalize(make_args(), runner=fake_runner)
        capsys.readouterr()

        events = _read_events(isolate_telemetry)
        finalize_events = _filter_kind(events, "run.finalize")
        assert len(finalize_events) == 1
        ev = finalize_events[0]
        assert ev["merged_back"] is False
        assert ev["status"] == "completed"


# ============================================================
# 任务 3.3：telemetry 目录不可写时主流程不受影响
# ============================================================


class TestTelemetryWriteFailureTolerance:
    """验证 telemetry 写入失败不阻塞 auto_decide 与 finalize。"""

    def test_auto_decide_survives_unwritable_telemetry(
        self, env_setup, make_args, capsys, monkeypatch
    ):
        """telemetry emit 抛异常 → auto_decide.cli 仍返回正常 JSON 与 exit code。"""
        _state.init_run(make_args(plan_order='["change-a"]'))
        capsys.readouterr()

        # 让 emit_event 永远抛 OSError
        monkeypatch.setattr(_telemetry, "emit_event", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

        args = make_args(trigger="stale", seq=1, apply=False)
        # 不应抛异常，不应 SystemExit
        _ad.cli(args)
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])

        assert payload["ok"] is True
        assert "action" in payload

    def test_finalize_survives_unwritable_telemetry(
        self, env_setup, make_args, capsys, monkeypatch
    ):
        """telemetry emit 抛异常 → finalize 仍正常完成并输出 JSON。"""
        _state.init_run(make_args(plan_order='["a"]'))
        capsys.readouterr()
        _state.set_progress(
            make_args(
                seq=1, status="archived",
                reason=None, implement_commit=None,
                archive_commit=None, total_rounds=None, stale_verdict=None,
            )
        )
        capsys.readouterr()

        monkeypatch.setattr(_telemetry, "emit_event", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

        _state.finalize(make_args())
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])

        assert payload["ok"] is True
        assert payload["final_status"] == "completed"
