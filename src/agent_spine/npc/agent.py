"""Sub-agent prompt 渲染与 spawn 引导语生成（v1.0.0 起）。

两个 handler：

- ``prompt_render``：把 §A Implementer / §B Fixer 模板渲染到 disk
- ``spawn_prompt``：生成给 Claude ``Agent`` 工具 ``prompt`` 字段的薄引导语

两者均从 STATE_JSON 自包含 resolve seq / base / implement_commit /
categories_seen / blocking_trend，调用方仅需传 ``--phase`` 与 ``--change-id``
（fix 阶段加 ``--round``）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import _io, paths as _paths, telemetry as _telemetry, templates
from .fixer import render_findings
from .review import parse_review
from .state import read_state, update_state


# ============================================================
# Agent 调用 timeout 预算（渐进退避）
# ============================================================

# 默认值参见 skill.md v3 设计文档：base 1800s / mult 1.2 / max 3600s / 最多 5 次 record-timeout
TIMEOUT_BASE_SEC_DEFAULT = 1800
TIMEOUT_MULTIPLIER_DEFAULT = 1.2
TIMEOUT_MAX_SEC_DEFAULT = 3600
TIMEOUT_EXHAUSTED_AT_RETRIES = 5  # retries 达到该值即视为耗尽（含两次撞 3600 上限）


def _compute_budget(retries: int, base: int, mult: float, max_sec: int) -> int:
    raw = base * (mult ** max(0, int(retries)))
    return int(min(raw, max_sec))


def _exhausted(retries: int) -> bool:
    return retries >= TIMEOUT_EXHAUSTED_AT_RETRIES


# ----------------------------- 内部辅助 -----------------------------


def _resolve_seq(state: dict, change_id: str, explicit_seq: int | None) -> int:
    """按 change_id 在 state.progress 里找 seq；若 explicit_seq 给定则校验一致。"""
    progress = state.get("progress") or []
    matches = [p["seq"] for p in progress if p.get("change_id") == change_id]
    if not matches:
        raise ValueError(
            f"change_id={change_id!r} 不在 STATE_JSON.progress 中；"
            f"请先 `npc state add-change` 或检查 plan_order"
        )
    if len(matches) > 1:
        raise ValueError(f"change_id={change_id!r} 在 progress 中出现多次：seq={matches}")
    found = matches[0]
    if explicit_seq is not None and explicit_seq != found:
        raise ValueError(f"--seq={explicit_seq} 与 state 中 change_id={change_id} 的 seq={found} 不一致")
    return found


def _resolve_progress_entry(state: dict, seq: int) -> dict:
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise ValueError(f"seq={seq} 越界（total={len(progress)}）")
    return progress[seq - 1]


def _default_prompt_path(base: Path, phase: str, round_n: int | None) -> Path:
    if phase == "implement":
        return base / "implement.prompt.md"
    if phase == "fix":
        if round_n is None:
            raise ValueError("fix 阶段必须传 --round")
        return base / f"round-{round_n}.fix.prompt.md"
    raise ValueError(f"未知 phase：{phase!r}")


def _default_review_path(base: Path, round_n: int) -> Path:
    """fix round N 渲染时默认引用 round-(N-1).review.json 中的 blocking findings。"""
    return base / f"round-{round_n - 1}.review.json"


# ----------------------------- CLI handlers -----------------------------


def prompt_render(args: argparse.Namespace) -> None:
    """``npc agent prompt render --phase {implement|fix} --change-id CID [...]``。

    Implement 路径：
        渲染 §A Implementer 模板到 ``$BASE/implement.prompt.md``。

    Fix 路径：
        - 读 ``--review-json``（默认 ``$BASE/round-{N-1}.review.json``）抽 blocking findings
        - 从 state 取 implement_commit / categories_seen / blocking_trend
        - 渲染 §B Fixer 模板到 ``$BASE/round-N.fix.prompt.md``
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = read_state(p.state_json)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _io.emit_error("env_missing", f"读取 STATE_JSON 失败：{e}", exit_code=3)
        return

    try:
        seq = _resolve_seq(state, args.change_id, args.seq)
        entry = _resolve_progress_entry(state, seq)
    except ValueError as e:
        _io.emit_error("state_inconsistent", str(e), exit_code=1)
        return

    base = Path(entry.get("base") or _paths.base_for(p, seq, args.change_id))
    base.mkdir(parents=True, exist_ok=True)

    phase = args.phase
    round_n = args.round_n

    if phase == "fix" and round_n is None:
        _io.emit_error("missing_round", "fix 阶段必须传 --round", exit_code=2)
        return
    if phase == "implement" and round_n is not None:
        _io.emit_error(
            "round_not_allowed", "implement 阶段不接受 --round（implement 是单次 phase）", exit_code=2
        )
        return

    try:
        output = Path(args.output) if args.output else _default_prompt_path(base, phase, round_n)
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    output.parent.mkdir(parents=True, exist_ok=True)

    if phase == "implement":
        text = templates.render_implementer(
            change_id=args.change_id,
            base=str(base),
            repo_root=str(p.repo_root),
        )
        meta_extra: dict = {}
    else:  # fix
        implement_commit = args.implement_commit or entry.get("implement_commit")
        if not implement_commit:
            _io.emit_error(
                "missing_implement_commit",
                "fix 渲染需要 implement_commit；请先 `npc implement record` 或传 --implement-commit",
                exit_code=2,
            )
            return

        review_path = Path(args.review_json) if args.review_json else _default_review_path(base, round_n)
        if not review_path.exists():
            _io.emit_error(
                "review_not_found",
                f"review.json 不存在：{review_path}（默认取 round-{round_n - 1}.review.json）",
                exit_code=3,
            )
            return
        try:
            review_data = json.loads(review_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            _io.emit_error("invalid_json", f"review.json 解析失败：{e}", exit_code=1)
            return
        try:
            parsed = parse_review(review_data)
        except ValueError as e:
            _io.emit_error("invalid_schema", f"review.json schema 不合法：{e}", exit_code=1)
            return

        findings_md = render_findings(parsed["blocking_findings"])

        text = templates.render_fixer(
            change_id=args.change_id,
            round_n=round_n,
            implement_commit=implement_commit,
            base=str(base),
            repo_root=str(p.repo_root),
            blocking_findings_md=findings_md,
            categories_seen=entry.get("categories_seen") or [],
            blocking_trend=entry.get("blocking_trend") or [],
        )
        meta_extra = {
            "round": round_n,
            "blocking_count": len(parsed["blocking_findings"]),
            "review_json": str(review_path),
            "implement_commit": implement_commit,
        }

    output.write_text(text, encoding="utf-8")

    _io.emit(
        {
            "ok": True,
            "phase": phase,
            "seq": seq,
            "change_id": args.change_id,
            "output": str(output),
            "bytes": len(text.encode("utf-8")),
            "template_version": templates.TEMPLATE_VERSION,
            **meta_extra,
        }
    )


def spawn_prompt(args: argparse.Namespace) -> None:
    """``npc agent spawn-prompt --phase ... --change-id CID [...]``。

    生成给主 session 调 ``Agent(prompt=...)`` 使用的引导语字符串（含 prompt 文件
    绝对路径 + 可选 extension）。stdout JSON 含 ``prompt`` / ``prompt_file`` 两字段。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = read_state(p.state_json)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _io.emit_error("env_missing", f"读取 STATE_JSON 失败：{e}", exit_code=3)
        return

    try:
        seq = _resolve_seq(state, args.change_id, args.seq)
        entry = _resolve_progress_entry(state, seq)
    except ValueError as e:
        _io.emit_error("state_inconsistent", str(e), exit_code=1)
        return

    phase = args.phase
    round_n = args.round_n
    if phase == "fix" and round_n is None:
        _io.emit_error("missing_round", "fix 阶段必须传 --round", exit_code=2)
        return

    base = Path(entry.get("base") or _paths.base_for(p, seq, args.change_id))

    if args.prompt_file:
        prompt_file = Path(args.prompt_file)
    else:
        try:
            prompt_file = _default_prompt_path(base, phase, round_n)
        except ValueError as e:
            _io.emit_error("invalid_args", str(e), exit_code=2)
            return

    if not prompt_file.is_absolute():
        prompt_file = prompt_file.resolve()

    if not prompt_file.exists():
        _io.emit_error(
            "prompt_file_not_found",
            f"prompt 文件不存在：{prompt_file}（先跑 `npc agent prompt render`）",
            exit_code=3,
        )
        return

    extension_text: str | None = None
    if args.extension and args.extension_inline:
        _io.emit_error(
            "conflicting_args",
            "--extension 与 --extension-inline 互斥",
            exit_code=2,
        )
        return
    if args.extension:
        ext_path = Path(args.extension)
        if not ext_path.exists():
            _io.emit_error("extension_not_found", f"extension 文件不存在：{ext_path}", exit_code=3)
            return
        extension_text = ext_path.read_text(encoding="utf-8").strip()
    elif args.extension_inline:
        extension_text = args.extension_inline.strip()

    prompt_text = templates.render_spawn_prompt(
        phase=phase,
        change_id=args.change_id,
        prompt_file=str(prompt_file),
        extension=extension_text,
    )

    _telemetry.emit_agent_spawn(
        proj_key=p.proj_key,
        run_ts=p.run_ts,
        change_seq=seq,
        change_id=args.change_id,
        phase=phase,
        round_n=round_n,
        prompt_file=prompt_file,
        state_json=p.state_json,
    )

    _io.emit(
        {
            "ok": True,
            "phase": phase,
            "seq": seq,
            "change_id": args.change_id,
            "prompt": prompt_text,
            "prompt_file": str(prompt_file),
            "has_extension": extension_text is not None,
            "bytes": len(prompt_text.encode("utf-8")),
        }
    )


# ============================================================
# Timeout budget / record-timeout
# ============================================================


def _resolve_phase_entry(state: dict, seq: int, phase: str) -> tuple[dict, dict]:
    """返回 (progress_entry, phase_dict)。phase_dict 不存在时返回 ({})。"""
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
    entry = progress[seq - 1]
    phase_dict = (entry.get("phases") or {}).get(phase) or {}
    return entry, phase_dict


def timeout_budget(args: argparse.Namespace) -> None:
    """``npc agent timeout-budget --seq N --phase X [--base N --mult F --max N]``。

    纯查询；不修改 state。返回 ``{timeout_sec, retries, exhausted, max_reached}``。
    主 session 在每次 Agent(...) 调用前先取一次预算，超时则调 ``record-timeout``，
    再下次 Agent 调用时再取——直到 ``exhausted=true`` 则放弃当前 change。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = read_state(p.state_json)
    except FileNotFoundError as e:
        _io.emit_error("state_not_found", str(e), exit_code=3)
        return

    try:
        _, phase_dict = _resolve_phase_entry(state, args.seq, args.phase)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return

    retries = int(phase_dict.get("timeout_retries") or 0)
    base = int(args.base) if args.base is not None else TIMEOUT_BASE_SEC_DEFAULT
    mult = float(args.mult) if args.mult is not None else TIMEOUT_MULTIPLIER_DEFAULT
    max_sec = int(args.max_sec) if args.max_sec is not None else TIMEOUT_MAX_SEC_DEFAULT

    timeout_sec = _compute_budget(retries, base, mult, max_sec)
    _io.emit(
        {
            "ok": True,
            "seq": args.seq,
            "phase": args.phase,
            "timeout_sec": timeout_sec,
            "retries": retries,
            "exhausted": _exhausted(retries),
            "max_reached": timeout_sec >= max_sec,
            "base_sec": base,
            "multiplier": mult,
            "max_sec": max_sec,
            "exhausted_at_retries": TIMEOUT_EXHAUSTED_AT_RETRIES,
        }
    )


def record_timeout(args: argparse.Namespace) -> None:
    """``npc agent record-timeout --seq N --phase X``。

    递增 ``phases[X].timeout_retries`` 并写 ``timeout_last_ts``。返回新的预算。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    base = int(args.base) if args.base is not None else TIMEOUT_BASE_SEC_DEFAULT
    mult = float(args.mult) if args.mult is not None else TIMEOUT_MULTIPLIER_DEFAULT
    max_sec = int(args.max_sec) if args.max_sec is not None else TIMEOUT_MAX_SEC_DEFAULT
    seq = args.seq
    phase = args.phase

    captured: dict = {}

    def mutate(state: dict) -> None:
        progress = state.get("progress") or []
        if not (1 <= seq <= len(progress)):
            raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
        entry = progress[seq - 1]
        phases = entry.setdefault("phases", {})
        phase_dict = phases.setdefault(phase, {})
        retries = int(phase_dict.get("timeout_retries") or 0) + 1
        phase_dict["timeout_retries"] = retries
        phase_dict["timeout_last_ts"] = _io.now_iso()
        captured["retries"] = retries

    try:
        update_state(p.state_json, p.state_md, mutate)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return
    except FileNotFoundError as e:
        _io.emit_error("state_not_found", str(e), exit_code=3)
        return

    retries = captured["retries"]
    next_budget = _compute_budget(retries, base, mult, max_sec)
    _io.emit(
        {
            "ok": True,
            "seq": seq,
            "phase": phase,
            "retries": retries,
            "next_timeout_sec": next_budget,
            "exhausted": _exhausted(retries),
            "max_reached": next_budget >= max_sec,
        }
    )
