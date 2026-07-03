"""telemetry cages 统计测试。

覆盖：
- 3.1 三类笼子（有触发 / 0 触发 / 无数据）分类正确
- 3.2 --since 时间窗口过滤生效
- 3.3 空 telemetry 目录 → 全 no_data、不报错
- 3.4 CLI cli_cages handler 输出结构正确
- deletion_candidates 仅在 runs_observed >= min_runs 时出现
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from npc import telemetry as _telemetry


# ============================================================
# 辅助
# ============================================================

def _make_auto_decide_event(trigger: str, run_ts: str, ts: str) -> dict:
    return {
        "kind": "auto_decide.decision",
        "trigger": trigger,
        "run_ts": run_ts,
        "proj_key": "proj/demo",
        "ts": ts,
    }


def _recent_ts(offset_days: int = 0) -> str:
    """返回相对于现在偏移 offset_days 天的 ISO 时间戳（UTC）。"""
    dt = datetime.now(timezone.utc) - timedelta(days=offset_days)
    return dt.isoformat()


# ============================================================
# 3.1 三类笼子分类正确
# ============================================================


def test_cage_stats_triggered_and_untriggered_and_no_data(isolate_telemetry: Path):
    """stale 触发 12 次；max-rounds 有数据源但 0 次触发；routing-violation 无数据源。"""
    events = []
    # stale 触发 12 次（分布在 3 个 run）
    for i in range(12):
        run_ts = f"run-{i % 3}"
        events.append(_make_auto_decide_event("stale", run_ts, _recent_ts(i % 10)))

    # 写入 events 到 telemetry 文件
    for ev in events:
        _telemetry.emit_event(ev)

    # cage_stats：拉全部事件
    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    # stale 应计 12
    assert stats["cages"]["stale"] == 12

    # max-rounds 有数据源（auto_decide.decision 种在流里），但未出现 trigger=max-rounds → untriggered
    assert "max-rounds" in stats["untriggered"]
    assert stats["cages"]["max-rounds"] == 0

    # routing-violation no_data（事件种类未接线）
    assert "routing-violation" in stats["no_data"]
    # verify-tests-rerun 已接线至 phase.exit + outcome_reason=="rerun-tests-failed"；
    # 此处事件流里有 auto_decide.decision 但没有 phase.exit → no_data（kind 从未出现）
    assert "verify-tests-rerun" in stats["no_data"]

    # untriggered 和 no_data 互斥
    assert set(stats["untriggered"]).isdisjoint(set(stats["no_data"]))


def test_cage_stats_all_triggered(isolate_telemetry: Path):
    """当两个 auto_decide 触发器都触发时，两者都不在 untriggered 里。"""
    for trigger in ("stale", "max-rounds"):
        _telemetry.emit_event(
            _make_auto_decide_event(trigger, "run-0", _recent_ts(1))
        )

    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    assert stats["cages"]["stale"] >= 1
    assert stats["cages"]["max-rounds"] >= 1
    assert "stale" not in stats["untriggered"]
    assert "max-rounds" not in stats["untriggered"]


# ============================================================
# 3.2 时间窗口过滤（--since）
# ============================================================


def test_cage_stats_since_filters_old_events(isolate_telemetry: Path):
    """--since 30d 应过滤掉 60 天前的事件。"""
    old_ts = _recent_ts(60)   # 60 天前
    new_ts = _recent_ts(5)    # 5 天前

    # stale：1 条旧事件 + 1 条新事件
    _telemetry.emit_event(_make_auto_decide_event("stale", "run-old", old_ts))
    _telemetry.emit_event(_make_auto_decide_event("stale", "run-new", new_ts))
    # max-rounds：仅旧事件
    _telemetry.emit_event(_make_auto_decide_event("max-rounds", "run-old", old_ts))

    since_30d = datetime.now(timezone.utc) - timedelta(days=30)
    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=since_30d)

    # 30d 窗口内：stale 应为 1（新的那条）
    assert stats["cages"]["stale"] == 1

    # max-rounds 的旧事件被过滤 → 0 触发 → untriggered
    assert stats["cages"]["max-rounds"] == 0
    assert "max-rounds" in stats["untriggered"]

    # max-rounds 不在 no_data（事件种类存在于整体流中）
    assert "max-rounds" not in stats["no_data"]


def test_cage_stats_since_none_counts_all(isolate_telemetry: Path):
    """since_dt=None 时应计所有事件（不过滤）。"""
    old_ts = _recent_ts(200)
    _telemetry.emit_event(_make_auto_decide_event("stale", "run-x", old_ts))

    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)
    assert stats["cages"]["stale"] == 1


# ============================================================
# 3.3 空 telemetry 目录 → 全 no_data、不报错
# ============================================================


def test_cage_stats_empty_telemetry(isolate_telemetry: Path):
    """events.ndjson 不存在时，所有笼子归 no_data，runs_observed=0，不抛异常。"""
    # isolate_telemetry 夹具已设置独立的 NPC_TELEMETRY_ROOT 但未创建文件
    all_evts = list(_telemetry.iter_events())
    assert all_evts == []

    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    assert stats["runs_observed"] == 0
    assert stats["untriggered"] == []
    # 所有笼子应归入 no_data（包括 has_data=True 的，因为 kind 从未出现在空流中）
    assert len(stats["no_data"]) == len(_telemetry._CAGE_DEFS)
    assert stats["cages"]  # 非空字典
    for name, count in stats["cages"].items():
        assert count == 0


# ============================================================
# 3.4 CLI cli_cages handler
# ============================================================


def test_cli_cages_basic(isolate_telemetry: Path, capsys):
    """cli_cages 在有数据时输出合法 JSON 且包含所需字段。"""
    # 写 6 个 run 的 stale 事件（超过默认 min_runs=5）
    for i in range(6):
        _telemetry.emit_event(
            _make_auto_decide_event("stale", f"run-{i}", _recent_ts(i))
        )

    ns = argparse.Namespace(since=None, min_runs=None)
    _telemetry.cli_cages(ns)

    out = capsys.readouterr().out.strip()
    result = json.loads(out)

    assert result["ok"] is True
    assert "cages" in result
    assert "untriggered" in result
    assert "no_data" in result
    assert "runs_observed" in result
    assert "deletion_candidates" in result
    assert result["cages"]["stale"] == 6
    # runs_observed >= 5 → deletion_candidates 非空（max-rounds 等 0 触发笼子）
    assert len(result["deletion_candidates"]) > 0
    assert "max-rounds" in result["deletion_candidates"]


def test_cli_cages_deletion_candidates_require_min_runs(isolate_telemetry: Path, capsys):
    """runs_observed < min_runs 时 deletion_candidates 为空。"""
    # 只有 2 个 run（< 默认 min_runs=5）
    for i in range(2):
        _telemetry.emit_event(
            _make_auto_decide_event("stale", f"run-{i}", _recent_ts(i))
        )

    ns = argparse.Namespace(since=None, min_runs=None)
    _telemetry.cli_cages(ns)

    out = capsys.readouterr().out.strip()
    result = json.loads(out)
    assert result["deletion_candidates"] == []


def test_cli_cages_custom_min_runs(isolate_telemetry: Path, capsys):
    """--min-runs 1 时，即使只有 1 个 run 也列出删除候选。"""
    _telemetry.emit_event(
        _make_auto_decide_event("stale", "run-0", _recent_ts(1))
    )

    ns = argparse.Namespace(since=None, min_runs=1)
    _telemetry.cli_cages(ns)

    out = capsys.readouterr().out.strip()
    result = json.loads(out)
    assert len(result["deletion_candidates"]) > 0


def test_cli_cages_since_invalid(isolate_telemetry: Path, capsys):
    """--since 格式错误时输出错误并退出。"""
    ns = argparse.Namespace(since="bad-value", min_runs=None)
    with pytest.raises(SystemExit):
        _telemetry.cli_cages(ns)


def test_cli_cages_no_data_not_in_untriggered(isolate_telemetry: Path, capsys):
    """no_data 笼子不出现在 untriggered 也不出现在 deletion_candidates。"""
    # 不写任何 cage.routing_violation 事件（该种类从不 emit）
    # 写足够 runs 的 stale 事件触发 deletion_candidates 生效
    for i in range(10):
        _telemetry.emit_event(
            _make_auto_decide_event("stale", f"run-{i}", _recent_ts(i))
        )

    ns = argparse.Namespace(since=None, min_runs=5)
    _telemetry.cli_cages(ns)

    out = capsys.readouterr().out.strip()
    result = json.loads(out)

    # routing-violation 应在 no_data
    assert "routing-violation" in result["no_data"]
    # 不在 untriggered
    assert "routing-violation" not in result["untriggered"]
    # 不在 deletion_candidates
    assert "routing-violation" not in result["deletion_candidates"]


# ============================================================
# runs_observed 计数正确
# ============================================================


def test_runs_observed_dedup_by_run_ts(isolate_telemetry: Path):
    """同一 run_ts 的多条事件只计 1 个 run。"""
    for _ in range(5):
        _telemetry.emit_event(
            _make_auto_decide_event("stale", "run-SAME", _recent_ts(1))
        )
    for _ in range(3):
        _telemetry.emit_event(
            _make_auto_decide_event("stale", "run-OTHER", _recent_ts(2))
        )

    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    assert stats["runs_observed"] == 2  # 两个不同 run_ts
    assert stats["cages"]["stale"] == 8  # 总触发次数


# ============================================================
# F1 回归：timeout 笼子维度存在于 _CAGE_DEFS
# ============================================================


def test_cage_defs_include_timeout_dimensions():
    """_CAGE_DEFS 必须包含 timeout-budget 和 record-timeout 维度（spec/proposal 要求）。"""
    cage_names = {c["name"] for c in _telemetry._CAGE_DEFS}
    assert "timeout-budget" in cage_names, "timeout-budget 笼子缺失于 _CAGE_DEFS"
    assert "record-timeout" in cage_names, "record-timeout 笼子缺失于 _CAGE_DEFS"


def test_cage_stats_timeout_cages_appear_in_no_data(isolate_telemetry: Path):
    """timeout-budget / record-timeout 事件尚未接线，应归入 no_data 而非 untriggered。"""
    # 写入足够事件让 auto_decide.decision 出现在流里，但不 emit 任何 agent.timeout_budget 事件
    _telemetry.emit_event(_make_auto_decide_event("stale", "run-0", _recent_ts(1)))

    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    assert "timeout-budget" in stats["no_data"], "timeout-budget 应在 no_data（事件未接线）"
    assert "record-timeout" in stats["no_data"], "record-timeout 应在 no_data（事件未接线）"
    assert "timeout-budget" not in stats["untriggered"]
    assert "record-timeout" not in stats["untriggered"]


def test_cage_defs_cover_all_spec_dimensions():
    """验证 _CAGE_DEFS 覆盖 spec/tasks 中列出的全部维度。"""
    cage_names = {c["name"] for c in _telemetry._CAGE_DEFS}
    required = {
        "stale", "max-rounds", "agent-timeout-exhausted",
        "timeout-budget", "record-timeout",
        "routing-violation", "verify-tests-rerun",
    }
    missing = required - cage_names
    assert not missing, f"以下 spec 要求的笼子维度缺失：{missing}"


# ============================================================
# F2 回归：runs_observed 按 --since 窗口过滤
# ============================================================


def test_runs_observed_uses_windowed_events(isolate_telemetry: Path):
    """窗口外 run 数充足但窗口内 run 数不足时，deletion_candidates 必须为空。

    场景：10 个旧 run（> min_runs=5），近 30d 内 0 个 run。
    F2 修复前：runs_observed=10 ≥ 5 → deletion_candidates 非空（误判）。
    F2 修复后：runs_observed=0 < 5  → deletion_candidates=[]（正确）。
    """
    old_ts = _recent_ts(60)  # 60 天前，窗口外

    # 写 10 个不同旧 run 的事件（超过 min_runs 阈值）
    for i in range(10):
        _telemetry.emit_event(
            _make_auto_decide_event("stale", f"run-old-{i}", old_ts)
        )

    since_30d = datetime.now(timezone.utc) - timedelta(days=30)
    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=since_30d)

    # 窗口内 runs_observed 应为 0
    assert stats["runs_observed"] == 0, (
        f"runs_observed 应反映窗口内 run 数（0），实际得到 {stats['runs_observed']}"
    )


def test_runs_observed_windowed_vs_historical(isolate_telemetry: Path):
    """窗口内有 run 而窗口外也有更多 run 时，runs_observed 应只计窗口内的。"""
    new_ts = _recent_ts(5)    # 5 天前（窗口内）
    old_ts = _recent_ts(60)   # 60 天前（窗口外）

    # 2 个新 run（窗口内）
    for i in range(2):
        _telemetry.emit_event(
            _make_auto_decide_event("stale", f"run-new-{i}", new_ts)
        )
    # 8 个旧 run（窗口外）
    for i in range(8):
        _telemetry.emit_event(
            _make_auto_decide_event("stale", f"run-old-{i}", old_ts)
        )

    since_30d = datetime.now(timezone.utc) - timedelta(days=30)
    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=since_30d)

    # 窗口内只有 2 个 run
    assert stats["runs_observed"] == 2, (
        f"runs_observed 应为窗口内 run 数（2），实际得到 {stats['runs_observed']}"
    )


# ============================================================
# F1 回归（round-2）：verify-tests-rerun 映射到 phase.exit + outcome_reason
# ============================================================


def _make_phase_exit_event(
    outcome_reason: str | None,
    run_ts: str,
    ts: str,
    phase: str = "implement",
    status: str = "failed",
) -> dict:
    ev: dict = {
        "kind": "phase.exit",
        "phase": phase,
        "status": status,
        "run_ts": run_ts,
        "proj_key": "proj/demo",
        "ts": ts,
    }
    if outcome_reason is not None:
        ev["outcome_reason"] = outcome_reason
    return ev


def test_verify_tests_rerun_counted_when_phase_exit_rerun_failed(isolate_telemetry: Path):
    """phase.exit + outcome_reason=rerun-tests-failed → verify-tests-rerun count=1，不在 no_data / untriggered。

    回归测试：F1 修复前 verify-tests-rerun 始终 count=0 且在 no_data。
    修复后：有真实 phase.exit/rerun-tests-failed 事件时应被计入。
    """
    _telemetry.emit_event(
        _make_phase_exit_event("rerun-tests-failed", "run-A", _recent_ts(1))
    )

    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    assert stats["cages"]["verify-tests-rerun"] == 1, (
        f"verify-tests-rerun 应计 1，实际 {stats['cages']['verify-tests-rerun']}"
    )
    assert "verify-tests-rerun" not in stats["no_data"], (
        "verify-tests-rerun 不应在 no_data（phase.exit 事件已出现在流中）"
    )
    assert "verify-tests-rerun" not in stats["untriggered"], (
        "verify-tests-rerun 不应在 untriggered（count=1 > 0）"
    )


def test_verify_tests_rerun_untriggered_when_phase_exit_exists_but_no_rerun_failed(
    isolate_telemetry: Path,
):
    """phase.exit 出现但无 outcome_reason=rerun-tests-failed → verify-tests-rerun 在 untriggered，不在 no_data。"""
    _telemetry.emit_event(
        _make_phase_exit_event("commit-not-found", "run-B", _recent_ts(1))
    )

    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    assert stats["cages"]["verify-tests-rerun"] == 0
    assert "verify-tests-rerun" not in stats["no_data"], (
        "phase.exit 已出现 → verify-tests-rerun 不应在 no_data"
    )
    assert "verify-tests-rerun" in stats["untriggered"], (
        "count=0 且 kind 已出现 → verify-tests-rerun 应在 untriggered"
    )


def test_verify_tests_rerun_counts_multiple_events(isolate_telemetry: Path):
    """多条 phase.exit/rerun-tests-failed 事件（来自 implement 和 fix-rN）都计入 verify-tests-rerun。"""
    for i, phase in enumerate(("implement", "fix-r1", "fix-r2")):
        _telemetry.emit_event(
            _make_phase_exit_event("rerun-tests-failed", f"run-{i}", _recent_ts(i + 1), phase=phase)
        )
    # 一条不相关的 phase.exit 不应计入
    _telemetry.emit_event(
        _make_phase_exit_event("codex-failed", "run-X", _recent_ts(1))
    )

    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    assert stats["cages"]["verify-tests-rerun"] == 3, (
        f"三条 rerun-tests-failed 事件应计 3，实际 {stats['cages']['verify-tests-rerun']}"
    )


def test_verify_tests_rerun_no_data_when_no_phase_exit_events(isolate_telemetry: Path):
    """事件流中无任何 phase.exit 事件时，verify-tests-rerun 归 no_data（无法区分 0 触发）。"""
    # 只写 auto_decide.decision 事件，无 phase.exit
    _telemetry.emit_event(_make_auto_decide_event("stale", "run-0", _recent_ts(1)))

    all_evts = list(_telemetry.iter_events())
    stats = _telemetry.cage_stats(all_evts, since_dt=None)

    assert "verify-tests-rerun" in stats["no_data"], (
        "无 phase.exit 事件 → verify-tests-rerun 应在 no_data"
    )
