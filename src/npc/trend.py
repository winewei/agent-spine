"""Blocking trend 与 stale 检测。

update_trend：每轮 review 后追加 blocking 计数，维护 rounds_since_strict_decrease
            与 categories_seen
check_stale：判定 rounds_since_strict_decrease >= 3
"""

from __future__ import annotations

import argparse
import json
import re

from . import _io, paths as _paths, state as _state


STALE_THRESHOLD = 3

_REVIEW_PHASE_RE = re.compile(r"^review-r(\d+)$")
_FIX_PHASE_RE = re.compile(r"^fix-r(\d+)$")


# ============================================================
# 连续同 category 计数 + 复现判定（change fix-prompt-exhaustive-sweep）
#
# 两个纯函数均只读 ``entry["phases"]``（review-rN 的 blocking ``categories`` 与
# fix-rN 的自报 ``categories_scanned``），MUST NOT 打开任何 round-*.review.json，
# MUST NOT 落盘任何新 state 字段。coder.py / agent.py / pipeline.py / spec_report.py
# 各消费点统一调用这两个函数现场重算（design D1/D2/D5）。
# ============================================================


def _parse_scanned_csv(raw: object) -> set[str]:
    """把 fix-rN 自报的 ``categories_scanned`` csv 字符串拆成去空/去 ``-`` 的 category 集合。

    None / 空 / ``-`` 均返回空集（自报缺失，见 spec「自报缺失不产生复现判定」）。
    """
    if raw is None:
        return set()
    s = str(raw).strip()
    if not s or s == "-":
        return set()
    return {c.strip() for c in s.split(",") if c.strip()}


def _review_rounds_ordered(phases: dict) -> list[tuple[int, set[str]]]:
    """按轮次升序返回每个 review-rN 的 blocking category 集合。"""
    out: list[tuple[int, set[str]]] = []
    for k, v in (phases or {}).items():
        m = _REVIEW_PHASE_RE.match(k)
        if not m:
            continue
        cats = (v or {}).get("categories") or []
        out.append((int(m.group(1)), {c for c in cats if c}))
    out.sort(key=lambda t: t[0])
    return out


def _fix_scanned_ordered(phases: dict) -> list[tuple[int, object]]:
    """按轮次升序返回每个 fix-rN 的原始 ``categories_scanned`` 自报值（含缺失项）。

    保留原始值（含 None）以便区分「未提供自报」与「提供了空自报」——两者都不产生
    复现判定，但语义上都由 :func:`_parse_scanned_csv` 归一化为空集。
    """
    out: list[tuple[int, object]] = []
    for k, v in (phases or {}).items():
        m = _FIX_PHASE_RE.match(k)
        if not m:
            continue
        out.append((int(m.group(1)), (v or {}).get("categories_scanned")))
    out.sort(key=lambda t: t[0])
    return out


def category_streaks(phases: dict) -> dict[str, int]:
    """对每个在最近一轮 review 中被判 blocking 的 category，从最新轮向前逐轮追溯
    连续出现轮数（逐轮不中断，缺席即停止并清零；OQ2）。

    只读 ``entry["phases"]`` 中已落盘的逐轮 review ``categories``，MUST NOT 打开任何
    round-*.review.json。无 review 轮次时返回空 dict。
    """
    rounds = _review_rounds_ordered(phases)
    if not rounds:
        return {}
    latest_cats = rounds[-1][1]
    result: dict[str, int] = {}
    for c in latest_cats:
        streak = 0
        for _n, cats in reversed(rounds):
            if c in cats:
                streak += 1
            else:
                break
        result[c] = streak
    return result


def recurred_categories(phases: dict) -> list[dict]:
    """判定每个 fix-rN 自报 ``categories_scanned`` 之后是否出现同 category 复现。

    对 fix-rN 自报中的 category c，若存在时序上晚于 fix-rN 的某轮 review-rM（M ≥ N）
    再次将 c 判为 blocking，即记一次复现 ``{category, claimed_at_round: N,
    recurred_at_round: M}``（取满足条件的最小 M，即最早复现轮）。

    ``review-r(N-1)``（触发该自报的那一轮，时序早于 fix-rN）MUST NOT 被计入证据——
    这里以 ``M ≥ N`` 精确排除它。自报缺失/为空的 fix 轮不产生复现判定。

    输出按 ``(claimed_at_round, category, recurred_at_round)`` 稳定排序。纯函数，
    不落盘（design D1/D5）。
    """
    review_by_round = {n: cats for n, cats in _review_rounds_ordered(phases)}
    out: list[dict] = []
    for n, raw in _fix_scanned_ordered(phases):
        for c in _parse_scanned_csv(raw):
            ms = sorted(m for m, cats in review_by_round.items() if m >= n and c in cats)
            if ms:
                out.append(
                    {"category": c, "claimed_at_round": n, "recurred_at_round": ms[0]}
                )
    out.sort(key=lambda d: (d["claimed_at_round"], d["category"], d["recurred_at_round"]))
    return out


