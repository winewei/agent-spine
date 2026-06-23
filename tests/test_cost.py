"""``npc cost`` 测试。

覆盖：
- 纯函数 aggregate_cost：review.round engine 分桶、coder 归类、空输入、无 token record 跳过、
  脏数据健壮性
- run handler（经 NPC_TELEMETRY_ROOT 隔离的 events.ndjson）：since / run_ts 过滤、空 telemetry 不崩、
  非法 since 退出码 2

isolate_telemetry 为 autouse fixture（见 conftest），已把 NPC_TELEMETRY_ROOT 指向 tmp。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from npc import cost as _cost, telemetry as _telemetry


# ============================================================
# helpers
# ============================================================


def _tok(inp: int | None = None, out: int | None = None) -> dict:
    d: dict = {"method": "bytes_div_4"}
    if inp is not None:
        d["est_input_tokens"] = inp
    if out is not None:
        d["est_output_tokens"] = out
    return d


def _read_emit(capsys) -> dict:
    lines = capsys.readouterr().out.strip().splitlines()
    return json.loads(lines[-1])


def _seed(events: list[dict]) -> None:
    for e in events:
        _telemetry.emit_event(e)


# ============================================================
# 纯函数 aggregate_cost
# ============================================================


def test_aggregate_cost_review_engines_split():
    events = [
        {"kind": "review.round", "engine": "claude",
         "tokens": _tok(100, 50), "duration_ms": 1000},
        {"kind": "review.round", "engine": "claude",
         "tokens": _tok(200, 80), "duration_ms": 2000},
        {"kind": "review.round", "engine": "codex",
         "tokens": _tok(300, 90), "duration_ms": 3000},
    ]
    out = _cost.aggregate_cost(events)
    assert out["method"] == "heuristic"
    bb = out["by_bucket"]
    assert bb["claude"]["events"] == 2
    assert bb["claude"]["est_input_tokens"] == 300
    assert bb["claude"]["est_output_tokens"] == 130
    assert bb["claude"]["duration_ms"] == 3000
    assert bb["codex"]["events"] == 1
    assert bb["codex"]["est_input_tokens"] == 300
    assert bb["codex"]["est_output_tokens"] == 90
    # total = claude + codex
    assert out["total"]["events"] == 3
    assert out["total"]["est_input_tokens"] == 600
    assert out["total"]["est_output_tokens"] == 220
    assert out["total"]["duration_ms"] == 6000


def test_aggregate_cost_non_review_tokens_go_to_coder():
    events = [
        {"kind": "phase.exit", "phase": "implement",
         "tokens": _tok(500, 200), "duration_ms": 4000},
        {"kind": "agent.spawn", "phase": "implement",
         "tokens": _tok(100, None)},
    ]
    out = _cost.aggregate_cost(events)
    coder = out["by_bucket"]["coder"]
    assert coder["events"] == 2
    assert coder["est_input_tokens"] == 600
    assert coder["est_output_tokens"] == 200
    assert coder["duration_ms"] == 4000
    assert "claude" not in out["by_bucket"]


def test_aggregate_cost_review_missing_engine_goes_to_review_bucket():
    events = [
        {"kind": "review.round", "tokens": _tok(10, 5)},  # engine 缺失
        {"kind": "review.round", "engine": "  ", "tokens": _tok(20, 5)},  # 空白
    ]
    out = _cost.aggregate_cost(events)
    assert out["by_bucket"]["review"]["events"] == 2
    assert out["by_bucket"]["review"]["est_input_tokens"] == 30


def test_aggregate_cost_empty():
    out = _cost.aggregate_cost([])
    assert out["by_bucket"] == {}
    assert out["total"] == {
        "events": 0,
        "est_input_tokens": 0,
        "est_output_tokens": 0,
        "duration_ms": 0,
    }
    assert out["method"] == "heuristic"


def test_aggregate_cost_skips_records_without_tokens():
    events = [
        {"kind": "archive.done", "duration_ms": 999},  # 无 tokens
        {"kind": "phase.exit", "tokens": None},  # tokens=None
        {"kind": "phase.exit", "tokens": {"method": "x"}},  # tokens 无 est_*
        {"kind": "review.round", "engine": "claude", "tokens": _tok(7, 3)},
    ]
    out = _cost.aggregate_cost(events)
    # 只有最后一条计入
    assert out["total"]["events"] == 1
    assert out["total"]["est_input_tokens"] == 7
    assert list(out["by_bucket"].keys()) == ["claude"]


def test_aggregate_cost_robust_to_dirty_values():
    events = [
        "not-a-dict",  # type: ignore[list-item]
        {"kind": "review.round", "engine": "claude",
         "tokens": {"est_input_tokens": "bad", "est_output_tokens": -5}},
        {"kind": "review.round", "engine": "claude",
         "tokens": _tok(10, 4), "duration_ms": "nope"},
    ]
    out = _cost.aggregate_cost(events)  # type: ignore[arg-type]
    claude = out["by_bucket"]["claude"]
    # 第一条 review 无有效 token → 跳过；第二条 est_input=10 计入
    assert claude["events"] == 1
    assert claude["est_input_tokens"] == 10
    assert claude["est_output_tokens"] == 4
    # duration_ms 脏值被忽略
    assert claude["duration_ms"] == 0


# ============================================================
# run handler（经 events.ndjson）
# ============================================================


def test_run_aggregates_from_telemetry(isolate_telemetry: Path, capsys):
    _seed([
        {"kind": "review.round", "engine": "claude", "tokens": _tok(100, 50)},
        {"kind": "review.round", "engine": "codex", "tokens": _tok(200, 60)},
        {"kind": "phase.exit", "phase": "implement", "tokens": _tok(300, 70)},
    ])
    args = argparse.Namespace(since=None, run_ts=None)
    _cost.run(args)
    payload = _read_emit(capsys)
    assert payload["ok"] is True
    assert payload["method"] == "heuristic"
    assert payload["by_bucket"]["claude"]["est_input_tokens"] == 100
    assert payload["by_bucket"]["codex"]["est_input_tokens"] == 200
    assert payload["by_bucket"]["coder"]["est_input_tokens"] == 300
    assert payload["total"]["est_input_tokens"] == 600
    assert payload["total"]["est_output_tokens"] == 180


def test_run_empty_telemetry_total_zero(isolate_telemetry: Path, capsys):
    args = argparse.Namespace(since=None, run_ts=None)
    _cost.run(args)
    payload = _read_emit(capsys)
    assert payload["ok"] is True
    assert payload["by_bucket"] == {}
    assert payload["total"]["est_input_tokens"] == 0
    assert payload["total"]["events"] == 0


def test_run_filters_by_run_ts(isolate_telemetry: Path, capsys):
    _seed([
        {"kind": "review.round", "engine": "claude", "run_ts": "RUN-A",
         "tokens": _tok(100, 10)},
        {"kind": "review.round", "engine": "codex", "run_ts": "RUN-B",
         "tokens": _tok(999, 99)},
    ])
    args = argparse.Namespace(since=None, run_ts="RUN-A")
    _cost.run(args)
    payload = _read_emit(capsys)
    assert payload["run_ts"] == "RUN-A"
    assert payload["total"]["est_input_tokens"] == 100
    assert "codex" not in payload["by_bucket"]
    assert "claude" in payload["by_bucket"]


def test_run_filters_by_since(isolate_telemetry: Path, capsys):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=30)).isoformat()
    new_ts = (now - timedelta(hours=1)).isoformat()
    _seed([
        {"kind": "review.round", "engine": "claude", "ts": old_ts,
         "tokens": _tok(500, 50)},
        {"kind": "review.round", "engine": "codex", "ts": new_ts,
         "tokens": _tok(20, 5)},
    ])
    args = argparse.Namespace(since="7d", run_ts=None)
    _cost.run(args)
    payload = _read_emit(capsys)
    assert payload["since"] == "7d"
    # 仅 7 天内的 codex 计入
    assert payload["total"]["est_input_tokens"] == 20
    assert "codex" in payload["by_bucket"]
    assert "claude" not in payload["by_bucket"]


def test_run_invalid_since_exits_2(isolate_telemetry: Path, capsys):
    args = argparse.Namespace(since="garbage", run_ts=None)
    with pytest.raises(SystemExit) as ei:
        _cost.run(args)
    assert ei.value.code == 2
    payload = _read_emit(capsys)
    assert payload["error"] == "invalid_since"


def test_run_no_run_ts_key_when_absent(isolate_telemetry: Path, capsys):
    """未传 run_ts 时 payload 不含 run_ts 键（避免 None 噪声）。"""
    args = argparse.Namespace(since=None, run_ts=None)
    _cost.run(args)
    payload = _read_emit(capsys)
    assert "run_ts" not in payload
