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
import dataclasses
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from datetime import datetime

import jsonschema

from . import (
    _io,
    events as _events,
    fixer as _fixer,
    focus as _focus,
    paths as _paths,
    review as _review,
    schema as _schema,
    state as _state,
    telemetry as _telemetry,
    verify as _verify,
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


def _should_rerun_tests(cfg: Config, p: _paths.Paths | None = None) -> bool:
    """判断 record 阶段是否应对 coder 自报 tests=pass 做真实复跑。

    优先级：
    1. ``cfg.verify.rerun_tests`` 显式配置 → 直接用。
    2. ``NPC_MODE`` 环境变量 → 兼容 --shell-exports 旧路径。
    3. ``p.mode``（从 run.json 持久化读取）→ npc init --auto 的默认编排路径，
       不经 --shell-exports 导出环境变量，通过 run.json 传递 mode。
    4. 三者均缺省 → False（interactive 默认不复跑）。
    """
    explicit = cfg.verify.rerun_tests
    if explicit is not None:
        return explicit
    env_mode = os.environ.get("NPC_MODE")
    if env_mode is not None:
        return env_mode == "auto"
    if p is not None:
        return p.mode == "auto"
    return False


def _iso_to_ms(iso_str: str | None) -> int | None:
    """ISO 8601 → 毫秒时间戳。失败返回 None。"""
    if not iso_str:
        return None
    try:
        return int(datetime.fromisoformat(iso_str).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _resolve_started_ms(cur: dict) -> int | None:
    """从 phase 记录取 started 基准（毫秒）。

    优先 ``started_ms``；缺失时回退解析 ``started_at``（ISO）。两者皆缺才 None。
    这让 phase 二次 exit（started_ms 已被首次 exit 抹掉，但 started_at 保留）
    以及只有 started_at 的路径仍能算出正确 duration。
    """
    started_ms = cur.get("started_ms")
    if started_ms is not None:
        return int(started_ms)
    return _iso_to_ms(cur.get("started_at"))


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


# ============================================================
# _do_phase_exit 调用点 handoff 契约（结构不变量 R1，第二层）
# ============================================================
#
# 背景缺陷（已复发多次）：record_implement / record_fix 把 tests_verified 等已算出的字段
# 写进 `extra={...}` 传给 _do_phase_exit，但 _do_phase_exit 转发给 telemetry.emit_phase_exit
# 时曾经只透传 `engine`，导致这些字段在调用点被静默丢弃、telemetry 侧永远看不到。
#
# PHASE_EXIT_EXTRA_CONTRACT：声明每个 phase family 中，已算出并写入 `extra` 的字段
# MUST 被 _do_phase_exit 透传给 telemetry。
#
# PHASE_EXIT_EXTRA_LOCAL_ONLY：显式登记"已算出但故意只落 state phase record、不透传
# telemetry"的字段（如 commit/summary 路径——已有更合适的落点，透传只会重复 payload）。
#
# 每个出现在 record_implement / record_fix 的 `_do_phase_exit(..., extra={...})` 字面量里
# 的字段名，MUST 属于二者之一，否则 `tests/test_structural_invariants.py` 的 AST 扫描会 fail
# （防止新增已算出字段既不透传也不登记、悄悄被丢）。
PHASE_EXIT_EXTRA_CONTRACT: dict[str, frozenset[str]] = {
    "implement": frozenset({"tests_verified"}),
    "fix": frozenset({"tests_verified"}),
}

PHASE_EXIT_EXTRA_LOCAL_ONLY: dict[str, frozenset[str]] = {
    "implement": frozenset({
        "commit", "tasks", "tests", "summary", "reason", "notes",
        "rerun_tail", "missing_keys",
    }),
    "fix": frozenset({
        "commit", "fixed", "tests", "summary", "categories_scanned",
        "regressions_added", "reason", "notes", "rerun_tail", "missing_keys",
    }),
}


def _phase_family(phase: str) -> str:
    """把具体 phase（如 fix-r2）折叠为 handoff 契约的 family key（fix）。"""
    if phase == "implement":
        return "implement"
    if phase.startswith("fix-r"):
        return "fix"
    return phase


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
        started_ms = _resolve_started_ms(cur)
        started_at = cur.get("started_at")
        duration_ms = max(0, done_ms - started_ms) if started_ms is not None else None
        new_phase: dict[str, Any] = {
            "status": status,
            "done_at": done_at,
            "duration_ms": duration_ms,
        }
        if started_at:
            new_phase["started_at"] = started_at
        if started_ms is not None:
            new_phase["started_ms"] = started_ms
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
        # handoff 透传：PHASE_EXIT_EXTRA_CONTRACT 声明的、本次 extra 中已算出的字段
        # 必须一并透给 telemetry（不能像历史缺陷那样只透 engine，把 tests_verified 等丢在调用点）。
        telemetry_extra: dict[str, Any] = {}
        if isinstance(extra, dict):
            if extra.get("engine"):
                telemetry_extra["engine"] = extra["engine"]
            handoff_fields = PHASE_EXIT_EXTRA_CONTRACT.get(_phase_family(phase), frozenset())
            for field in handoff_fields:
                if field in extra:
                    telemetry_extra[field] = extra[field]
        _telemetry.emit_phase_exit(
            proj_key=p.proj_key,
            canonical_proj_key=p.canonical_proj_key,
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
            extra=telemetry_extra or None,
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
        started_ms = _resolve_started_ms(cur)
        started_at = cur.get("started_at")
        duration_ms = max(0, done_ms - started_ms) if started_ms is not None else None
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
        if started_ms is not None:
            new_phase["started_ms"] = started_ms
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


def _classify_bad_review_schema(err: jsonschema.ValidationError) -> str:
    """引擎产出合法 JSON 但不符合 ``REVIEW_SCHEMA`` 时的可诊断失败原因。

    与 ``_classify_bad_review_output`` 同级：合法 JSON 语法不代表合法 review 结构
    （缺 ``verdict``/``findings`` 必填字段、``findings`` 非数组、finding 缺必填字段、
    枚举越界等）。这类输出 MUST NOT 被当作成功——否则 pass2 会以空替身静默"成功"、
    pass1 会在执行 pass2 后才在 ``parse_review`` 处炸掉，绕过降级与 telemetry 语义。
    """
    loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
    return f"invalid_review_schema:{loc}:{err.message}"


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


def _execute_review_pass(
    *,
    selected_engine: str,
    review_cfg,
    repo_root: Path,
    schema_path: Path,
    focus_text: str,
    review_out: Path,
    events_out: Path,
    timeout_sec: int,
    engine_bin: str,
    pt: Path,
    attempts: int,
) -> tuple[dict | None, str | None]:
    """执行单个 review pass 的引擎调用（含重试）。返回 ``(review_data, last_error)``。

    ``review_data is None`` 表示重试预算耗尽仍未产出合法 JSON。纯粹的执行 + 解析，
    不做 phase/telemetry 副作用——供 pass1 与 round-0 对抗式 pass2 共用（见 change
    review-r0-adversarial-pass D7 顺序执行）。
    """
    review_data: dict | None = None
    last_error: str | None = None
    for _attempt in range(attempts):
        if review_out.exists():
            review_out.unlink()
        if events_out.exists():
            events_out.unlink()
        if selected_engine == "codex":
            rc = _codex_exec(
                repo_root=repo_root,
                schema_path=schema_path,
                focus_text=focus_text,
                review_out=review_out,
                events_out=events_out,
                timeout_sec=timeout_sec,
                codex_bin=engine_bin,
                portable_timeout=pt,
            )
        else:
            rc = _claude_exec(
                repo_root=repo_root,
                schema_path=schema_path,
                focus_text=focus_text,
                review_out=review_out,
                events_out=events_out,
                timeout_sec=timeout_sec,
                claude_bin=engine_bin,
                portable_timeout=pt,
                model=review_cfg.claude_model,
                extra_args=review_cfg.claude_extra_args,
            )
        if rc == 0 and review_out.is_file():
            raw = review_out.read_text(encoding="utf-8")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                last_error = _classify_bad_review_output(raw, e)
                review_data = None
                continue
            # 合法 JSON 语法不等于合法 review 结构：MUST 按 REVIEW_SCHEMA 校验完整
            # 结构，否则非法 schema（缺 verdict/findings、findings 非数组、finding 字段
            # 缺失等）会被当作成功，绕过 pass2 降级与 telemetry 语义（见 change
            # review-r0-adversarial-pass D6：pass2 只有产出合法 REVIEW_SCHEMA JSON 才算成功）。
            try:
                jsonschema.validate(parsed, _schema.REVIEW_SCHEMA)
            except jsonschema.ValidationError as e:
                last_error = _classify_bad_review_schema(e)
                review_data = None
                continue
            review_data = parsed
            break
        else:
            last_error = (
                f"exit_code={rc}"
                if rc != 0
                else "review_json_missing_after_engine_exit_0"
            )
    return review_data, last_error


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

    # 解析实际将执行的 review engine（engine_name CLI 参数优先于配置文件）
    # 必须在 check_routing 之前确定，确保守卫校验的是实际执行的 engine，而非原始配置值。
    selected_engine = (engine_name or review_cfg.engine).lower()

    # 不变量 1/4 强制：review 执行前校验路由；violations 非空立即拒绝。
    # 若 CLI 传入 engine_name 覆盖了配置中的 review.engine，需用覆盖后的值做校验，
    # 否则会出现"按旧 engine 通过校验、按新 engine 实际执行"的漏洞。
    if engine_name and engine_name.lower() != review_cfg.engine.lower():
        effective_review_cfg = dataclasses.replace(review_cfg, engine=selected_engine)
        effective_cfg = dataclasses.replace(cfg, review=effective_review_cfg)
    else:
        effective_cfg = cfg
    violations = _verify.check_routing(effective_cfg)
    if violations:
        _io.emit({"ok": False, "error": "routing-violation", "violations": violations})
        raise SystemExit(1)

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
    # 最终归一化产物路径恒为 round-{n}.review.json（下游硬编码，见 D3）。
    review_path = base / f"round-{round_n}.review.json"
    events_path = base / f"round-{round_n}.events.jsonl"

    # round-0 双通道判定：round_n==0 且配置开启时，pass1 原始产物落 pass1.json，
    # 合并结果写 round-0.review.json；否则 pass1 直接写最终路径（单通道，行为不变）。
    is_adversarial_round0 = round_n == 0 and review_cfg.adversarial_round0 is True
    pass1_review_path = (
        base / "round-0.review.pass1.json" if is_adversarial_round0 else review_path
    )

    attempts = retries + 1
    # telemetry 对抗字段：默认 False/None（覆盖情形 2/3/4/5，见 D6）。
    adversarial_pass_ran = False
    adversarial_blocking_count: int | None = None

    review_data, last_error = _execute_review_pass(
        selected_engine=selected_engine,
        review_cfg=review_cfg,
        repo_root=p.repo_root,
        schema_path=p.schema_path,
        focus_text=focus_text,
        review_out=pass1_review_path,
        events_out=events_path,
        timeout_sec=timeout_sec,
        engine_bin=engine_bin,
        pt=pt,
        attempts=attempts,
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
            canonical_proj_key=p.canonical_proj_key,
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
            spec_attribution_counts=None,
            duration_ms=exit_info.get("duration_ms"),
            retry_count=max(0, attempts - 1),
            outcome_reason=error_code,
            state_json=p.state_json,
            run_events=p.run_events,
            adversarial_pass_ran=False,
            adversarial_blocking_count=None,
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

    # 3b. round-0 对抗式 pass2（仅在 pass1 成功后顺序执行；见 D6/D7）
    if is_adversarial_round0:
        adv_focus_text = _focus._adversarial_round_0_template(change_id)
        (base / "round-0.adversarial.focus.md").write_text(
            adv_focus_text, encoding="utf-8"
        )
        adv_review_path = base / "round-0.review.pass2.adversarial.json"
        adv_events_path = base / "round-0.adversarial.events.jsonl"
        pass2_data, _pass2_err = _execute_review_pass(
            selected_engine=selected_engine,
            review_cfg=review_cfg,
            repo_root=p.repo_root,
            schema_path=p.schema_path,
            focus_text=adv_focus_text,
            review_out=adv_review_path,
            events_out=adv_events_path,
            timeout_sec=timeout_sec,
            engine_bin=engine_bin,
            pt=pt,
            attempts=attempts,
        )
        if pass2_data is not None:
            # 情形 1：双 pass 成功 → 合并，adversarial_pass_ran=True，count 取 side-channel
            merged, stats = _review.merge_review_passes(review_data, pass2_data)
            adversarial_pass_ran = True
            adversarial_blocking_count = stats["adversarial_blocking_count"]
        else:
            # 情形 2：pass2 失败降级 → 以空 findings 替身参与合并，等价 pass1-only；
            # adversarial_pass_ran 保持 False、count 保持 None（D6）
            merged, _stats = _review.merge_review_passes(review_data, {"findings": []})
        review_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        review_data = merged

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
            canonical_proj_key=p.canonical_proj_key,
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
            spec_attribution_counts=None,
            duration_ms=exit_info.get("duration_ms"),
            retry_count=max(0, attempts - 1),
            outcome_reason="invalid_review_schema",
            state_json=p.state_json,
            run_events=p.run_events,
            adversarial_pass_ran=adversarial_pass_ran,
            adversarial_blocking_count=adversarial_blocking_count,
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
        canonical_proj_key=p.canonical_proj_key,
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
        spec_attribution_counts=metrics.get("spec_attribution_counts"),
        duration_ms=_review_round_duration_ms,
        retry_count=max(0, attempts - 1),
        outcome_reason=None,
        state_json=p.state_json,
        run_events=p.run_events,
        adversarial_pass_ran=adversarial_pass_ran,
        adversarial_blocking_count=adversarial_blocking_count,
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


class _GitHeadError(Exception):
    """git rev-parse HEAD 失败时抛出，携带 stderr 摘要。"""

    def __init__(self, stderr_summary: str) -> None:
        self.stderr_summary = stderr_summary
        super().__init__(stderr_summary)


def _git_head(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise _GitHeadError(stderr.strip()[:500]) from exc
    return out.stdout.strip()


_ARCHIVE_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def _archive_effect_happened(repo_root: Path, change_id: str) -> bool:
    r"""独立核验 `openspec archive` 是否真的产生了归档副作用。

    不采信子进程 returncode，转而核对文件系统真实状态（对齐
    `git_chain.check_chain` 确立的"不信子进程返回码"先例）。判定口径为双重
    确定性检查，两条同时满足才算归档已发生：

    (a) `openspec/changes/<change_id>/` 目录已不再存在（change 被移出原位）；
    (b) `openspec/changes/archive/` 下存在一个恰好命名为
        `YYYY-MM-DD-<change_id>` 的目录（OpenSpec archive 契约：确定性的
        `YYYY-MM-DD-` 零填充日期前缀 + change_id 整体，见 archive/ 实测目录名）。

    匹配用「锚定日期前缀 + change_id 整体相等」而非裸后缀匹配：先剥去
    `^\d{4}-\d{2}-\d{2}-` 日期前缀，再要求剩余部分与 change_id **整体相等**。
    裸 `endswith(f"-{change_id}")` 对连字符 change_id 不是身份安全的——
    例如 `change_id="foo"` 会被历史归档 `2026-07-10-add-foo` 误命中（它以
    `-foo` 结尾），从而在 change 实际未归档时误判副作用已发生。锚定日期边界
    + 整体相等消除了这种 suffix 碰撞。`archive/` 目录本身不存在（全新仓库从未
    归档过）时返回 False，不抛异常。
    """
    change_dir = repo_root / "openspec" / "changes" / change_id
    if change_dir.exists():
        return False
    archive_dir = repo_root / "openspec" / "changes" / "archive"
    if not archive_dir.is_dir():
        return False
    for child in archive_dir.iterdir():
        if not child.is_dir():
            continue
        stripped = _ARCHIVE_DATE_PREFIX.sub("", child.name, count=1)
        if stripped != child.name and stripped == change_id:
            return True
    return False


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
    try:
        chain = _git_chain.check_chain(p.repo_root, entry)
    except RuntimeError as exc:
        # git 二进制缺失时 check_chain 内部抛 RuntimeError("未找到 git 命令")
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "git-missing", "detail": str(exc)},
            progress_updates={"status": "failed", "reason": "git-missing"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "git-missing",
            "detail": str(exc),
        }
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

    try:
        osp = _find_openspec_bin(openspec_bin)
    except FileNotFoundError as exc:
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "openspec-missing", "detail": str(exc)},
            progress_updates={"status": "failed", "reason": "openspec-missing"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "openspec-missing",
            "detail": str(exc),
        }

    # 2. openspec validate --strict
    try:
        val = subprocess.run(
            [osp, "validate", change_id, "--strict"],
            cwd=p.repo_root,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "openspec-subprocess-failed", "detail": str(exc)},
            progress_updates={"status": "failed", "reason": "openspec-subprocess-failed"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "openspec-subprocess-failed",
            "detail": str(exc),
        }
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
    try:
        arc = subprocess.run(
            [osp, "archive", change_id, "--yes"],
            cwd=p.repo_root,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "openspec-subprocess-failed", "detail": str(exc)},
            progress_updates={"status": "failed", "reason": "openspec-subprocess-failed"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "openspec-subprocess-failed",
            "detail": str(exc),
        }
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

    # 3b. 核验归档副作用（仅 returncode == 0 时触发；与上面的
    # openspec-archive-failed 分支短路互斥）。`openspec archive` 可能在 abort
    # 场景下把 "Aborted. No files were changed." 打到 stdout 却仍 exit 0——
    # 若归档目录实际未搬迁，视为静默 abort，短路返回，不继续 git 操作。
    if not _archive_effect_happened(p.repo_root, change_id):
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "openspec-archive-aborted", "stdout": arc.stdout.strip()[:2000]},
            progress_updates={"status": "failed", "reason": "openspec-archive-aborted"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "openspec-archive-aborted",
            "stdout_tail": arc.stdout.strip()[-1000:],
        }

    # 4. git add + commit
    try:
        subprocess.run(
            ["git", "add", "openspec/"],
            cwd=p.repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "git-add-failed", "stderr": stderr.strip()[:2000]},
            progress_updates={"status": "failed", "reason": "git-add-failed"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "git-add-failed",
            "stderr_tail": stderr.strip()[-1000:],
        }
    try:
        commit = subprocess.run(
            ["git", "commit", "-m", f"chore: archive {change_id}"],
            cwd=p.repo_root,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        # git 二进制完全缺失时 subprocess.run 自身抛 FileNotFoundError
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "git-missing", "detail": str(exc)},
            progress_updates={"status": "failed", "reason": "git-missing"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "git-missing",
            "detail": str(exc),
        }
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
    try:
        archive_commit = _git_head(p.repo_root)
    except _GitHeadError as exc:
        _do_phase_exit(
            p,
            seq,
            "archive",
            status="failed",
            extra={"reason": "git-head-failed", "stderr": exc.stderr_summary},
            progress_updates={"status": "failed", "reason": "git-head-failed"},
        )
        return {
            "ok": False,
            "seq": seq,
            "change_id": change_id,
            "error": "git-head-failed",
            "stderr_tail": exc.stderr_summary,
        }

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
        canonical_proj_key=p.canonical_proj_key,
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


# RESULT 行必需键 —— 单一事实源（结构不变量 R2）。
#
# 背景缺陷（R2 round 1）：历史上 `_parse_result_line(text, keys)` 接收 `keys`
# 参数却从不校验，直接把解析出的字典原样返回；缺键的 RESULT 行会被静默当作
# 合法输入处理（往往在下游 `.get(key, 默认值)` 处产生误导性默认值，而不是快速
# 失败并指明缺了什么）。
#
# 背景缺陷（R2 round 2 复查）：round 1 修复只在 `record_implement` /
# `record_fix` 里加了调用点层面的 `_missing_required_keys()` 检查，但
# `_parse_result_line` 本身仍然接收（且忽略）一个 `keys` 参数——参数名具有
# 误导性，且解析器层面并未真正强制这条不变量，只是恰好两个调用方都记得手动
# 校验。现改为：`_parse_result_line` 不再接收/暗示做校验的 `keys` 参数，改由
# `_parse_and_validate_result_line(text, phase)` 统一做「解析 + 校验」，
# `record_implement` / `record_fix` 必须调用这一个函数，不得绕开。
#
# "failure" 条目：commit=- 且 tests=fail 时的失败态 schema（implement / fix 共用，
# 详见 plugins/agent-spine/hooks/verify-subagent-result.sh 的 SCHEMA_VARIANT=failure
# 分支）。只登记 implement 失败与 fix 失败两种失败态 RESULT 行共同必需的字段
# （commit/tests/summary/notes）；tasks/fixed 等 phase 专属字段不强制，因为失败态下
# coder 未必已算出该值。`_parse_and_validate_result_line` 在检测到 commit=-/tests=fail
# 时自动切到本条目校验，而非继续套用各自 phase 的成功态必需键集合。
RESULT_REQUIRED_KEYS: dict[str, frozenset[str]] = {
    "implement": frozenset({"commit", "tasks", "tests", "summary"}),
    "fix": frozenset({
        "commit", "fixed", "tests", "summary",
        "categories_scanned", "regressions_added",
    }),
    "failure": frozenset({"commit", "tests", "summary", "notes"}),
    # spec writer 的产物是 openspec/changes/<id>/ 下的 artifact 文件，不是代码
    # commit（沿用 implement 的 commit/tests 键会逼它做无意义的动作，见 change
    # spine-spec-writer design.md D5）。spec_write/spec_fix 的 RESULT 不含
    # commit/tests，故不会被 `_is_failure_schema` 误判为失败态覆盖。
    "spec_write": frozenset({"change", "artifacts", "validate", "summary"}),
    "spec_fix": frozenset({"change", "fixed", "validate", "summary"}),
    # spec_interrogate 的产物是 pattern-interrogation.md（模式盘问），不是 openspec
    # validate 认识的 artifact 类型，故 RESULT 不含 validate 键（见 change
    # spec-writer-pattern-interrogation proposal「新增 phase spec_interrogate 的
    # RESULT 必需键集合」）。
    "spec_interrogate": frozenset({"change", "artifacts", "summary"}),
}


def _is_failure_schema(parsed: dict) -> bool:
    """判断一条已解析的 RESULT 行是否走失败态 schema（commit=- 且 tests=fail）。"""
    return parsed.get("commit") == "-" and parsed.get("tests") == "fail"


def _missing_required_keys(parsed: dict, phase: str) -> list[str]:
    """返回 parsed 中缺失的 RESULT_REQUIRED_KEYS[phase] 键（升序，输出确定性）。"""
    required = RESULT_REQUIRED_KEYS.get(phase, frozenset())
    return sorted(k for k in required if k not in parsed)


def _parse_result_line(text: str) -> dict | None:
    """从 sub-agent message 末尾抽 RESULT 行，纯 tokenize，不做 key 校验。

    格式：``RESULT: key1=value1 key2=value2 ...``。

    本函数只负责把 RESULT 行切成 dict；required-key 校验由
    `_parse_and_validate_result_line()` 统一完成（R2 结构不变量：解析器
    不得对缺键静默放行，见 RESULT_REQUIRED_KEYS 注释）。
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
        return out
    return None


def _parse_and_validate_result_line(
    text: str, phase: str
) -> tuple[dict | None, list[str]]:
    """解析 RESULT 行并按 `RESULT_REQUIRED_KEYS` 强制校验必需键。

    这是 R2 结构不变量的**解析器级**落点：`_parse_result_line` 本身只做
    tokenize，绝不允许调用方绕过 required-key 校验直接拿到"看似合法但缺键"
    的 dict —— 所有调用方（`record_implement` / `record_fix`）必须走本函数。

    Returns:
        (parsed, missing_keys)
        - parsed is None：整行 RESULT 缺失（连 tokenize 都没匹配到）。
        - parsed is not None 且 missing_keys 非空：已解析但缺
          RESULT_REQUIRED_KEYS[phase]（或失败态覆盖为 RESULT_REQUIRED_KEYS["failure"]）
          中的键，调用方 MUST 视为解析失败，不得使用 parsed 中的字段兜底。
        - parsed is not None 且 missing_keys 为空：合法，可安全使用。
    """
    parsed = _parse_result_line(text)
    if parsed is None:
        return None, []
    check_phase = "failure" if _is_failure_schema(parsed) else phase
    missing = _missing_required_keys(parsed, check_phase)
    return parsed, missing


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

    parsed, missing_keys = _parse_and_validate_result_line(result_line, "implement")
    if parsed is None:
        _do_phase_exit(
            p, seq, "implement",
            status="failed",
            extra={"reason": "result-line-missing"},
            progress_updates={"status": "failed", "reason": "implementer"},
        )
        return {"ok": False, "seq": seq, "error": "result-line-missing"}

    if missing_keys:
        _do_phase_exit(
            p, seq, "implement",
            status="failed",
            extra={"reason": "result-missing-keys", "missing_keys": missing_keys},
            progress_updates={"status": "failed", "reason": "implementer"},
        )
        return {
            "ok": False,
            "seq": seq,
            "error": "result-missing-keys",
            "missing_keys": missing_keys,
        }

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

    # 真实复跑验证（tests=pass 自报硬轨）
    tests_verified: bool | None = None
    rerun_tail: str | None = None
    try:
        cfg = load_config(p.repo_root)
    except ConfigError:
        cfg = Config()
    if _should_rerun_tests(cfg, p):
        rerun = _verify.run_tests_result(p.repo_root, cfg)
        if rerun.get("no_command"):
            tests_verified = None  # 探测不到命令：降级，不阻塞
        elif rerun["passed"]:
            tests_verified = True
        else:
            tests_verified = False
            rerun_tail = rerun.get("tail", "")
            _do_phase_exit(
                p, seq, "implement",
                status="failed",
                extra={
                    "reason": "rerun-tests-failed",
                    "tests": "fail",
                    "tests_verified": False,
                    "rerun_tail": rerun_tail,
                },
                progress_updates={"status": "failed", "reason": "rerun-tests-failed"},
            )
            return {
                "ok": False,
                "seq": seq,
                "error": "rerun-tests-failed",
                "tests": "fail",
                "tests_verified": False,
                "rerun_tail": rerun_tail,
            }

    _do_phase_exit(
        p, seq, "implement",
        status="done",
        extra={
            "commit": commit,
            "tasks": tasks,
            "tests": tests,
            "summary": summary_path,
            "tests_verified": tests_verified,
        },
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
        "tests_verified": tests_verified,
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

    parsed, missing_keys = _parse_and_validate_result_line(result_line, "fix")
    if parsed is None:
        _do_phase_exit(
            p, seq, phase,
            status="failed",
            extra={"reason": "result-line-missing"},
            progress_updates={"status": "needs-user-decision", "reason": f"fixer-failed-r{round_n}"},
        )
        return {"ok": False, "seq": seq, "round": round_n, "error": "result-line-missing"}

    if missing_keys:
        _do_phase_exit(
            p, seq, phase,
            status="failed",
            extra={"reason": "result-missing-keys", "missing_keys": missing_keys},
            progress_updates={"status": "needs-user-decision", "reason": f"fixer-failed-r{round_n}"},
        )
        return {
            "ok": False,
            "seq": seq,
            "round": round_n,
            "error": "result-missing-keys",
            "missing_keys": missing_keys,
        }

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

    # 真实复跑验证（tests=pass 自报硬轨）
    tests_verified: bool | None = None
    rerun_tail: str | None = None
    try:
        cfg = load_config(p.repo_root)
    except ConfigError:
        cfg = Config()
    if _should_rerun_tests(cfg, p):
        rerun = _verify.run_tests_result(p.repo_root, cfg)
        if rerun.get("no_command"):
            tests_verified = None  # 探测不到命令：降级，不阻塞
        elif rerun["passed"]:
            tests_verified = True
        else:
            tests_verified = False
            rerun_tail = rerun.get("tail", "")
            _do_phase_exit(
                p, seq, phase,
                status="failed",
                extra={
                    "reason": "rerun-tests-failed",
                    "tests": "fail",
                    "tests_verified": False,
                    "rerun_tail": rerun_tail,
                },
                progress_updates={"status": "needs-user-decision", "reason": f"rerun-tests-failed-r{round_n}"},
            )
            return {
                "ok": False,
                "seq": seq,
                "round": round_n,
                "error": "rerun-tests-failed",
                "tests": "fail",
                "tests_verified": False,
                "rerun_tail": rerun_tail,
            }

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
            "notes": parsed.get("notes", ""),
            "tests_verified": tests_verified,
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
        "tests_verified": tests_verified,
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
        # run_archive 内部已处理 openspec-missing 路径；此处捕获其他意外缺失依赖。
        # 按 archive-error-contract：任何失败均以 exit 1 退出。
        _io.emit_error("openspec-missing", str(e), exit_code=1)
        return
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()[:500]
        _io.emit_error("git-subprocess-failed", f"cmd={e.cmd} stderr={stderr}", exit_code=1)
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
