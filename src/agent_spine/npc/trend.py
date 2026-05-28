"""Blocking trend 与 stale 检测。

update_trend：每轮 review 后追加 blocking 计数，维护 rounds_since_strict_decrease
            与 categories_seen
check_stale：判定 rounds_since_strict_decrease >= 3
"""

from __future__ import annotations

import argparse
import json

from . import _io, paths as _paths, state as _state


STALE_THRESHOLD = 3


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
