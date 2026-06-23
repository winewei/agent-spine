"""高层管线：review / archive / implement-fix 的整段流程下沉。

设计目标：让 LLM 不再做"数据搬运"——focus 渲染、codex/openspec 子进程、
metrics 解析、phase/state 装订、git commit 等全部下沉到 CLI；LLM 只读一行 JSON。

复用约束：
- 不动 events.phase_enter / phase_exit / state.set_progress 等既有 CLI handler；
  本模块直接复用其内部 helper（append_event / update_state / parse_review / focus 模板）。
- 一次 update_state 内尽量完成多项装订（phase exit + trend + set_progress），
  保证原子性，避免连发多条 npc 命令导致的中间态。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from datetime import datetime

from . import (
    _io,
    events as _events,
    fixer as _fixer,
    focus as _focus,
    paths as _paths,
    review as _review,
    state as _state,
    telemetry as _telemetry,
)
from .config import Config, ConfigError, load_config
from .engines import (
    ClaudeEngine,
    CodexEngine,
    EngineError,
    ReviewRunInputs,
    get_engine,
)
from .trend import STALE_THRESHOLD


def _iso_to_ms(iso_str: str | None) -> int | None:
    """ISO 8601 → 毫秒时间戳。失败返回 None。"""
    if not iso_str:
        return None
    try:
        return int(datetime.fromisoformat(iso_str).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _ensure_phase_in_progress(
    p: _paths.Paths,
    seq: int,
    phase: str,
    *,
    fallback_from_phase: str | None = None,
) -> bool:
    """兜底：若 phase 不在 in-progress 状态，自动补一次 enter。

    started_at 优先级：fallback_from_phase 的 done_at → 当前时间。
    这条逻辑用于修复主 session 漏调 ``npc phase enter`` 导致的 started_at=null
    漂移（v1.0 实测：seq=3 add-checkpoint-store 的 fix-r2/r3/r4 全部丢 started_at）。

    返回 True 表示触发了补 enter；False 表示 phase 已经 in-progress 不动。
    """
    state = _state.read_state(p.state_json)
    entry = _get_entry(state, seq)
    cur = (entry.get("phases") or {}).get(phase) or {}
    if cur.get("status") == "in-progress":
        return False

    fallback_iso: str | None = None
    fallback_ms: int | None = None
    if fallback_from_phase:
        prev = (entry.get("phases") or {}).get(fallback_from_phase) or {}
        cand_iso = prev.get("done_at")
        cand_ms = _iso_to_ms(cand_iso)
        if cand_iso and cand_ms is not None:
            fallback_iso, fallback_ms = cand_iso, cand_ms
    if fallback_iso is None:
        fallback_iso = _io.now_iso()
        fallback_ms = _io.now_ms()

    def mutate(state: dict) -> None:
        entry = _get_entry(state, seq)
        phases = entry.setdefault("phases", {})
        phases[phase] = {
            "status": "in-progress",
            "started_at": fallback_iso,
            "started_ms": fallback_ms,
        }

    _state.update_state(p.state_json, p.state_md, mutate)
    base = Path(entry.get("base") or _paths.base_for(p, seq, entry["change_id"]))
    _events.append_event(
        base / "events.jsonl",
        p.run_events,
        {
            "event": "phase.start",
            "ts": fallback_iso,
            "change_seq": seq,
            "change_id": entry["change_id"],
            "phase": phase,
            "auto_enter": True,
        },
    )
    return True


# ============================================================
# 外部依赖发现
# ============================================================


def _find_codex_bin(override: str | None = None) -> str:
    if override:
        return override
    p = shutil.which("codex")
    if not p:
        raise FileNotFoundError("未在 PATH 中找到 codex 命令；请先安装")
    return p


def _find_claude_bin(override: str | None = None) -> str:
    if override:
        return override
    p = shutil.which("claude")
    if not p:
        raise FileNotFoundError(
            "未在 PATH 中找到 claude 命令；请安装 Claude Code CLI 或在 [review.claude] bin 指定"
        )
    return p


def _find_openspec_bin(override: str | None = None) -> str:
    if override:
        return override
    p = shutil.which("openspec")
    if not p:
        raise FileNotFoundError("未在 PATH 中找到 openspec 命令")
    return p


def _portable_timeout_bin(override: Path | None = None) -> Path:
    if override:
        return override
    p = Path.home() / ".local" / "bin" / "portable-timeout"
    if not p.is_file():
        raise FileNotFoundError(f"portable-timeout 未安装：{p}（请先运行 npc init）")
    return p


# ============================================================
# 状态装订原语（直接 mutate；不经 CLI 子进程）
# ============================================================


def _get_entry(state: dict, seq: int) -> dict:
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
    return progress[seq - 1]


def _do_phase_enter(p: _paths.Paths, seq: int, phase: str) -> dict:
    """phase enter 的纯内部版本。"""
    _events._validate_phase(phase)
    started_at = _io.now_iso()
    started_ms = _io.now_ms()
    captured: dict[str, Any] = {}

    def mutate(state: dict) -> None:
        entry = _get_entry(state, seq)
        if not entry.get("base"):
            base = _paths.base_for(p, seq, entry["change_id"])
            base.mkdir(parents=True, exist_ok=True)
            entry["base"] = str(base)
        phases = entry.setdefault("phases", {})
        phases[phase] = {
            "status": "in-progress",
            "started_at": started_at,
            "started_ms": started_ms,
        }
        captured["change_id"] = entry["change_id"]
        captured["base"] = entry["base"]

    _state.update_state(p.state_json, p.state_md, mutate)
    base = Path(captured["base"])
    _events.append_event(
        base / "events.jsonl",
        p.run_events,
        {
            "event": "phase.start",
            "ts": started_at,
            "change_seq": seq,
            "change_id": captured["change_id"],
            "phase": phase,
        },
    )
    return {"base": captured["base"], "change_id": captured["change_id"], "started_at": started_at}


def _do_phase_exit(
    p: _paths.Paths,
    seq: int,
    phase: str,
    *,
    status: str,
    extra: dict | None = None,
    progress_updates: dict | None = None,
) -> dict:
    """phase exit + 可选的 progress 字段更新（一次 update_state 完成）。"""
    _events._validate_phase(phase)
    done_at = _io.now_iso()
    done_ms = _io.now_ms()
    extra = extra or {}
    progress_updates = progress_updates or {}
    captured: dict[str, Any] = {}

    def mutate(state: dict) -> None:
        entry = _get_entry(state, seq)
        phases = entry.setdefault("phases", {})
        cur = phases.get(phase) or {}
        started_ms = cur.get("started_ms")
        started_at = cur.get("started_at")
        duration_ms = max(0, done_ms - int(started_ms)) if started_ms is not None else None
        new_phase: dict[str, Any] = {
            "status": status,
            "done_at": done_at,
            "duration_ms": duration_ms,
        }
        if started_at:
            new_phase["started_at"] = started_at
        new_phase.update(extra)
        phases[phase] = new_phase
        # progress 字段批量更新
        for k, v in progress_updates.items():
            entry[k] = v
        if progress_updates.get("status") and not entry.get("started_at"):
            entry["started_at"] = _io.now_iso()
        captured["change_id"] = entry["change_id"]
        captured["base"] = entry.get("base") or str(
            _paths.base_for(p, seq, entry["change_id"])
        )
        captured["duration_ms"] = duration_ms

    _state.update_state(p.state_json, p.state_md, mutate)
    base = Path(captured["base"])
    event_name = _events._phase_base_event_name(phase) + (
        "." + ("done" if status == "done" else "failed")
    )
    _events.append_event(
        base / "events.jsonl",
        p.run_events,
        {
            "event": event_name,
            "ts": done_at,
            "change_seq": seq,
            "change_id": captured["change_id"],
            "phase": phase,
            "duration_ms": captured["duration_ms"],
            **extra,
        },
    )
    # telemetry：高层 review-rN / archive-done 走专用 emit；其余统一走 phase.exit
    if not (phase.startswith("review-r") or (phase == "archive" and status == "done")):
        _telemetry.emit_phase_exit(
            proj_key=p.proj_key,
            run_ts=p.run_ts,
            change_seq=seq,
            change_id=captured["change_id"],
            phase=phase,
            status=status,
            duration_ms=captured["duration_ms"],
            base=base,
            state_json=p.state_json,
            run_events=p.run_events,
            outcome_reason=extra.get("reason") if isinstance(extra, dict) else None,
            extra={"engine": extra.get("engine")} if isinstance(extra, dict) and extra.get("engine") else None,
        )
    return {
        "change_id": captured["change_id"],
        "base": captured["base"],
        "duration_ms": captured["duration_ms"],
    }


def _do_review_phase_exit_and_trend(
    p: _paths.Paths, seq: int, phase: str, metrics: dict
) -> dict:
    """review-rN done：phase exit + update_trend + capture stale。一次 IO 完成。"""
    blocking = int(metrics.get("blocking", 0))
    new_categories = metrics.get("categories") or []
    done_at = _io.now_iso()
    done_ms = _io.now_ms()
    captured: dict[str, Any] = {}

    def mutate(state: dict) -> None:
        entry = _get_entry(state, seq)
        phases = entry.setdefault("phases", {})
        cur = phases.get(phase) or {}
        started_ms = cur.get("started_ms")
        started_at = cur.get("started_at")
        duration_ms = max(0, done_ms - int(started_ms)) if started_ms is not None else None
        new_phase: dict[str, Any] = {
            "status": "done",
            "done_at": done_at,
            "duration_ms": duration_ms,
            "verdict": metrics.get("verdict"),
            "blocking": blocking,
            "advisory": metrics.get("advisory"),
            "categories": metrics.get("categories"),
        }
        if started_at:
            new_phase["started_at"] = started_at
        phases[phase] = new_phase

        trend = list(entry.get("blocking_trend") or [])
        if not trend:
            new_rsd = 0
        else:
            prev = trend[-1]
            prev_rsd = int(entry.get("rounds_since_strict_decrease") or 0)
            new_rsd = 0 if blocking < prev else prev_rsd + 1
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

        captured["change_id"] = entry["change_id"]
        captured["base"] = entry.get("base")
        captured["duration_ms"] = duration_ms
        captured["blocking_trend"] = trend
        captured["rounds_since_strict_decrease"] = new_rsd

    _state.update_state(p.state_json, p.state_md, mutate)
    base = Path(captured["base"])
    event_name = _events._phase_base_event_name(phase) + ".done"
    _events.append_event(
        base / "events.jsonl",
        p.run_events,
        {
            "event": event_name,
            "ts": done_at,
            "change_seq": seq,
            "change_id": captured["change_id"],
            "phase": phase,
            "duration_ms": captured["duration_ms"],
            "verdict": metrics.get("verdict"),
            "blocking": blocking,
            "advisory": metrics.get("advisory"),
            "categories": metrics.get("categories"),
        },
    )
    return {
        "stale": captured["rounds_since_strict_decrease"] >= STALE_THRESHOLD,
        "rounds_since_strict_decrease": captured["rounds_since_strict_decrease"],
        "blocking_trend": captured["blocking_trend"],
    }


# ============================================================
# Codex 子进程
# ============================================================


def _codex_exec(
    *,
    repo_root: Path,
    schema_path: Path,
    focus_text: str,
    review_out: Path,
    events_out: Path,
    timeout_sec: int,
    codex_bin: str,
    portable_timeout: Path,
) -> int:
    """单次 codex exec 子进程。返回 exit code（124=timeout，127=not found）。

    本函数对外签名保留作为 test seam（``test_pipeline.py`` 通过 monkeypatch 替换它）。
    实际逻辑下沉到 :class:`engines.CodexEngine`。
    """
    return CodexEngine(codex_bin).run(
        ReviewRunInputs(
            repo_root=repo_root,
            schema_path=schema_path,
            focus_text=focus_text,
            review_out=review_out,
            events_out=events_out,
            timeout_sec=timeout_sec,
            portable_timeout=portable_timeout,
        )
    )


def _claude_exec(
    *,
    repo_root: Path,
    schema_path: Path,
    focus_text: str,
    review_out: Path,
    events_out: Path,
    timeout_sec: int,
    claude_bin: str,
    portable_timeout: Path,
    model: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> int:
    """单次 ``claude -p`` 子进程；同样以函数形式暴露便于测试 monkeypatch。"""
    return ClaudeEngine(claude_bin, model=model, extra_args=extra_args).run(
        ReviewRunInputs(
            repo_root=repo_root,
            schema_path=schema_path,
            focus_text=focus_text,
            review_out=review_out,
            events_out=events_out,
            timeout_sec=timeout_sec,
            portable_timeout=portable_timeout,
        )
    )


# ============================================================
# review run（高层入口）
# ============================================================


def _classify_bad_review_output(raw: str, err: json.JSONDecodeError) -> str:
    """引擎退出 0 但输出文件不是合法 review JSON 时，给出可诊断的失败原因。

    最常见形态：codex ``-o``（``--output-last-message``）写入的是 agent 最后一条消息。
    若 turn 在产出 schema JSON 之前结束（exec 轮次耗尽 / 模型提前收尾），落盘的就是一段
    散文，此时报成笼统的 JSON 语法错误会误导排查方向，应明确指出"未产出终判"。
    """
    stripped = raw.strip()
    if not stripped:
        return "empty_output:引擎退出 0 但输出文件为空（未产出终判 JSON）"
    if not stripped.startswith("{"):
        snippet = " ".join(stripped[:200].split())
        return (
            "non_json_output:引擎未产出终判 JSON——输出文件首字符不是 '{'。"
            "codex -o 写的是 agent 最后一条消息，很可能 turn 在产出 schema 前结束"
            f"（轮次耗尽 / 提前收尾）；实际内容片段：{snippet!r}"
        )
    return f"invalid_json:{err}"


def _render_focus(
    p: _paths.Paths,
    change_id: str,
    round_n: int,
    implement_commit: str | None,
    base: Path | None = None,
    project_context_override: Path | None = None,
) -> tuple[str, str, int]:
    """渲染 focus 文本；返回 (text, project_context_source, fixed_history_count)。"""
    ctx, src = _focus.load_project_context(p.repo_root, project_context_override)
    fixed_count = 0
    if round_n == 0:
        text = _focus._round_0_template(change_id, ctx)
    else:
        if not implement_commit:
            raise ValueError("round>=1 时必须提供 implement_commit")
        history_md = ""
        if base is not None:
            items = _focus.extract_fixed_history(base, round_n)
            fixed_count = len(items)
            if items:
                history_md = _focus.render_fixed_history_section(items)
                _focus.write_fixed_history_json(base, items)
        text = _focus._round_n_template(
            change_id, round_n, implement_commit, ctx, fixed_history_md=history_md
        )
    return text, src, fixed_count


def run_review_round(
    p: _paths.Paths,
    seq: int,
    round_n: int,
    *,
    retries: int = 1,
    timeout_sec: int = 900,
    codex_bin: str | None = None,
    portable_timeout: Path | None = None,
    engine_name: str | None = None,
    config_path: Path | None = None,
) -> dict:
    """跑完整一轮 review。失败时返回 {ok:false,...}；调用方判断 exit code。

    engine 选择优先级：``engine_name`` > 配置文件 ``[review].engine`` > 默认 ``codex``。
    ``config_path`` 显式指定 TOML 配置；省略走 :func:`config.load_config` 的标准查找链。
    """
    try:
        cfg = load_config(p.repo_root, override_path=config_path)
    except ConfigError as e:
        raise ValueError(str(e)) from e
    review_cfg = cfg.review
    if engine_name and engine_name not in ("codex", "claude"):
        raise ValueError(
            f"未知 review engine：{engine_name!r}（仅支持 codex / claude）"
        )
    selected_engine = (engine_name or review_cfg.engine).lower()

    state = _state.read_state(p.state_json)
    entry = _get_entry(state, seq)
    change_id = entry["change_id"]
    base = Path(entry.get("base") or _paths.base_for(p, seq, change_id))
    base.mkdir(parents=True, exist_ok=True)

    impl_phase = (entry.get("phases") or {}).get("implement") or {}
    implement_commit = impl_phase.get("commit")

    # 1. focus（round>=1 时自动注入 Already-Fixed History）
    focus_text, ctx_src, fixed_history_count = _render_focus(
        p, change_id, round_n, implement_commit, base=base
    )
    (base / f"round-{round_n}.focus.md").write_text(focus_text, encoding="utf-8")

    # 2. phase enter
    phase = f"review-r{round_n}"
    _do_phase_enter(p, seq, phase)

    # 3. engine exec（含重试）
    pt = _portable_timeout_bin(portable_timeout)
    if selected_engine == "codex":
        engine_bin = _find_codex_bin(codex_bin or review_cfg.codex_bin)
    else:
        engine_bin = _find_claude_bin(review_cfg.claude_bin)
    review_path = base / f"round-{round_n}.review.json"
    events_path = base / f"round-{round_n}.events.jsonl"

    review_data: dict | None = None
    last_error: str | None = None
    attempts = retries + 1
    for attempt in range(attempts):
        if review_path.exists():
            review_path.unlink()
        if events_path.exists():
            events_path.unlink()
        if selected_engine == "codex":
            rc = _codex_exec(
                repo_root=p.repo_root,
                schema_path=p.schema_path,
                focus_text=focus_text,
                review_out=review_path,
                events_out=events_path,
                timeout_sec=timeout_sec,
                codex_bin=engine_bin,
                portable_timeout=pt,
            )
        else:
            rc = _claude_exec(
                repo_root=p.repo_root,
                schema_path=p.schema_path,
                focus_text=focus_text,
                review_out=review_path,
                events_out=events_path,
                timeout_sec=timeout_sec,
                claude_bin=engine_bin,
                portable_timeout=pt,
                model=review_cfg.claude_model,
                extra_args=review_cfg.claude_extra_args,
            )
        if rc == 0 and review_path.is_file():
            raw = review_path.read_text(encoding="utf-8")
            try:
                review_data = json.loads(raw)
                break
            except json.JSONDecodeError as e:
                last_error = _classify_bad_review_output(raw, e)
                review_data = None
        else:
            last_error = (
                f"exit_code={rc}"
                if rc != 0
                else "review_json_missing_after_engine_exit_0"
            )

    if review_data is None:
        error_code = f"{selected_engine}-exec-failed"
        exit_info = _do_phase_exit(
            p,
            seq,
            phase,
            status="failed",
            extra={"reason": error_code, "error": last_error, "engine": selected_engine},
        )
        _telemetry.emit_review_round(
            proj_key=p.proj_key,
            run_ts=p.run_ts,
            change_seq=seq,
            change_id=change_id,
            round_n=round_n,
            base=base,
            ok=False,
            engine=selected_engine,
            verdict=None,
            blocking_count=None,
            blocking_categories=None,
            duration_ms=exit_info.get("duration_ms"),
            retry_count=max(0, attempts - 1),
            outcome_reason=error_code,
            state_json=p.state_json,
            run_events=p.run_events,
        )
        return {
            "ok": False,
            "seq": seq,
            "round": round_n,
            "error": error_code,
            "engine": selected_engine,
            "detail": last_error,
            "attempts": attempts,
            "events_path": str(events_path),
        }

    # 4. parse
    try:
        metrics = _review.parse_review(review_data)
    except ValueError as e:
        exit_info = _do_phase_exit(
            p,
            seq,
            phase,
            status="failed",
            extra={"reason": "invalid_review_schema", "error": str(e)},
        )
        _telemetry.emit_review_round(
            proj_key=p.proj_key,
            run_ts=p.run_ts,
            change_seq=seq,
            change_id=change_id,
            round_n=round_n,
            base=base,
            ok=False,
            engine=selected_engine,
            verdict=None,
            blocking_count=None,
            blocking_categories=None,
            duration_ms=exit_info.get("duration_ms"),
            retry_count=max(0, attempts - 1),
            outcome_reason="invalid_review_schema",
            state_json=p.state_json,
            run_events=p.run_events,
        )
        return {
            "ok": False,
            "seq": seq,
            "round": round_n,
            "error": "invalid_review_schema",
            "detail": str(e),
        }

    # 5. phase exit + trend（原子）
    stale = _do_review_phase_exit_and_trend(p, seq, phase, metrics)
    # 计算 review-rN 的 duration_ms（从 state 重新读最简单）
    _review_round_duration_ms = (
        _state.read_state(p.state_json).get("progress", [{}])[seq - 1]
        .get("phases", {})
        .get(phase, {})
        .get("duration_ms")
    )
    _telemetry.emit_review_round(
        proj_key=p.proj_key,
        run_ts=p.run_ts,
        change_seq=seq,
        change_id=change_id,
        round_n=round_n,
        base=base,
        ok=True,
        engine=selected_engine,
        verdict=metrics.get("verdict"),
        blocking_count=metrics.get("blocking"),
        blocking_categories=metrics.get("categories"),
        duration_ms=_review_round_duration_ms,
        retry_count=max(0, attempts - 1),
        outcome_reason=None,
        state_json=p.state_json,
        run_events=p.run_events,
    )

    # 6. fixer findings 自动渲染（下一轮 fix 用），仅在 blocking>0 时
    findings_path: str | None = None
    if metrics["blocking"] > 0:
        out = base / f"round-{round_n + 1}.fix.findings.md"
        out.write_text(_fixer.render_findings(metrics["blocking_findings"]), encoding="utf-8")
        findings_path = str(out)

    return {
        "ok": True,
        "seq": seq,
        "round": round_n,
        "change_id": change_id,
        "engine": selected_engine,
        "verdict": metrics["verdict"],
        "blocking": metrics["blocking"],
        "advisory": metrics["advisory"],
        "categories": metrics["categories"],
        "stale": stale["stale"],
        "rounds_since_strict_decrease": stale["rounds_since_strict_decrease"],
        "blocking_trend": stale["blocking_trend"],
        "review_json": str(review_path),
        "events_path": str(events_path),
        "focus_path": str(base / f"round-{round_n}.focus.md"),
        "findings_path": findings_path,
        "project_context_source": ctx_src,
        "fixed_history_items": fixed_history_count,
    }


# ============================================================
# archive run（高层入口）
# ============================================================


def _git_head(repo_root: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def run_archive(
    p: _paths.Paths,
    seq: int,
    *,
    openspec_bin: str | None = None,
) -> dict:
    """archive 一站式：precheck → openspec validate --strict → openspec archive --yes → git commit → 状态装订。"""
    from . import git_chain as _git_chain

    state = _state.read_state(p.state_json)
    entry = _get_entry(state, seq)
    change_id = entry["change_id"]
    base = Path(entry.get("base") or _paths.base_for(p, seq, change_id))

    # 0. phase enter
    _do_phase_enter(p, seq, "archive")

    # 1. precheck（commit chain）
    chain = _git_chain.check_chain(p.repo_root, entry)
    if not chain.get("ok"):
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "commit-chain-broken", "missing": chain.get("missing", [])},
            progress_updates={"status": "failed", "reason": "commit-chain-broken"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "commit-chain-broken",
            "missing": chain.get("missing", []),
        }

    osp = _find_openspec_bin(openspec_bin)

    # 2. openspec validate --strict
    val = subprocess.run(
        [osp, "validate", change_id, "--strict"],
        cwd=p.repo_root,
        capture_output=True,
        text=True,
    )
    if val.returncode != 0:
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "openspec-validate-failed", "stderr": val.stderr.strip()[:2000]},
            progress_updates={"status": "failed", "reason": "openspec-validate"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "openspec-validate-failed",
            "stderr_tail": val.stderr.strip()[-1000:],
        }

    # 3. openspec archive --yes
    arc = subprocess.run(
        [osp, "archive", change_id, "--yes"],
        cwd=p.repo_root,
        capture_output=True,
        text=True,
    )
    if arc.returncode != 0:
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "openspec-archive-failed", "stderr": arc.stderr.strip()[:2000]},
            progress_updates={"status": "failed", "reason": "openspec-archive"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "openspec-archive-failed",
            "stderr_tail": arc.stderr.strip()[-1000:],
        }

    # 4. git add + commit
    subprocess.run(["git", "add", "openspec/"], cwd=p.repo_root, check=True)
    commit = subprocess.run(
        ["git", "commit", "-m", f"chore: archive {change_id}"],
        cwd=p.repo_root,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "git-commit-failed", "stderr": commit.stderr.strip()[:2000]},
            progress_updates={"status": "failed", "reason": "git-commit"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "git-commit-failed",
            "stderr_tail": commit.stderr.strip()[-1000:],
        }
    archive_commit = _git_head(p.repo_root)

    # 5. 计算 total_rounds = 最大 review-rN 索引
    phases = entry.get("phases") or {}
    import re
    total_rounds = max(
        (int(m.group(1)) for k in phases.keys() if (m := re.match(r"^review-r(\d+)$", k))),
        default=0,
    )

    exit_info = _do_phase_exit(
        p,
        seq,
        "archive",
        status="done",
        extra={
            "archive_commit": archive_commit,
            "final_status": f"passed (round {total_rounds})",
        },
        progress_updates={
            "status": "archived",
            "archive_commit": archive_commit,
            "total_rounds": total_rounds,
        },
    )

    _telemetry.emit_archive_done(
        proj_key=p.proj_key,
        run_ts=p.run_ts,
        change_seq=seq,
        change_id=change_id,
        archive_commit=archive_commit,
        total_rounds=total_rounds,
        duration_ms=exit_info.get("duration_ms"),
        state_json=p.state_json,
        run_events=p.run_events,
        base=base,
    )

    return {
        "ok": True,
        "seq": seq,
        "change_id": change_id,
        "archive_commit": archive_commit,
        "total_rounds": total_rounds,
        "final_status": f"passed (round {total_rounds})",
    }


# ============================================================
# implement / fix record（高层入口）
# ============================================================


def _parse_result_line(text: str, keys: list[str]) -> dict | None:
    """从 sub-agent message 末尾抽 RESULT 行。

    格式：``RESULT: key1=value1 key2=value2 ...``。
    """
    if "RESULT:" not in text:
        return None
    # 从末尾倒推找 RESULT 行
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("RESULT:"):
            continue
        rest = line[len("RESULT:") :].strip()
        # 切 token：key=value，value 可能含空格直到下一个 key=
        out: dict[str, str] = {}
        # 简化解析：按 key= 切分
        import re
        tokens = re.split(r"\s+(?=[a-zA-Z_]+=)", rest)
        for tok in tokens:
            if "=" not in tok:
                continue
            k, _, v = tok.partition("=")
            out[k.strip()] = v.strip()
        # 校验 keys
        return out
    return None


def record_implement(
    p: _paths.Paths,
    seq: int,
    result_line: str,
    *,
    require_summary: bool = True,
) -> dict:
    """喂入 sub-agent 的 RESULT 行，完成 phase exit + state set-progress。

    成功格式：``RESULT: commit=<hash> tasks=<n> tests=<pass|fail> summary=<path> notes=<...>``
    失败格式：``RESULT: commit=- ... tests=fail ...``
    """
    state = _state.read_state(p.state_json)
    entry = _get_entry(state, seq)
    change_id = entry["change_id"]
    base = Path(entry.get("base") or _paths.base_for(p, seq, change_id))

    parsed = _parse_result_line(result_line, ["commit", "tasks", "tests", "summary"])
    if parsed is None:
        _do_phase_exit(
            p, seq, "implement",
            status="failed",
            extra={"reason": "result-line-missing"},
            progress_updates={"status": "failed", "reason": "implementer"},
        )
        return {"ok": False, "seq": seq, "error": "result-line-missing"}

    commit = parsed.get("commit", "-")
    tests = parsed.get("tests", "fail")
    summary_path = parsed.get("summary", "-")
    tasks_str = parsed.get("tasks", "0")
    try:
        tasks = int(tasks_str)
    except ValueError:
        tasks = 0

    # 失败信号：commit=- 或 tests=fail
    if commit == "-" or tests != "pass":
        _do_phase_exit(
            p, seq, "implement",
            status="failed",
            extra={
                "reason": "implementer",
                "tests": tests,
                "notes": parsed.get("notes", ""),
            },
            progress_updates={"status": "failed", "reason": "implementer"},
        )
        return {
            "ok": False,
            "seq": seq,
            "error": "implementer-failed",
            "tests": tests,
            "notes": parsed.get("notes", ""),
        }

    # summary 文件校验
    if require_summary:
        sp = Path(summary_path)
        if not sp.is_file():
            _do_phase_exit(
                p, seq, "implement",
                status="failed",
                extra={"reason": "summary-missing", "summary": summary_path},
                progress_updates={"status": "failed", "reason": "summary-missing"},
            )
            return {"ok": False, "seq": seq, "error": "summary-missing", "summary": summary_path}

    # commit 存在性校验
    head_check = subprocess.run(
        ["git", "cat-file", "-e", commit],
        cwd=p.repo_root,
        capture_output=True,
    )
    if head_check.returncode != 0:
        _do_phase_exit(
            p, seq, "implement",
            status="failed",
            extra={"reason": "commit-not-found", "commit": commit},
            progress_updates={"status": "failed", "reason": "commit-not-found"},
        )
        return {"ok": False, "seq": seq, "error": "commit-not-found", "commit": commit}

    _do_phase_exit(
        p, seq, "implement",
        status="done",
        extra={"commit": commit, "tasks": tasks, "tests": tests, "summary": summary_path},
        progress_updates={"status": "reviewing", "implement_commit": commit},
    )
    return {
        "ok": True,
        "seq": seq,
        "change_id": change_id,
        "commit": commit,
        "tasks": tasks,
        "tests": tests,
        "summary": summary_path,
    }


def record_fix(
    p: _paths.Paths,
    seq: int,
    round_n: int,
    result_line: str,
    *,
    require_summary: bool = True,
) -> dict:
    """fix-rN 的 RESULT 行喂入。

    格式：``RESULT: commit=<hash> fixed=<n> tests=<pass|fail> summary=<path> categories_scanned=<csv> regressions_added=<csv|-> notes=<...>``
    """
    phase = f"fix-r{round_n}"
    # 兜底：主 session 若漏调 phase enter（v1.0 实测回归），用 review-r(N-1).done_at 派生 started_at
    prev_phase = f"review-r{round_n - 1}" if round_n > 0 else "implement"
    _ensure_phase_in_progress(p, seq, phase, fallback_from_phase=prev_phase)

    state = _state.read_state(p.state_json)
    entry = _get_entry(state, seq)
    change_id = entry["change_id"]

    parsed = _parse_result_line(
        result_line,
        ["commit", "fixed", "tests", "summary", "categories_scanned", "regressions_added"],
    )
    if parsed is None:
        _do_phase_exit(
            p, seq, phase,
            status="failed",
            extra={"reason": "result-line-missing"},
            progress_updates={"status": "needs-user-decision", "reason": f"fixer-failed-r{round_n}"},
        )
        return {"ok": False, "seq": seq, "round": round_n, "error": "result-line-missing"}

    commit = parsed.get("commit", "-")
    tests = parsed.get("tests", "fail")
    summary_path = parsed.get("summary", "-")
    fixed_str = parsed.get("fixed", "0")
    try:
        fixed = int(fixed_str)
    except ValueError:
        fixed = 0

    if commit == "-" or tests != "pass":
        _do_phase_exit(
            p, seq, phase,
            status="failed",
            extra={"reason": "fixer", "tests": tests, "notes": parsed.get("notes", "")},
            progress_updates={"status": "needs-user-decision", "reason": f"fixer-failed-r{round_n}"},
        )
        return {
            "ok": False,
            "seq": seq,
            "round": round_n,
            "error": "fixer-failed",
            "tests": tests,
        }

    if require_summary:
        sp = Path(summary_path)
        if not sp.is_file():
            _do_phase_exit(
                p, seq, phase,
                status="failed",
                extra={"reason": "summary-missing", "summary": summary_path},
                progress_updates={"status": "needs-user-decision", "reason": "summary-missing"},
            )
            return {
                "ok": False,
                "seq": seq,
                "round": round_n,
                "error": "summary-missing",
                "summary": summary_path,
            }

    head_check = subprocess.run(
        ["git", "cat-file", "-e", commit],
        cwd=p.repo_root,
        capture_output=True,
    )
    if head_check.returncode != 0:
        _do_phase_exit(
            p, seq, phase,
            status="failed",
            extra={"reason": "commit-not-found", "commit": commit},
            progress_updates={"status": "needs-user-decision", "reason": "commit-not-found"},
        )
        return {"ok": False, "seq": seq, "round": round_n, "error": "commit-not-found"}

    _do_phase_exit(
        p, seq, phase,
        status="done",
        extra={
            "commit": commit,
            "fixed": fixed,
            "tests": tests,
            "summary": summary_path,
            "categories_scanned": parsed.get("categories_scanned", ""),
            "regressions_added": parsed.get("regressions_added", ""),
        },
        progress_updates={"status": "in-fix-loop"},
    )
    return {
        "ok": True,
        "seq": seq,
        "round": round_n,
        "change_id": change_id,
        "commit": commit,
        "fixed": fixed,
        "tests": tests,
        "summary": summary_path,
    }


# ============================================================
# CLI handler 入口
# ============================================================


def _emit_and_exit(result: dict) -> None:
    _io.emit(result)
    if not result.get("ok", False):
        import sys

        sys.exit(1)


def cli_review_run(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = run_review_round(
            p,
            args.seq,
            args.round_n,
            retries=args.retries,
            timeout_sec=args.timeout,
            codex_bin=args.codex_bin,
            portable_timeout=Path(args.portable_timeout) if args.portable_timeout else None,
            engine_name=getattr(args, "engine", None),
            config_path=Path(args.config) if getattr(args, "config", None) else None,
        )
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return
    except EngineError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)


def cli_archive_run(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = run_archive(p, args.seq, openspec_bin=args.openspec_bin)
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)


def cli_implement_record(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    result_line = args.result
    if not result_line and args.result_file:
        result_line = Path(args.result_file).read_text(encoding="utf-8")
    if not result_line:
        _io.emit_error("invalid_args", "必须提供 --result 或 --result-file", exit_code=2)
        return
    try:
        result = record_implement(p, args.seq, result_line, require_summary=not args.no_summary_check)
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)


def cli_fix_record(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    result_line = args.result
    if not result_line and args.result_file:
        result_line = Path(args.result_file).read_text(encoding="utf-8")
    if not result_line:
        _io.emit_error("invalid_args", "必须提供 --result 或 --result-file", exit_code=2)
        return
    try:
        result = record_fix(
            p, args.seq, args.round_n, result_line, require_summary=not args.no_summary_check
        )
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)
