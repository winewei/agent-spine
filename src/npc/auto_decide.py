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

from . import _io, paths as _paths, state as _state, telemetry as _telemetry


VALID_TRIGGERS = {
    "stale",
    "max-rounds",
    "agent-timeout-exhausted",
    "codex-failed",
    "implementer-failed",
    "fixer-failed",
    "summary-missing",
    "commit-not-found",
    "archive-failed",
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
    "archive-failed",
}

# 系统性阻塞止损阈值（不接 CLI 覆盖）
SYSTEMIC_TRIGGER_CONSECUTIVE = 3   # 同一 trigger 连续出现 ≥ 该值 → abort
SYSTEMIC_SKIP_RATIO = 0.5          # skipped-auto 占比 ≥ 该值 → abort（须同时满足最小数量）
SYSTEMIC_SKIP_MIN = 3              # 触发比例判断的最小 skipped-auto 数量


def _is_systemic_block(progress: list[dict], trigger: str) -> bool:
    """检测跨 change 维度的系统性阻塞。

    两条规则（任一成立即返回 True）：
    1. 同一 trigger 在 progress 中**连续**最后 N 项（已完成决策的）≥ SYSTEMIC_TRIGGER_CONSECUTIVE。
    2. skipped-auto 状态的 change 占总进度 ≥ SYSTEMIC_SKIP_RATIO 且绝对数量 ≥ SYSTEMIC_SKIP_MIN。

    progress 中状态为 pending / implementing / reviewing 的 entry 不计入连续计数，
    因为它们尚未触发 auto-decide，不代表已确认的阻塞事件。
    """
    if not progress:
        return False

    # 规则 1：同一 trigger 连续出现
    # 取已触发过 auto-decide 的 entry（有 last_trigger 字段），从末尾往前数。
    # 当前这次 trigger 本身计为第 1 次，加上之前已记录的连续数。
    decided = [e for e in progress if e.get("last_trigger")]
    consecutive = 1  # 当前决策点本身计入
    for e in reversed(decided):
        entry_trigger = e.get("last_trigger") or ""
        if entry_trigger == trigger:
            consecutive += 1
        else:
            break
    if consecutive >= SYSTEMIC_TRIGGER_CONSECUTIVE:
        return True

    # 规则 2：skipped-auto 占比
    skipped = sum(1 for e in progress if e.get("status") == "skipped-auto")
    total = len(progress)
    if skipped >= SYSTEMIC_SKIP_MIN and skipped / total >= SYSTEMIC_SKIP_RATIO:
        return True

    return False


def _decide(entry: dict, trigger: str, progress: list[dict] | None = None) -> dict:
    """纯函数：基于 entry 与 trigger 计算 action 与 mutation 指令。

    progress 为全量 progress 列表（可选），用于跨 change 系统性阻塞检测。
    传入时会在前置步骤检测是否应当 abort，优先级高于单 change 判断。
    """
    blocking_trend = entry.get("blocking_trend") or []
    categories_seen = entry.get("categories_seen") or []
    b_last = int(blocking_trend[-1]) if blocking_trend else 0
    nc = len(categories_seen)

    out: dict = {"trigger": trigger}

    # ── 前置：跨 change 系统性阻塞检测 ──────────────────────────────────────
    # archive-failed 二次决策不参与系统性检测（防止误触 abort）；
    # 其余 trigger 均检测。
    if trigger != "archive-failed" and _is_systemic_block(progress or [], trigger):
        out.update(
            {
                "action": "abort",
                "reason": "systemic-failure",
                "set_status": "skipped-auto",
                "set_aborted": True,
            }
        )
        return out

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
    # 特例：archive-failed 在 force-archive 兜底路径下（上一次 last_trigger 为
    # stale/max-rounds，说明已经 force-archive --apply 过）必须直接收敛为终态，
    # 不再给 retry 机会，防止 stale→force-archive→archive-failed→continue-retry 死循环。
    if trigger == "archive-failed" and entry.get("last_trigger") in ("stale", "max-rounds"):
        out.update(
            {
                "action": "skip",
                "reason": "archive-failed-after-force-archive",
                "set_status": "skipped-auto",
            }
        )
        return out

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
    decision = _decide(entry, trigger, progress)
    decision["seq"] = seq
    decision["change_id"] = entry.get("change_id")
    decision["blocking_trend"] = entry.get("blocking_trend") or []
    decision["categories_seen"] = entry.get("categories_seen") or []

    if args.apply:
        set_status = decision.get("set_status")
        inc_key = decision.get("increment_retry_key")
        reason = decision.get("reason")
        set_aborted = decision.get("set_aborted", False)

        def mutate(state: dict) -> None:
            prog = state.get("progress") or []
            e = prog[seq - 1]
            if inc_key:
                e[inc_key] = int(e.get(inc_key) or 0) + 1
            if set_status:
                e["status"] = set_status
            if reason:
                e["reason"] = reason
            # 记录本次 trigger，供后续系统性检测连续计数使用
            e["last_trigger"] = trigger
            # abort 决策：置顶层 aborted 标记，供 finalize 判断语义
            if set_aborted:
                state["aborted"] = True

        _state.update_state(p.state_json, p.state_md, mutate)
        decision["applied"] = True
    else:
        decision["applied"] = False

    decision["ok"] = True

    # ── telemetry：best-effort，失败不影响主流程 ──────────────────────────
    try:
        _telemetry.emit_event(
            {
                "kind": "auto_decide.decision",
                "proj_key": p.proj_key,
                "canonical_proj_key": p.canonical_proj_key or p.proj_key,
                "run_ts": p.run_ts,
                "change_seq": seq,
                "change_id": decision.get("change_id"),
                "trigger": trigger,
                "action": decision.get("action"),
                "reason": decision.get("reason"),
                "seq": seq,
                "applied": decision.get("applied", False),
            }
        )
    except Exception:
        pass

    _io.emit(decision)
