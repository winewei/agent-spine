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


def _next_action_hint(summary: dict, pending_decisions: list[dict]) -> str:
    """派生"下一步动作"提示（compaction 后主 session 的重定向锚点）。"""
    if pending_decisions:
        pd = pending_decisions[0]
        return (
            f"resolve pending decision: npc change run --seq {pd['seq']} "
            f"--decision <continue-retry|skip|force-archive|abort> (suggested: {pd['suggested']})"
        )
    cur = summary.get("current")
    if cur is not None:
        return f"npc change run --seq {cur['seq']}"
    if summary.get("top_status") == "in-progress":
        return "npc state finalize && npc summary render && npc index append"
    return "run finished"


def brief_status(state: dict, notes: list[dict]) -> dict:
    """--brief（v1.5，P4）：compaction/续跑后的单命令重入契约。

    在 summarize 基础上收掉 changes 全列表（那是全量视图），补三样重定向必需品：
    pending_decisions（悬而未决的裁定）、notes（未消费的编排日志/steering）、
    next_action（下一步动作提示）。
    """
    summary = summarize_status(state)
    pending_decisions = [
        {
            "seq": entry.get("seq"),
            "change_id": entry.get("change_id"),
            "trigger": (entry.get("pending_decision") or {}).get("trigger"),
            "round": (entry.get("pending_decision") or {}).get("round"),
            "suggested": (entry.get("pending_decision") or {}).get("suggested"),
        }
        for entry in (state.get("progress") or [])
        if entry.get("pending_decision")
    ]
    return {
        "run_ts": summary["run_ts"],
        "goal": state.get("goal"),
        "mode": state.get("mode"),
        "top_status": summary["top_status"],
        "total": summary["total"],
        "by_status": summary["by_status"],
        "current": summary["current"],
        "pending_decisions": pending_decisions,
        "notes": [
            {"ts": n.get("ts"), "source": n.get("source"), "text": n.get("text")}
            for n in notes
        ],
        "next_action": _next_action_hint(summary, pending_decisions),
    }


def run(args: argparse.Namespace) -> None:
    """status：定位 active run → 读 STATE_JSON → emit 只读快照。

    --brief：收掉 changes 全列表，带出 pending_decisions / 未消费 notes /
    next_action——主 session 在任何 compaction 或续跑后以此单命令重建盘面。
    无 active run / 定位失败 / state 文件缺失 → exit 3（env_missing）。
    """
    try:
        p = _paths.load_paths(args)
        state = _state.read_state(p.state_json)
    except (_paths.PathsError, FileNotFoundError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    if getattr(args, "brief", False):
        notes = _state.read_unconsumed_notes(p, state)
        _io.emit({"ok": True, **brief_status(state, notes)})
        return

    _io.emit({"ok": True, **summarize_status(state)})
