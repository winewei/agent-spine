"""Phase 计时与事件流追加。

每次 phase enter/exit 都执行：
1. 更新 STATE_JSON.progress[seq-1].phases.<phase>
2. 同步重写 STATE_MD
3. append 事件到 per-change events.jsonl + run-level run.events.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from . import _io, paths as _paths, state as _state, telemetry as _telemetry


def _phase_base_event_name(phase: str) -> str:
    """phase 名 → 事件名前缀。"""
    if phase == "implement":
        return "implement"
    if phase == "archive":
        return "archive"
    if re.match(r"^review-r\d+$", phase):
        return "review"
    if re.match(r"^fix-r\d+$", phase):
        return "fix"
    return phase  # 退化


VALID_PHASE_RE = re.compile(r"^(implement|archive|review-r\d+|fix-r\d+)$")


def _validate_phase(phase: str) -> None:
    if not VALID_PHASE_RE.match(phase):
        raise ValueError(
            f"phase={phase} 不合法；合法集：implement / archive / review-rN / fix-rN"
        )


def _events_file(progress_entry: dict) -> Path:
    """从 progress 条目取 per-change events.jsonl 路径。"""
    base = progress_entry.get("base")
    if not base:
        raise ValueError(
            f"progress[{progress_entry.get('seq')}].base 未设置；请先调用 npc state add-change"
        )
    return Path(base) / "events.jsonl"


def append_event(per_change_file: Path, run_events_file: Path, event: dict) -> None:
    """双流追加同一行事件。

    per-change_file 与 run_events_file 都是 append-only jsonl；每行一个紧凑 JSON。
    """
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    per_change_file.parent.mkdir(parents=True, exist_ok=True)
    run_events_file.parent.mkdir(parents=True, exist_ok=True)
    with per_change_file.open("a", encoding="utf-8") as f:
        f.write(line)
    with run_events_file.open("a", encoding="utf-8") as f:
        f.write(line)


# ----------------------------- CLI handlers -----------------------------


def phase_enter(args: argparse.Namespace) -> None:
    """phase enter <seq> <phase>。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        _validate_phase(args.phase)
    except ValueError as e:
        _io.emit_error("invalid_phase", str(e), exit_code=2)
        return

    started_at = _io.now_iso()
    started_ms = _io.now_ms()
    seq = args.seq
    phase = args.phase

    captured: dict[str, Any] = {}

    def mutate(state: dict) -> None:
        progress = state.get("progress") or []
        if not (1 <= seq <= len(progress)):
            raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
        entry = progress[seq - 1]
        # 若 base 缺失，尝试用 NPC_RUN_DIR + seq + change_id 兜底
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

    try:
        _state.update_state(p.state_json, p.state_md, mutate)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return
    except FileNotFoundError:
        _io.emit_error("state_not_found", f"STATE_JSON 不存在：{p.state_json}", exit_code=3)
        return

    base = Path(captured["base"])
    per_change_events = base / "events.jsonl"

    event = {
        "event": "phase.start",
        "ts": started_at,
        "change_seq": seq,
        "change_id": captured["change_id"],
        "phase": phase,
    }
    append_event(per_change_events, p.run_events, event)

    _io.emit(
        {
            "ok": True,
            "seq": seq,
            "phase": phase,
            "base": str(base),
            "started_at": started_at,
        }
    )


