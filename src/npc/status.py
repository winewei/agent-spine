"""npc status：只读快照当前 run 进度。

一眼看清：顶层状态、各 change 状态分布、当前正在处理的 change、每个 change 的轮次。
绝不写 state——纯读取 + 派生。

CLI handler：run
纯函数：summarize_status（便于单测）
"""

from __future__ import annotations

import argparse

from . import _io, paths as _paths, state as _state


# 终态：进入这些状态的 change 不再是"当前"处理对象。
_TERMINAL_STATUS = frozenset({"archived", "failed", "skipped-auto"})


def _rounds_for(entry: dict) -> int:
    """推断该 change 的轮次。

    优先 total_rounds；否则用 blocking_trend 长度；都没有则 0。
    """
    tr = entry.get("total_rounds")
    if isinstance(tr, int):
        return tr
    bt = entry.get("blocking_trend")
    if isinstance(bt, list):
        return len(bt)
    return 0


def summarize_status(state: dict) -> dict:
    """从 STATE_JSON dict 派生只读进度快照。

    返回结构：
        run_ts, top_status, total,
        by_status: {status: count, ...}（对 progress[].status 计数）,
        current: {seq, change_id, status} | None（第一个非终态 change）,
        changes: [{seq, change_id, status, rounds}, ...]
    """
    progress = state.get("progress") or []

    by_status: dict[str, int] = {}
    for entry in progress:
        st = entry.get("status")
        if st is None:
            continue
        by_status[st] = by_status.get(st, 0) + 1

    current: dict | None = None
    for entry in progress:
        if entry.get("status") not in _TERMINAL_STATUS:
            current = {
                "seq": entry.get("seq"),
                "change_id": entry.get("change_id"),
                "status": entry.get("status"),
            }
            break

    changes = [
        {
            "seq": entry.get("seq"),
            "change_id": entry.get("change_id"),
            "status": entry.get("status"),
            "rounds": _rounds_for(entry),
        }
        for entry in progress
    ]

    return {
        "run_ts": state.get("run_ts"),
        "top_status": state.get("status"),
        "total": len(progress),
        "by_status": by_status,
        "current": current,
        "changes": changes,
    }


def run(args: argparse.Namespace) -> None:
    """status：定位 active run → 读 STATE_JSON → emit 只读快照。

    无 active run / 定位失败 / state 文件缺失 → exit 3（env_missing）。
    """
    try:
        p = _paths.load_paths(args)
        state = _state.read_state(p.state_json)
    except (_paths.PathsError, FileNotFoundError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    _io.emit({"ok": True, **summarize_status(state)})
