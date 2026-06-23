"""``npc auto-decide``：把 ``--auto`` 模式下的主 session 决策点统一封装。

v3 设计目标是 fire-and-forget：主 session 不再向用户问"continue/skip/abort"，
所有原本 ``needs-user-decision`` 的触发点改成调用 ``npc auto-decide``，由 npc
基于 progress 数据给出 action：``continue-retry`` / ``skip`` / ``force-archive`` /
``abort``。主 session 只负责执行 action。

设计原则：
- **action 推荐是确定性的**：同一 state + 同一 trigger 应当总返回同一个 action
- **可选 ``--apply`` 直接 mutate state**：把 status / reason / auto_retry_<trigger>
  字段直接写回，避免主 session 多发一条 ``state set-progress`` 的浪费
- **不读 git / 不调 sub-agent**：纯 state 决策；副作用只限于 state.json
"""

from __future__ import annotations

import argparse

from . import _io, paths as _paths, state as _state


VALID_TRIGGERS = {
    "stale",
    "max-rounds",
    "agent-timeout-exhausted",
    "codex-failed",
    "implementer-failed",
    "fixer-failed",
    "summary-missing",
    "commit-not-found",
}

# 软阈值；与 skill --auto 默认参数同步。可改但不接 CLI 覆盖（避免主 session 误调）
B_THRESHOLD_ARCHIVE = 2  # blocking 末值 ≤ 该值且 trend 长度 ≥ 3 → force-archive
B_THRESHOLD_OVERSIZED = 3  # blocking 末值 ≥ 该值 → 视为 oversized
NC_THRESHOLD_OVERSIZED = 6  # categories_seen 数量 ≥ 该值 → 视为 oversized
RETRY_TRIGGERS = {  # 仅这几类 trigger 走 "continue-retry → 再失败就 skip"
    "implementer-failed",
    "fixer-failed",
    "summary-missing",
    "commit-not-found",
}


def _decide(entry: dict, trigger: str) -> dict:
    """纯函数：基于 entry 与 trigger 计算 action 与 mutation 指令。"""
    blocking_trend = entry.get("blocking_trend") or []
    categories_seen = entry.get("categories_seen") or []
    b_last = int(blocking_trend[-1]) if blocking_trend else 0
    nc = len(categories_seen)

    out: dict = {"trigger": trigger}

    if trigger in ("stale", "max-rounds"):
        if b_last <= B_THRESHOLD_ARCHIVE and len(blocking_trend) >= 3:
            out.update(
                {
                    "action": "force-archive",
                    "reason": f"{trigger}-acceptable-blocking-{b_last}",
                    "set_status": None,
                }
            )
        elif b_last >= B_THRESHOLD_OVERSIZED or nc >= NC_THRESHOLD_OVERSIZED:
            out.update(
                {
                    "action": "skip",
                    "reason": "oversized-change",
                    "set_status": "skipped-auto",
                }
            )
        else:
            out.update(
                {
                    "action": "skip",
                    "reason": f"{trigger}-cannot-converge",
                    "set_status": "skipped-auto",
                }
            )
        return out

    if trigger == "agent-timeout-exhausted":
        out.update(
            {
                "action": "skip",
                "reason": "agent-timeout-exhausted-oversized",
                "set_status": "skipped-auto",
            }
        )
        return out

    if trigger == "codex-failed":
        # pipeline 已内部重试 1 次；走到 auto-decide 即视为永久失败
        out.update(
            {
                "action": "skip",
                "reason": "codex-failed-after-internal-retry",
                "set_status": "skipped-auto",
            }
        )
        return out

    # 软失败 trigger：第一次给一次 retry 机会
    retry_key = f"auto_retry_{trigger}"
    retries = int(entry.get(retry_key) or 0)
    if retries < 1:
        out.update(
            {
                "action": "continue-retry",
                "reason": f"first-auto-retry-of-{trigger}",
                "set_status": None,
                "increment_retry_key": retry_key,
            }
        )
    else:
        out.update(
            {
                "action": "skip",
                "reason": f"{trigger}-after-auto-retry",
                "set_status": "skipped-auto",
            }
        )
    return out


def cli(args: argparse.Namespace) -> None:
    """``npc auto-decide --seq N --trigger <kind> [--apply]``。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    trigger = args.trigger
    if trigger not in VALID_TRIGGERS:
        _io.emit_error(
            "invalid_trigger",
            f"--trigger={trigger} 不合法；合法集：{sorted(VALID_TRIGGERS)}",
            exit_code=2,
        )
        return

    try:
        state = _state.read_state(p.state_json)
    except FileNotFoundError as e:
        _io.emit_error("state_not_found", str(e), exit_code=3)
        return

    progress = state.get("progress") or []
    seq = args.seq
    if not (1 <= seq <= len(progress)):
        _io.emit_error(
            "seq_out_of_range",
            f"seq={seq} 超出 progress 数组长度（total={len(progress)}）",
            exit_code=1,
        )
        return
    entry = progress[seq - 1]
    decision = _decide(entry, trigger)
    decision["seq"] = seq
    decision["change_id"] = entry.get("change_id")
    decision["blocking_trend"] = entry.get("blocking_trend") or []
    decision["categories_seen"] = entry.get("categories_seen") or []

    if args.apply:
        set_status = decision.get("set_status")
        inc_key = decision.get("increment_retry_key")
        reason = decision.get("reason")

        def mutate(state: dict) -> None:
            progress = state.get("progress") or []
            entry = progress[seq - 1]
            if inc_key:
                entry[inc_key] = int(entry.get(inc_key) or 0) + 1
            if set_status:
                entry["status"] = set_status
            if reason:
                entry["reason"] = reason

        _state.update_state(p.state_json, p.state_md, mutate)
        decision["applied"] = True
    else:
        decision["applied"] = False

    decision["ok"] = True
    _io.emit(decision)