def phase_rotate(args: argparse.Namespace) -> None:
    """phase rotate --seq N --to <new-phase> [--prev-status done|failed] [--prev-extra JSON]。

    原子完成：(1) 把当前 status=in-progress 的 phase（若有）以 prev-status 退出；
    (2) 进入 new-phase。设计目的是让 fix loop 的"开始下一轮"由单一命令承担，避免
    主 session 漏调 phase enter 造成的 started_at=null 漂移（v1.0 实测回归：
    seq=3 add-checkpoint-store 的 fix-r2/r3/r4 全部 started_at=null）。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        _validate_phase(args.to_phase)
    except ValueError as e:
        _io.emit_error("invalid_phase", str(e), exit_code=2)
        return

    if args.prev_status not in ("done", "failed"):
        _io.emit_error(
            "invalid_prev_status",
            f"--prev-status={args.prev_status} 不合法（只能是 done|failed）",
            exit_code=2,
        )
        return

    try:
        prev_extra = json.loads(args.prev_extra) if args.prev_extra else {}
        if not isinstance(prev_extra, dict):
            raise ValueError("--prev-extra 必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as e:
        _io.emit_error("invalid_prev_extra", f"--prev-extra 解析失败：{e}", exit_code=2)
        return

    now_iso = _io.now_iso()
    now_ms = _io.now_ms()
    seq = args.seq
    new_phase = args.to_phase
    prev_status = args.prev_status

    captured: dict[str, Any] = {"prev_phases": []}

    def mutate(state: dict) -> None:
        progress = state.get("progress") or []
        if not (1 <= seq <= len(progress)):
            raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
        entry = progress[seq - 1]
        if not entry.get("base"):
            base = _paths.base_for(p, seq, entry["change_id"])
            base.mkdir(parents=True, exist_ok=True)
            entry["base"] = str(base)
        phases = entry.setdefault("phases", {})
        # exit 所有仍 in-progress 的 phase（正常 ≤ 1 个；异常情况下也兜底全关）
        for k, v in list(phases.items()):
            if (v or {}).get("status") != "in-progress":
                continue
            started_ms = (v or {}).get("started_ms")
            duration_ms = max(0, now_ms - int(started_ms)) if started_ms is not None else None
            replaced = dict(v)
            replaced.update(
                {
                    "status": prev_status,
                    "done_at": now_iso,
                    "duration_ms": duration_ms,
                    **prev_extra,
                }
            )
            phases[k] = replaced
            captured["prev_phases"].append({"phase": k, "duration_ms": duration_ms})
        phases[new_phase] = {
            "status": "in-progress",
            "started_at": now_iso,
            "started_ms": now_ms,
        }
        captured["change_id"] = entry["change_id"]
        captured["base"] = entry["base"]

    try:
        _state.update_state(p.state_json, p.state_md, mutate)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return
    except FileNotFoundError:
        _io.emit_error("state_not_found", f"STATE_JSON 不存在：{p.state_json}", exit_code=3)
        return

    base = Path(captured["base"])
    per_change_events = base / "events.jsonl"
    change_id = captured["change_id"]

    # 1) 旧 phase 的 done/failed 事件（每个 in-progress 都发一次）
    for prev in captured["prev_phases"]:
        ev_name = _phase_base_event_name(prev["phase"]) + ("." + ("done" if prev_status == "done" else "failed"))
        append_event(
            per_change_events,
            p.run_events,
            {
                "event": ev_name,
                "ts": now_iso,
                "change_seq": seq,
                "change_id": change_id,
                "phase": prev["phase"],
                "duration_ms": prev["duration_ms"],
                **prev_extra,
            },
        )
        if not (
            prev["phase"].startswith("review-r")
            or (prev["phase"] == "archive" and prev_status == "done")
        ):
            _telemetry.emit_phase_exit(
                proj_key=p.proj_key,
                run_ts=p.run_ts,
                change_seq=seq,
                change_id=change_id,
                phase=prev["phase"],
                status=prev_status,
                duration_ms=prev["duration_ms"],
                base=base,
                state_json=p.state_json,
                run_events=p.run_events,
                outcome_reason=prev_extra.get("reason") if isinstance(prev_extra, dict) else None,
            )

    # 2) 新 phase 的 start 事件
    append_event(
        per_change_events,
        p.run_events,
        {
            "event": "phase.start",
            "ts": now_iso,
            "change_seq": seq,
            "change_id": change_id,
            "phase": new_phase,
        },
    )

    _io.emit(
        {
            "ok": True,
            "seq": seq,
            "to_phase": new_phase,
            "prev_phases_closed": captured["prev_phases"],
            "started_at": now_iso,
            "base": str(base),
        }
    )


def phase_exit(args: argparse.Namespace) -> None:
    """phase exit <seq> <phase> --status done|failed [--extra JSON]。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        _validate_phase(args.phase)
    except ValueError as e:
        _io.emit_error("invalid_phase", str(e), exit_code=2)
        return

    try:
        extra = json.loads(args.extra) if args.extra else {}
        if not isinstance(extra, dict):
            raise ValueError("--extra 必须是 JSON 对象")
    except (json.JSONDecodeError, ValueError) as e:
        _io.emit_error("invalid_extra", f"--extra 解析失败：{e}", exit_code=2)
        return

    done_at = _io.now_iso()
    done_ms = _io.now_ms()
    seq = args.seq
    phase = args.phase
    status = args.status

    captured: dict[str, Any] = {}

    def mutate(state: dict) -> None:
        progress = state.get("progress") or []
        if not (1 <= seq <= len(progress)):
            raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
        entry = progress[seq - 1]
        phases = entry.setdefault("phases", {})
        cur = phases.get(phase) or {}
        started_ms = cur.get("started_ms")
        started_at = cur.get("started_at")
        if started_ms is None:
            # phase 未通过 enter 创建，duration 为 None；记录但不阻塞
            duration_ms = None
        else:
            duration_ms = max(0, done_ms - int(started_ms))
        new_phase: dict[str, Any] = {
            "status": status,
            "done_at": done_at,
            "duration_ms": duration_ms,
        }
        if started_at:
            new_phase["started_at"] = started_at
        new_phase.update(extra)
        phases[phase] = new_phase
        captured["change_id"] = entry["change_id"]
        captured["base"] = entry.get("base") or str(_paths.base_for(p, seq, entry["change_id"]))
        captured["duration_ms"] = duration_ms

    try:
        _state.update_state(p.state_json, p.state_md, mutate)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return
    except FileNotFoundError:
        _io.emit_error("state_not_found", f"STATE_JSON 不存在：{p.state_json}", exit_code=3)
        return

    base = Path(captured["base"])
    per_change_events = base / "events.jsonl"

    event_name = _phase_base_event_name(phase) + ("." + ("done" if status == "done" else "failed"))
    event = {
        "event": event_name,
        "ts": done_at,
        "change_seq": seq,
        "change_id": captured["change_id"],
        "phase": phase,
        "duration_ms": captured["duration_ms"],
        **extra,
    }
    append_event(per_change_events, p.run_events, event)
    # telemetry：与 pipeline._do_phase_exit 同口径，跳过 review-rN / archive.done（由专用 emit 覆盖）
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
        )

    _io.emit(
        {
            "ok": True,
            "seq": seq,
            "phase": phase,
            "duration_ms": captured["duration_ms"],
            "status": status,
        }
    )
