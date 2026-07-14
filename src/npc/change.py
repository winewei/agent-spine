"""npc change run —— 单 change 内环整体下沉（v1.5，P1）。

把 implement → review round-0 → (fix → review)* → archive 的确定性循环从
skill 文本收进 npc 一条命令。设计依据 docs/optimization-proposals/
2026-07-05-orchestration-context-budget.md §3.2：

- **循环控制流本就是确定性的**（分支条件只有 blocking / stale / 轮数上限 /
  auto-decide 的 action），主 session 不该逐轮搬运 JSON。
- **决策点分档**：``--auto``（或 state.mode=auto）内部调 :mod:`auto_decide`
  一路跑完；交互档跑到决策点带 ``status=needs-decision`` 退出（exit 5），
  把 ``pending_decision`` 装订进 state，等主 session 问人后用
  ``--decision <action>`` 续跑。人驾驭的粒度从"盯每一轮"提升到"只在分叉点出场"。
- **复用不重写**：implement/fix 走 :func:`coder.run_implement` /
  :func:`coder.run_fix`，review 走 :func:`pipeline.run_review_round`，
  archive 走 :func:`pipeline.run_archive`；本模块只做状态机编排。

退出码：0 archived；1 终态失败（skipped / failed / aborted）；
5 needs-decision（仅交互档）；2 用法错；3 环境错；4 依赖缺失。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import _io, auto_decide as _auto, coder as _coder, paths as _paths, pipeline as _pipeline, state as _state, telemetry as _telemetry

DEFAULT_MAX_ROUNDS = 20

# --decision 合法动作（与 auto-decide 的 action 集对齐，外加人类专属 abort）
DECISION_ACTIONS = ("continue-retry", "skip", "force-archive", "abort")

# 起点 phase 合法集（--from）
FROM_PHASES = ("implement", "review", "fix", "archive")

# 终态：不允许再对其跑 change run
_TERMINAL = frozenset({"archived", "failed", "skipped-auto"})


class UsageError(ValueError):
    """用法错误（CLI 层转 exit 2）。"""


def _entry(state: dict, seq: int) -> dict:
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise UsageError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
    return progress[seq - 1]


def _pointer(p: _paths.Paths, entry: dict, seq: int) -> dict:
    base = entry.get("base") or str(_paths.base_for(p, seq, entry.get("change_id", "?")))
    return {
        "state_json": str(p.state_json),
        "run_events": str(p.run_events),
        "base": base,
    }


def _update_entry(p: _paths.Paths, seq: int, mutator) -> None:
    def mutate(state: dict) -> None:
        mutator(_entry(state, seq))

    _state.update_state(p.state_json, p.state_md, mutate)


def _next_round(entry: dict) -> int:
    """从 blocking_trend 推下一个 review 轮次（= 已完成 review 轮数）。"""
    return len(entry.get("blocking_trend") or [])


def derive_start(entry: dict, from_phase: str | None) -> tuple[str, int]:
    """确定起点 (phase, round_n)。

    显式 --from 优先；否则按 entry.status 推：pending/failed → implement；
    reviewing / in-fix-loop → review（下一轮次）。round 语义：review-rN 的 N；
    fix 起点时 N 表示将要跑的 fix-rN（其后接 review-rN）。
    """
    rn = _next_round(entry)
    if from_phase:
        if from_phase == "implement":
            return "implement", 0
        if from_phase in ("review", "fix"):
            return from_phase, max(rn, 1) if from_phase == "fix" else rn
        return "archive", rn
    status = entry.get("status") or "pending"
    if status in ("reviewing",):
        return "review", rn
    if status == "in-fix-loop":
        return "review", rn
    return "implement", 0


def _decision_point(
    p: _paths.Paths,
    seq: int,
    *,
    trigger: str,
    phase: str,
    round_n: int,
    auto: bool,
    detail: str | None = None,
) -> dict:
    """决策点：auto 档就地裁定并返回 action 指令；交互档装订 pending_decision。

    返回 dict：
    - ``{"kind": "action", "action": ...}``（auto 档，调用方继续状态机）
    - ``{"kind": "needs-decision", "result": {...}}``（交互档，调用方 emit + exit 5）
    """
    state = _state.read_state(p.state_json)
    entry = _entry(state, seq)
    decision = _auto._decide(entry, trigger) if trigger in _auto.VALID_TRIGGERS else {
        "action": "skip",
        "reason": trigger,
        "set_status": "skipped-auto",
    }

    if auto:
        set_status = decision.get("set_status")
        inc_key = decision.get("increment_retry_key")
        reason = decision.get("reason")

        def mut(e: dict) -> None:
            if inc_key:
                e[inc_key] = int(e.get(inc_key) or 0) + 1
            if set_status:
                e["status"] = set_status
            if reason:
                e["reason"] = reason

        _update_entry(p, seq, mut)
        _telemetry.emit_deviation(
            proj_key=p.proj_key,
            run_ts=p.run_ts,
            change_seq=seq,
            change_id=entry.get("change_id"),
            trigger=trigger,
            action=decision["action"],
            phase=phase,
            cost_rounds=round_n,
            state_json=p.state_json,
            run_events=p.run_events,
        )
        return {"kind": "action", "action": decision["action"], "reason": decision.get("reason")}

    pd = {
        "trigger": trigger,
        "phase": phase,
        "round": round_n,
        "suggested": decision["action"],
        "created_at": _io.now_iso(),
    }
    if detail:
        pd["detail"] = detail[:500]

    def mut(e: dict) -> None:
        e["pending_decision"] = pd
        e["status"] = "needs-user-decision"
        e["reason"] = trigger

    _update_entry(p, seq, mut)
    result = {
        "ok": False,
        "seq": seq,
        "change_id": entry.get("change_id"),
        "status": "needs-decision",
        "trigger": trigger,
        "phase": phase,
        "round": round_n,
        "suggested": decision["action"],
        "blocking_trend": entry.get("blocking_trend") or [],
        "categories_seen": entry.get("categories_seen") or [],
        "pointer": _pointer(p, entry, seq),
    }
    if detail:
        result["detail"] = detail[:500]
    return {"kind": "needs-decision", "result": result}


def _terminal(
    p: _paths.Paths, seq: int, entry: dict, *, status: str, reason: str | None = None, extra: dict | None = None
) -> dict:
    out = {
        "ok": status == "archived",
        "seq": seq,
        "change_id": entry.get("change_id"),
        "status": status,
        "blocking_trend": entry.get("blocking_trend") or [],
        "pointer": _pointer(p, entry, seq),
    }
    if reason:
        out["reason"] = reason
    if extra:
        out.update(extra)
    return out


def run_change(
    p: _paths.Paths,
    seq: int,
    *,
    from_phase: str | None = None,
    decision: str | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    auto: bool = False,
    backend: str | None = None,
    coder_timeout: int | None = None,
    review_retries: int = 1,
    review_timeout: int = 900,
    engine_name: str | None = None,
    config_path: Path | None = None,
) -> dict:
    """单 change 内环状态机。返回终态 dict；needs-decision 时带 status 字段。

    调用方（CLI handler）按返回的 status 决定 exit code：
    archived → 0；skipped/failed/aborted → 1；needs-decision → 5。
    """
    state = _state.read_state(p.state_json)
    entry = _entry(state, seq)
    change_id = entry.get("change_id")
    if not change_id:
        raise UsageError(f"seq={seq} 的 progress 缺少 change_id")

    auto = auto or (state.get("mode") == "auto")

    # ---- --decision：消费 pending_decision ----
    if decision is not None:
        if decision not in DECISION_ACTIONS:
            raise UsageError(
                f"--decision={decision!r} 不合法；合法集：{list(DECISION_ACTIONS)}"
            )
        pd = entry.get("pending_decision")
        if not pd:
            raise UsageError(
                f"seq={seq} 无 pending_decision；--decision 仅用于消费 needs-decision 退出点"
            )

        def clear(e: dict) -> None:
            e.pop("pending_decision", None)

        _update_entry(p, seq, clear)
        _telemetry.emit_deviation(
            proj_key=p.proj_key,
            run_ts=p.run_ts,
            change_seq=seq,
            change_id=change_id,
            trigger=pd.get("trigger", "unknown"),
            action=decision,
            phase=pd.get("phase"),
            cost_rounds=pd.get("round"),
            decided_by="user",
            state_json=p.state_json,
            run_events=p.run_events,
        )
        if decision == "skip":
            def mut(e: dict) -> None:
                e["status"] = "skipped-auto"
                e["reason"] = f"user-skip-after-{pd.get('trigger', 'unknown')}"

            _update_entry(p, seq, mut)
            return _terminal(p, seq, entry, status="skipped", reason=pd.get("trigger"))
        if decision == "abort":
            def mut(e: dict) -> None:
                e["status"] = "failed"
                e["reason"] = f"user-abort-after-{pd.get('trigger', 'unknown')}"

            _update_entry(p, seq, mut)
            return _terminal(p, seq, entry, status="aborted", reason=pd.get("trigger"))
        if decision == "force-archive":
            phase, round_n = "archive", int(pd.get("round") or 0)
        else:  # continue-retry：回到卡住的 phase 重试
            phase = pd.get("phase") or "implement"
            round_n = int(pd.get("round") or 0)
            # 决策消费后 status 复位（避免留 needs-user-decision）；archive 相位
            # 不动——run_archive 成功/失败都会自行装订终态。
            if phase != "archive":
                def mut(e: dict) -> None:
                    if e.get("status") == "needs-user-decision":
                        e["status"] = "in-fix-loop" if phase in ("fix", "review") else "pending"

                _update_entry(p, seq, mut)
    else:
        if entry.get("pending_decision"):
            raise UsageError(
                f"seq={seq} 存在未消费的 pending_decision（trigger="
                f"{entry['pending_decision'].get('trigger')}）；请带 --decision 续跑"
            )
        if entry.get("status") in _TERMINAL and not from_phase:
            raise UsageError(
                f"seq={seq} 已是终态 {entry.get('status')!r}；如需强行重跑请显式 --from"
            )
        phase, round_n = derive_start(entry, from_phase)

    # ---- 状态机主循环 ----
    while True:
        if phase == "implement":
            res = _coder.run_implement(
                p, seq, change_id,
                backend=backend, timeout=coder_timeout, config_path=config_path,
            )
            if not res.get("ok"):
                dp = _decision_point(
                    p, seq, trigger="implementer-failed", phase="implement",
                    round_n=0, auto=auto, detail=str(res.get("error") or res.get("reason")),
                )
                if dp["kind"] == "needs-decision":
                    return dp["result"]
                if dp["action"] == "continue-retry":
                    continue
                if dp["action"] == "force-archive":
                    phase = "archive"
                    continue
                state = _state.read_state(p.state_json)
                return _terminal(p, seq, _entry(state, seq), status="skipped", reason=dp.get("reason"))
            phase, round_n = "review", 0
            continue

        if phase == "fix":
            res = _coder.run_fix(
                p, seq, change_id, round_n,
                backend=backend, timeout=coder_timeout, config_path=config_path,
            )
            if not res.get("ok"):
                dp = _decision_point(
                    p, seq, trigger="fixer-failed", phase="fix",
                    round_n=round_n, auto=auto, detail=str(res.get("error")),
                )
                if dp["kind"] == "needs-decision":
                    return dp["result"]
                if dp["action"] == "continue-retry":
                    continue
                if dp["action"] == "force-archive":
                    phase = "archive"
                    continue
                state = _state.read_state(p.state_json)
                return _terminal(p, seq, _entry(state, seq), status="skipped", reason=dp.get("reason"))
            phase = "review"  # fix-rN 后接 review-rN，round_n 不变
            continue

        if phase == "review":
            res = _pipeline.run_review_round(
                p, seq, round_n,
                retries=review_retries, timeout_sec=review_timeout,
                engine_name=engine_name, config_path=config_path,
            )
            if not res.get("ok"):
                dp = _decision_point(
                    p, seq, trigger="codex-failed", phase="review",
                    round_n=round_n, auto=auto, detail=str(res.get("detail") or res.get("error")),
                )
                if dp["kind"] == "needs-decision":
                    return dp["result"]
                if dp["action"] == "continue-retry":
                    continue
                if dp["action"] == "force-archive":
                    phase = "archive"
                    continue
                state = _state.read_state(p.state_json)
                return _terminal(p, seq, _entry(state, seq), status="skipped", reason=dp.get("reason"))

            if int(res.get("blocking") or 0) == 0:
                phase = "archive"
                continue
            trigger: str | None = None
            if res.get("stale"):
                trigger = "stale"
            elif round_n + 1 > max_rounds:
                trigger = "max-rounds"
            if trigger:
                dp = _decision_point(
                    p, seq, trigger=trigger, phase="review", round_n=round_n, auto=auto,
                )
                if dp["kind"] == "needs-decision":
                    return dp["result"]
                if dp["action"] == "force-archive":
                    phase = "archive"
                    continue
                state = _state.read_state(p.state_json)
                return _terminal(p, seq, _entry(state, seq), status="skipped", reason=dp.get("reason"))
            phase, round_n = "fix", round_n + 1
            continue

        # phase == "archive"
        res = _pipeline.run_archive(p, seq)
        if not res.get("ok"):
            # archive 失败多为硬性问题（chain broken / validate），auto-decide 无对应
            # trigger；auto 档直接终态 failed（run_archive 已装订 status/reason），
            # 交互档留给人裁定（可 skip / 重试）。
            if auto:
                state = _state.read_state(p.state_json)
                _telemetry.emit_deviation(
                    proj_key=p.proj_key, run_ts=p.run_ts, change_seq=seq,
                    change_id=change_id, trigger="archive-failed", action="fail",
                    phase="archive", cost_rounds=round_n,
                    state_json=p.state_json, run_events=p.run_events,
                )
                return _terminal(
                    p, seq, _entry(state, seq), status="failed",
                    reason=res.get("error"), extra={"error": res.get("error")},
                )
            dp = _decision_point(
                p, seq, trigger="archive-failed", phase="archive",
                round_n=round_n, auto=False, detail=str(res.get("error")),
            )
            return dp["result"]
        state = _state.read_state(p.state_json)
        return _terminal(
            p, seq, _entry(state, seq), status="archived",
            extra={
                "archive_commit": res.get("archive_commit"),
                "rounds": res.get("total_rounds"),
            },
        )


# ============================================================
# CLI handler
# ============================================================


def cli_run(args: argparse.Namespace) -> None:
    """``npc change run`` handler。"""
    import sys

    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = run_change(
            p,
            args.seq,
            from_phase=getattr(args, "from_phase", None),
            decision=getattr(args, "decision", None),
            max_rounds=getattr(args, "max_rounds", DEFAULT_MAX_ROUNDS),
            auto=getattr(args, "auto", False),
            backend=getattr(args, "backend", None),
            coder_timeout=getattr(args, "coder_timeout", None),
            review_retries=getattr(args, "review_retries", 1),
            review_timeout=getattr(args, "review_timeout", 900),
            engine_name=getattr(args, "engine", None),
            config_path=Path(args.config) if getattr(args, "config", None) else None,
        )
    except UsageError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return
    except _coder.MimoEnvError as e:
        _io.emit_error("env_error", str(e), exit_code=3)
        return
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return

    _io.emit(result)
    status = result.get("status")
    if status == "archived":
        return
    if status == "needs-decision":
        sys.exit(5)
    sys.exit(1)