def recurred_category_names(phases: dict) -> list[str]:
    """去重排序后的复现 category 名列表（供 fix prompt 强制穷举集合消费）。"""
    return sorted({d["category"] for d in recurred_categories(phases)})


def _next_rounds_since_decrease(trend: list[int], new_value: int) -> int:
    """根据新值更新 rounds_since_strict_decrease 计数。

    - 首轮（trend 为空）→ 0
    - 严格下降 → 0
    - 持平或上升 → prev_count + 1
    """
    if not trend:
        return 0
    prev = trend[-1]
    if new_value < prev:
        return 0
    return 0  # 这里在 caller 处与旧值合并；保留接口形态


def update_trend(args: argparse.Namespace) -> None:
    """review update-trend <seq> --metrics <json>。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        metrics = json.loads(args.metrics)
        if not isinstance(metrics, dict):
            raise ValueError("metrics 必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as e:
        _io.emit_error("invalid_metrics", f"--metrics 解析失败：{e}", exit_code=2)
        return

    blocking = metrics.get("blocking")
    if not isinstance(blocking, int):
        _io.emit_error("invalid_metrics", "metrics.blocking 必须是整数", exit_code=2)
        return
    new_categories = metrics.get("categories") or []
    if not isinstance(new_categories, list):
        _io.emit_error("invalid_metrics", "metrics.categories 必须是数组", exit_code=2)
        return

    captured: dict = {}

    def mutate(state: dict) -> None:
        progress = state.get("progress") or []
        if not (1 <= args.seq <= len(progress)):
            raise ValueError(f"seq={args.seq} 超出 progress 长度（total={len(progress)}）")
        entry = progress[args.seq - 1]
        trend = list(entry.get("blocking_trend") or [])

        if not trend:
            new_rsd = 0
        else:
            prev = trend[-1]
            prev_rsd = int(entry.get("rounds_since_strict_decrease") or 0)
            if blocking < prev:
                new_rsd = 0
            else:
                new_rsd = prev_rsd + 1

        trend.append(blocking)

        seen = list(entry.get("categories_seen") or [])
        seen_set = set(seen)
        for c in new_categories:
            if c and c not in seen_set:
                seen_set.add(c)
                seen.append(c)

        entry["blocking_trend"] = trend
        entry["rounds_since_strict_decrease"] = new_rsd
        entry["categories_seen"] = seen

        captured["blocking_trend"] = trend
        captured["rounds_since_strict_decrease"] = new_rsd
        captured["categories_seen"] = seen

    try:
        _state.update_state(p.state_json, p.state_md, mutate)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return
    except FileNotFoundError:
        _io.emit_error("state_not_found", f"STATE_JSON 不存在：{p.state_json}", exit_code=3)
        return

    _io.emit({"ok": True, **captured})


def check_stale(args: argparse.Namespace) -> None:
    """review check-stale <seq>。"""
    try:
        p = _paths.load_paths(args)
        state = _state.read_state(p.state_json)
    except (_paths.PathsError, FileNotFoundError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    progress = state.get("progress") or []
    if not (1 <= args.seq <= len(progress)):
        _io.emit_error(
            "seq_out_of_range",
            f"seq={args.seq} 超出 progress 长度（total={len(progress)}）",
            exit_code=1,
        )
        return

    entry = progress[args.seq - 1]
    rsd = int(entry.get("rounds_since_strict_decrease") or 0)
    trend = entry.get("blocking_trend") or []
    stale = rsd >= STALE_THRESHOLD

    _io.emit(
        {
            "stale": stale,
            "rounds_since_strict_decrease": rsd,
            "blocking_trend": trend,
            "threshold": STALE_THRESHOLD,
        }
    )
