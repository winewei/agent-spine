"""``npc cost`` —— 按后端身份拆 token 成本（Claude vs MiMo/codex 等）。

目的：量化「成本路由」收益。core decision/analysis 留 Claude，coder 廉价层走 MiMo；
本命令把 telemetry 派生的 token 估算按「后端身份」分桶，让人能看到每个后端各吃了多少 token。

**只读 telemetry 派生指标**（``iter_events`` → events.ndjson），绝不碰原始 jsonl/transcript，
符合 telemetry 层「主 session 只读派生量」的设计约束。

后端身份推断（method=heuristic）::

    review.round  → record["engine"]（claude / codex）；缺失归 "review"
    其它含 tokens → 按 kind 归类：
        phase.exit / agent.spawn / 其它 → "coder"

telemetry 暂无每条 record 的 coder backend 字段（MiMo vs Claude 在 coder 层不可分），
故 coder 桶统一收口，并在输出标注 ``method: "heuristic"`` 提示口径。
"""

from __future__ import annotations

import argparse
from typing import Any, Iterable

from . import _io
from . import telemetry as _telemetry


# 桶内累加的 token / 计数字段
_BUCKET_FIELDS = ("events", "est_input_tokens", "est_output_tokens", "duration_ms")

AGGREGATE_METHOD = "heuristic"


def _new_bucket() -> dict[str, int]:
    return {f: 0 for f in _BUCKET_FIELDS}


def _bucket_for(event: dict) -> str:
    """推断一条 record 的「后端身份」桶名。

    review.round 用 ``engine``（claude/codex），缺失则归 ``review``；
    其它含 tokens 的 record 统一归 ``coder``（telemetry 暂无法区分 MiMo vs Claude）。
    """
    kind = event.get("kind")
    if kind == "review.round":
        engine = event.get("engine")
        if isinstance(engine, str) and engine.strip():
            return engine.strip()
        return "review"
    return "coder"


def _add_tokens(bucket: dict[str, int], event: dict) -> None:
    """把单条 record 的 token / duration 累加进桶。只累加确为非负 int 的值。"""
    bucket["events"] += 1
    tokens = event.get("tokens")
    if isinstance(tokens, dict):
        for field in ("est_input_tokens", "est_output_tokens"):
            v = tokens.get(field)
            if isinstance(v, int) and v >= 0:
                bucket[field] += v
    d = event.get("duration_ms")
    if isinstance(d, int) and d >= 0:
        bucket["duration_ms"] += d


def aggregate_cost(events: Iterable[dict]) -> dict[str, Any]:
    """按「后端身份」分桶聚合 token 成本（纯函数）。

    只把「含 tokens 的 record」纳入分桶——没有 token 估算的 record（如 archive.done）
    不计入任何桶，避免污染 events 计数。

    返回::

        {
            "by_bucket": {
                "claude": {events, est_input_tokens, est_output_tokens, duration_ms},
                "codex":  {...},
                "coder":  {...},
            },
            "total": {events, est_input_tokens, est_output_tokens, duration_ms},
            "method": "heuristic",
        }

    空输入 → by_bucket 为空 dict、total 全 0。
    """
    by_bucket: dict[str, dict[str, int]] = {}
    total = _new_bucket()

    for event in events:
        if not isinstance(event, dict):
            continue
        tokens = event.get("tokens")
        # 只把含 token 估算的 record 计入成本分桶
        if not isinstance(tokens, dict):
            continue
        has_token = any(
            isinstance(tokens.get(f), int) and tokens.get(f, 0) >= 0
            for f in ("est_input_tokens", "est_output_tokens")
        )
        if not has_token:
            continue

        name = _bucket_for(event)
        bucket = by_bucket.setdefault(name, _new_bucket())
        _add_tokens(bucket, event)
        _add_tokens(total, event)

    return {
        "by_bucket": by_bucket,
        "total": total,
        "method": AGGREGATE_METHOD,
    }


def _filter_events(
    events: Iterable[dict],
    *,
    since_dt,
    run_ts: str | None,
) -> list[dict]:
    """按 since（telemetry 既有解析）与 run_ts 过滤。"""
    out: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if since_dt is not None and not _telemetry._within_since(ev.get("ts", ""), since_dt):
            continue
        if run_ts is not None and ev.get("run_ts") != run_ts:
            continue
        out.append(ev)
    return out


def run(args: argparse.Namespace) -> None:
    """``npc cost [--since DUR] [--run-ts TS]``。

    按后端身份拆 token 成本。telemetry 无数据 → ok:true 且 total 全 0（不报错）。
    """
    since = getattr(args, "since", None)
    run_ts = getattr(args, "run_ts", None)

    try:
        since_dt = _telemetry._parse_since(since)
    except ValueError as e:
        _io.emit_error("invalid_since", str(e), exit_code=2)
        return

    events = _filter_events(
        _telemetry.iter_events(),
        since_dt=since_dt,
        run_ts=run_ts,
    )
    agg = aggregate_cost(events)

    payload: dict[str, Any] = {"ok": True, "since": since}
    if run_ts is not None:
        payload["run_ts"] = run_ts
    payload.update(agg)
    _io.emit(payload)
