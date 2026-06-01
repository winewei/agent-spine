"""Telemetry：跨 run 指标流与聚合（v1）。

设计目标（详见 docs/design.md > telemetry 章节）：

- **零侵入**：piggyback 现有 phase exit / review run / archive run / agent spawn 的出口；任何写入失败
  都被 swallow，绝不影响主流程
- **派生指标专用**：原始事件留在 ``run.events.jsonl``。这里只存优化决策必备的派生量（duration_ms /
  token 估算 / verdict / blocking_count / retry_count）与反查指针（绝对路径）
- **追加式**：``~/task_log/_telemetry/events.ndjson`` 单文件 append-only；查询走 ``jq`` 或本模块的 agg
- **可重建**：``aggregates/*.json`` 由 ``npc telemetry agg`` 全量重算；删了无伤大雅

文件布局::

    ~/task_log/_telemetry/
    ├── events.ndjson
    ├── schema-v1.json   ← 由 npc telemetry agg 首次运行时拷贝（人类参考）
    └── aggregates/
        ├── by-phase.json
        ├── by-change.json
        └── by-week.json

主 session 永远只读 ``aggregates/*.json`` 和 ``npc telemetry hotspots`` 输出；不读 events.ndjson 原文。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from . import _io


SCHEMA_VERSION = 1
TELEMETRY_DIRNAME = "_telemetry"
EVENTS_FILENAME = "events.ndjson"
SCHEMA_FILENAME = "schema-v1.json"
AGG_DIRNAME = "aggregates"

# token 估算系数：bytes / 4 ≈ tokens（OpenAI/Anthropic tokenizer 在中英混合文本上的中位数）。
# 故意不引 tiktoken，避免冷启动开销 + 第三方依赖污染。
TOKEN_BYTES_PER = 4


# ============================================================
# 路径与文件
# ============================================================


def telemetry_root(home: Path | None = None) -> Path:
    """返回 telemetry 根目录。

    优先 ``NPC_TELEMETRY_ROOT`` 环境变量（测试覆盖用）；否则 ``<home>/task_log/_telemetry``。
    """
    env = os.environ.get("NPC_TELEMETRY_ROOT")
    if env:
        return Path(env)
    h = home or Path.home()
    return h / "task_log" / TELEMETRY_DIRNAME


def events_path(home: Path | None = None) -> Path:
    return telemetry_root(home) / EVENTS_FILENAME


def schema_path(home: Path | None = None) -> Path:
    return telemetry_root(home) / SCHEMA_FILENAME


def aggregates_dir(home: Path | None = None) -> Path:
    return telemetry_root(home) / AGG_DIRNAME


def _ensure_dirs(home: Path | None = None) -> None:
    telemetry_root(home).mkdir(parents=True, exist_ok=True)
    aggregates_dir(home).mkdir(parents=True, exist_ok=True)


def _copy_schema_if_missing(home: Path | None = None) -> None:
    """首次写入时，把 telemetry_schema_v1.json 拷一份到 _telemetry/schema-v1.json。

    目的：让 meta-agent / 人类阅读者能直接在 _telemetry 目录看到字段契约。
    """
    target = schema_path(home)
    if target.exists():
        return
    src = Path(__file__).with_name("telemetry_schema_v1.json")
    if not src.is_file():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    except OSError:
        # 拷贝失败不致命；events.ndjson 主流程优先
        pass


# ============================================================
# token 估算
# ============================================================


def estimate_tokens_bytes(n_bytes: int) -> int:
    if n_bytes <= 0:
        return 0
    return max(1, n_bytes // TOKEN_BYTES_PER)


def estimate_tokens_text(text: str) -> int:
    return estimate_tokens_bytes(len(text.encode("utf-8")))


def estimate_tokens_file(p: Path | str | None) -> tuple[int | None, int | None]:
    """返回 (bytes, est_tokens)；文件不存在返回 (None, None)。"""
    if p is None:
        return None, None
    path = Path(p)
    try:
        size = path.stat().st_size
    except (OSError, ValueError):
        return None, None
    return size, estimate_tokens_bytes(size)


def _build_tokens(prompt_file: Path | str | None, output_file: Path | str | None) -> dict | None:
    """根据 prompt / output 文件路径派生 tokens 字段。

    两个都缺时返回 None；存其一则只填存在的那一侧。
    """
    pb, pt = estimate_tokens_file(prompt_file)
    ob, ot = estimate_tokens_file(output_file)
    if pb is None and ob is None:
        return None
    return {
        "prompt_bytes": pb,
        "output_bytes": ob,
        "est_input_tokens": pt,
        "est_output_tokens": ot,
        "method": f"bytes_div_{TOKEN_BYTES_PER}",
    }


# ============================================================
# 写入：emit_event（best-effort，不抛）
# ============================================================


def _now_iso() -> str:
    return _io.now_iso()


def emit_event(record: dict, *, home: Path | None = None) -> bool:
    """把一条 telemetry record 追加到 events.ndjson。

    失败时 swallow 异常，写 stderr warning，返回 False；调用方不必处理。
    record 中缺失的字段会用默认值补齐：schema_version=1 / ts=now / kind 必填。
    """
    if not isinstance(record, dict):
        return False
    kind = record.get("kind")
    if not kind:
        return False
    payload = dict(record)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("ts", _now_iso())

    try:
        _ensure_dirs(home)
        _copy_schema_if_missing(home)
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with events_path(home).open("a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError as e:
        _io.warn(f"telemetry emit failed: {e}")
        return False


# ============================================================
# 读取与聚合
# ============================================================


def iter_events(home: Path | None = None) -> Iterable[dict]:
    """惰性迭代 events.ndjson。解析失败的行跳过（不抛）。"""
    p = events_path(home)
    if not p.is_file():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _parse_iso_to_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _within_since(ts: str, since_dt: datetime | None) -> bool:
    if since_dt is None:
        return True
    dt = _parse_iso_to_dt(ts)
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= since_dt


def _parse_since(spec: str | None) -> datetime | None:
    """``--since`` 支持 ``7d`` / ``24h`` / ``30m`` / ISO 8601 时间戳。"""
    if not spec:
        return None
    spec = spec.strip()
    m = re.match(r"^(\d+)([dhm])$", spec)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        now = datetime.now(timezone.utc)
        if unit == "d":
            return now - timedelta(days=n)
        if unit == "h":
            return now - timedelta(hours=n)
        if unit == "m":
            return now - timedelta(minutes=n)
    dt = _parse_iso_to_dt(spec)
    if dt is None:
        raise ValueError(f"无法解析 --since={spec!r}（支持 Nd/Nh/Nm 或 ISO 8601）")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    vs = sorted(values)
    idx = max(0, min(len(vs) - 1, int(round((pct / 100.0) * (len(vs) - 1)))))
    return vs[idx]


def _iso_week_key(ts: str) -> str | None:
    dt = _parse_iso_to_dt(ts)
    if dt is None:
        return None
    iso = dt.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def aggregate(
    events: Iterable[dict],
    by: str,
) -> dict:
    """按维度聚合事件。返回 {key: stats} 字典。

    by ∈ {phase, change, week}。
    """
    if by not in ("phase", "change", "week"):
        raise ValueError(f"未知聚合维度：{by!r}（仅支持 phase/change/week）")

    buckets: dict[str, dict] = defaultdict(lambda: {
        "count": 0,
        "done": 0,
        "failed": 0,
        "durations_ms": [],
        "est_input_tokens": 0,
        "est_output_tokens": 0,
        "retry_count": 0,
        "blocking_total": 0,
        "review_rounds": 0,
        "kinds": defaultdict(int),
        "reasons": defaultdict(int),
        "verdicts": defaultdict(int),
    })

    for ev in events:
        if by == "phase":
            key = ev.get("phase") or ev.get("kind") or "<unknown>"
        elif by == "change":
            cid = ev.get("change_id") or "<unknown>"
            seq = ev.get("change_seq")
            key = f"{seq:03d}-{cid}" if isinstance(seq, int) else cid
        else:  # week
            key = _iso_week_key(ev.get("ts", "")) or "<unknown>"

        b = buckets[key]
        b["count"] += 1
        b["kinds"][ev.get("kind") or "<unknown>"] += 1
        status = ev.get("status")
        if status == "done":
            b["done"] += 1
        elif status == "failed":
            b["failed"] += 1
        d = ev.get("duration_ms")
        if isinstance(d, int) and d >= 0:
            b["durations_ms"].append(d)
        tokens = ev.get("tokens") or {}
        for k_in, k_out in (("est_input_tokens", "est_input_tokens"),):
            v = tokens.get(k_in)
            if isinstance(v, int):
                b[k_out] += v
        v = tokens.get("est_output_tokens")
        if isinstance(v, int):
            b["est_output_tokens"] += v
        rc = ev.get("retry_count")
        if isinstance(rc, int):
            b["retry_count"] += rc
        bc = ev.get("blocking_count")
        if isinstance(bc, int):
            b["blocking_total"] += bc
        if ev.get("kind") == "review.round":
            b["review_rounds"] += 1
        reason = ev.get("outcome_reason")
        if reason:
            b["reasons"][reason] += 1
        verdict = ev.get("verdict")
        if verdict:
            b["verdicts"][verdict] += 1

    out: dict[str, dict] = {}
    for k, b in buckets.items():
        durs = b["durations_ms"]
        out[k] = {
            "count": b["count"],
            "done": b["done"],
            "failed": b["failed"],
            "failure_rate": round(b["failed"] / b["count"], 4) if b["count"] else 0.0,
            "duration_ms": {
                "p50": _percentile(durs, 50),
                "p95": _percentile(durs, 95),
                "max": max(durs) if durs else None,
                "sum": sum(durs) if durs else 0,
            },
            "est_input_tokens_sum": b["est_input_tokens"],
            "est_output_tokens_sum": b["est_output_tokens"],
            "retry_count_sum": b["retry_count"],
            "blocking_total": b["blocking_total"],
            "review_rounds": b["review_rounds"],
            "kinds": dict(b["kinds"]),
            "reasons": dict(b["reasons"]),
            "verdicts": dict(b["verdicts"]),
        }
    return out


def hotspots(events: Iterable[dict], top: int = 5) -> list[dict]:
    """按 (failure_rate × p50_duration_ms × (1 + retry_count_sum)) 排序，给出最值得优化的前 N 个 phase。

    选 phase 维度而不是 change 维度，是因为优化目标通常是模板/CLI 流程级别，
    一次改动能惠及所有 change。
    """
    agg = aggregate(events, by="phase")
    scored: list[dict] = []
    for key, stats in agg.items():
        p50 = stats["duration_ms"]["p50"] or 0
        fr = stats["failure_rate"]
        retries = stats["retry_count_sum"]
        # 给 failure_rate 加一个常数项，避免 fr=0 时 score 永远为 0 把高 retry 的项目压住
        score = (fr + 0.1) * (p50 + 1) * (1 + retries)
        scored.append(
            {
                "phase": key,
                "score": round(score, 2),
                "count": stats["count"],
                "failure_rate": stats["failure_rate"],
                "p50_duration_ms": p50,
                "p95_duration_ms": stats["duration_ms"]["p95"],
                "retry_count_sum": retries,
                "top_reasons": _top_n_dict(stats["reasons"], 3),
                "top_verdicts": _top_n_dict(stats["verdicts"], 3),
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top]


def _top_n_dict(d: dict, n: int) -> list[tuple[str, int]]:
    return sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:n]


# ============================================================
# CLI handlers
# ============================================================


def cli_emit(args: argparse.Namespace) -> None:
    """``npc telemetry emit --kind <K> [--seq N] [--phase X] [--status S] [--extra JSON]``。

    手动写入一条 telemetry record。常用于排错；正常流程由 pipeline / events / agent 内部自动调 emit_event。
    """
    kind = args.kind
    record: dict[str, Any] = {"kind": kind}
    if args.seq is not None:
        record["change_seq"] = args.seq
    if args.change_id:
        record["change_id"] = args.change_id
    if args.phase:
        record["phase"] = args.phase
    if args.status:
        record["status"] = args.status
    if args.duration_ms is not None:
        record["duration_ms"] = args.duration_ms
    if args.proj_key:
        record["proj_key"] = args.proj_key
    if args.run_ts:
        record["run_ts"] = args.run_ts
    if args.extra:
        try:
            extra = json.loads(args.extra)
            if not isinstance(extra, dict):
                raise ValueError("--extra 必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError) as e:
            _io.emit_error("invalid_extra", f"--extra 解析失败：{e}", exit_code=2)
            return
        record.update(extra)
    # proj_key 兜底：从 cwd 推
    if "proj_key" not in record:
        try:
            from . import paths as _paths

            repo = _paths.detect_repo_root()
            record["proj_key"] = _paths.proj_key_for(repo)
        except _paths.PathsError:
            record["proj_key"] = "<unknown>"

    ok = emit_event(record)
    _io.emit({"ok": ok, "kind": kind, "path": str(events_path())})


def cli_tail(args: argparse.Namespace) -> None:
    """``npc telemetry tail [--kind K] [--last N]``。"""
    last = args.last
    kind_filter = args.kind
    rows: list[dict] = []
    for ev in iter_events():
        if kind_filter and ev.get("kind") != kind_filter:
            continue
        rows.append(ev)
    tail_rows = rows[-last:] if last and last > 0 else rows
    _io.emit({"ok": True, "count": len(tail_rows), "total": len(rows), "events": tail_rows})


def cli_agg(args: argparse.Namespace) -> None:
    """``npc telemetry agg --by phase|change|week [--since DUR] [--no-write]``。

    默认把结果同时输出到 stdout 与 ``aggregates/by-<by>.json``。
    """
    try:
        since_dt = _parse_since(args.since)
    except ValueError as e:
        _io.emit_error("invalid_since", str(e), exit_code=2)
        return

    if args.by:
        bys = [args.by]
    else:
        bys = ["phase", "change", "week"]

    out: dict[str, Any] = {"ok": True, "since": args.since, "by": {}}
    events_cached = [
        ev for ev in iter_events() if _within_since(ev.get("ts", ""), since_dt)
    ]
    for by in bys:
        agg = aggregate(events_cached, by=by)
        out["by"][by] = agg
        if not args.no_write:
            _ensure_dirs()
            target = aggregates_dir() / f"by-{by}.json"
            try:
                target.write_text(
                    json.dumps(
                        {"generated_at": _now_iso(), "since": args.since, "data": agg},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                out["by"][by + "_path"] = str(target)
            except OSError as e:
                _io.warn(f"aggregates 写入失败 {target}: {e}")
    out["events_considered"] = len(events_cached)
    _io.emit(out)


def cli_hotspots(args: argparse.Namespace) -> None:
    """``npc telemetry hotspots [--top N] [--since DUR]``。"""
    try:
        since_dt = _parse_since(args.since)
    except ValueError as e:
        _io.emit_error("invalid_since", str(e), exit_code=2)
        return
    events_filtered = [
        ev for ev in iter_events() if _within_since(ev.get("ts", ""), since_dt)
    ]
    hs = hotspots(events_filtered, top=args.top)
    _io.emit(
        {
            "ok": True,
            "since": args.since,
            "top": args.top,
            "events_considered": len(events_filtered),
            "hotspots": hs,
        }
    )


def cli_estimate_tokens(args: argparse.Namespace) -> None:
    """``npc telemetry estimate-tokens <file>``。"""
    path = Path(args.file)
    if not path.is_file():
        _io.emit_error("file_not_found", f"文件不存在：{path}", exit_code=3)
        return
    size, tokens = estimate_tokens_file(path)
    _io.emit(
        {
            "ok": True,
            "file": str(path),
            "bytes": size,
            "est_tokens": tokens,
            "method": f"bytes_div_{TOKEN_BYTES_PER}",
        }
    )


# ============================================================
# 内部钩子辅助：供 events / pipeline / agent 模块调用
# ============================================================


def emit_phase_exit(
    *,
    proj_key: str,
    run_ts: str | None,
    change_seq: int | None,
    change_id: str | None,
    phase: str,
    status: str,
    duration_ms: int | None,
    base: Path | str | None = None,
    state_json: Path | str | None = None,
    run_events: Path | str | None = None,
    outcome_reason: str | None = None,
    extra: dict | None = None,
) -> None:
    """通用 phase exit 钩子（implement / fix-rN / archive 失败路径）。

    review-rN.done 走 :func:`emit_review_round`；本函数专注 implement/fix/archive。
    所有 IO 都被 swallow。
    """
    record: dict[str, Any] = {
        "kind": "phase.exit",
        "proj_key": proj_key,
        "run_ts": run_ts,
        "change_seq": change_seq,
        "change_id": change_id,
        "phase": phase,
        "status": status,
        "duration_ms": duration_ms,
        "outcome_reason": outcome_reason,
    }
    if extra:
        record.update({k: v for k, v in extra.items() if k not in record})

    base_p = Path(base) if base else None
    summary_md = None
    prompt_md = None
    if base_p is not None:
        # 命名约定与 pipeline.record_implement / record_fix 一致
        if phase == "implement":
            summary_md = base_p / "implement.summary.md"
            prompt_md = base_p / "implement.prompt.md"
        elif phase.startswith("fix-r"):
            m = re.match(r"^fix-r(\d+)$", phase)
            if m:
                rn = m.group(1)
                summary_md = base_p / f"round-{rn}.fix.summary.md"
                prompt_md = base_p / f"round-{rn}.fix.prompt.md"
        elif phase == "archive":
            summary_md = base_p / "archive.summary.md"
    tokens = _build_tokens(prompt_md, summary_md)
    if tokens:
        record["tokens"] = tokens

    record["pointer"] = _build_pointer(
        state_json=state_json,
        run_events=run_events,
        per_change_events=(base_p / "events.jsonl") if base_p else None,
        summary_md=summary_md,
        prompt_md=prompt_md,
    )
    emit_event(record)


def emit_review_round(
    *,
    proj_key: str,
    run_ts: str | None,
    change_seq: int,
    change_id: str,
    round_n: int,
    base: Path | str,
    ok: bool,
    engine: str | None,
    verdict: str | None,
    blocking_count: int | None,
    blocking_categories: list[str] | None,
    duration_ms: int | None,
    retry_count: int,
    outcome_reason: str | None,
    state_json: Path | str | None,
    run_events: Path | str | None,
) -> None:
    """review-rN 一轮结束（成功 / 失败都调用一次）。"""
    base_p = Path(base)
    focus_md = base_p / f"round-{round_n}.focus.md"
    review_json = base_p / f"round-{round_n}.review.json"
    record: dict[str, Any] = {
        "kind": "review.round",
        "proj_key": proj_key,
        "run_ts": run_ts,
        "change_seq": change_seq,
        "change_id": change_id,
        "phase": f"review-r{round_n}",
        "round": round_n,
        "status": "done" if ok else "failed",
        "duration_ms": duration_ms,
        "verdict": verdict,
        "blocking_count": blocking_count,
        "blocking_categories": blocking_categories,
        "engine": engine,
        "retry_count": retry_count,
        "outcome_reason": outcome_reason,
        "tokens": _build_tokens(focus_md, review_json),
        "pointer": _build_pointer(
            state_json=state_json,
            run_events=run_events,
            per_change_events=base_p / "events.jsonl",
            review_json=review_json,
            focus_md=focus_md,
        ),
    }
    emit_event(record)


def emit_archive_done(
    *,
    proj_key: str,
    run_ts: str | None,
    change_seq: int,
    change_id: str,
    archive_commit: str,
    total_rounds: int,
    duration_ms: int | None,
    state_json: Path | str | None,
    run_events: Path | str | None,
    base: Path | str | None,
) -> None:
    record: dict[str, Any] = {
        "kind": "archive.done",
        "proj_key": proj_key,
        "run_ts": run_ts,
        "change_seq": change_seq,
        "change_id": change_id,
        "phase": "archive",
        "status": "done",
        "duration_ms": duration_ms,
        "archive_commit": archive_commit,
        "total_rounds": total_rounds,
        "pointer": _build_pointer(
            state_json=state_json,
            run_events=run_events,
            per_change_events=(Path(base) / "events.jsonl") if base else None,
        ),
    }
    emit_event(record)


def emit_agent_spawn(
    *,
    proj_key: str,
    run_ts: str | None,
    change_seq: int | None,
    change_id: str,
    phase: str,
    round_n: int | None,
    prompt_file: Path | str,
    state_json: Path | str | None,
) -> None:
    """agent spawn-prompt 写完引导语后的轻量埋点。"""
    pf = Path(prompt_file)
    size, est = estimate_tokens_file(pf)
    record: dict[str, Any] = {
        "kind": "agent.spawn",
        "proj_key": proj_key,
        "run_ts": run_ts,
        "change_seq": change_seq,
        "change_id": change_id,
        "phase": phase,
        "round": round_n,
        "tokens": {
            "prompt_bytes": size,
            "est_input_tokens": est,
            "output_bytes": None,
            "est_output_tokens": None,
            "method": f"bytes_div_{TOKEN_BYTES_PER}",
        }
        if size is not None
        else None,
        "pointer": _build_pointer(
            state_json=state_json,
            prompt_md=pf,
        ),
    }
    emit_event(record)


def _build_pointer(**kwargs) -> dict | None:
    """构造 pointer 字段；任何路径都转为绝对字符串；全 None 时返回 None。"""
    out: dict[str, str] = {}
    for k, v in kwargs.items():
        if v is None:
            continue
        try:
            p = Path(v).resolve()
        except (OSError, RuntimeError):
            continue
        out[k] = str(p)
    return out or None
