"""Run 收尾产物：run-summary.md 渲染 + index.jsonl 追加。

summary 与 index 都从 STATE_JSON + run.events.jsonl 派生，不需要额外记账。
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from . import _io, paths as _paths, state as _state


def _fmt_duration_ms(ms: int | None) -> str:
    return _state._fmt_duration_ms(ms)


def _total_duration_ms_from_state(state: dict) -> int | None:
    """优先用 last_updated_at - started_at；缺一返回 None。"""
    started = state.get("started_at")
    ended = state.get("last_updated_at")
    if not (started and ended):
        return None
    try:
        s = datetime.fromisoformat(started)
        e = datetime.fromisoformat(ended)
    except ValueError:
        return None
    return int((e - s).total_seconds() * 1000)


def _phase_durations_top(state: dict, n: int = 5) -> list[tuple[str, str, int]]:
    """收集所有 (change_id, phase, duration_ms)，按 duration 倒序取 top N。"""
    out: list[tuple[str, str, int]] = []
    for entry in state.get("progress") or []:
        cid = entry.get("change_id", "?")
        phases = entry.get("phases") or {}
        for ph, v in phases.items():
            d = (v or {}).get("duration_ms")
            if d is not None:
                out.append((cid, ph, int(d)))
    out.sort(key=lambda x: x[2], reverse=True)
    return out[:n]


def _categories_distribution(state: dict) -> Counter:
    """统计 categories_seen 在多少个 change 中出现 + 总 fix rounds。"""
    change_count: Counter = Counter()
    fix_count: Counter = Counter()
    for entry in state.get("progress") or []:
        for c in entry.get("categories_seen") or []:
            change_count[c] += 1
        # fix rounds 通过 phases 数
        phases = entry.get("phases") or {}
        fix_rounds = sum(
            1 for k in phases.keys() if re.match(r"^fix-r\d+$", k)
        )
        for c in entry.get("categories_seen") or []:
            fix_count[c] += fix_rounds
    return change_count, fix_count


def render_summary(state: dict) -> str:
    """渲染 run-summary.md 内容。"""
    run_ts = state.get("run_ts", "?")
    status = state.get("status", "?")
    mode = state.get("mode", "?")
    total_ms = _total_duration_ms_from_state(state)
    progress = state.get("progress") or []

    archived = [p for p in progress if p.get("status") == "archived"]
    failed = [p for p in progress if p.get("status") == "failed"]
    skipped = [p for p in progress if p.get("status") == "skipped-auto"]
    needs = [p for p in progress if p.get("status") == "needs-user-decision"]

    lines: list[str] = []
    lines.append(f"# Run Summary — {run_ts}")
    lines.append("")
    if total_ms is not None:
        started = state.get("started_at", "?")
        ended = state.get("last_updated_at", "?")
        lines.append(f"Duration: {_fmt_duration_ms(total_ms)} ({started} → {ended})")
    lines.append(f"Mode: {mode}")
    lines.append(f"Status: {status}")
    lines.append("")

    lines.append("## Totals")
    lines.append(f"- Total changes: {len(progress)}")
    lines.append(f"- Archived: {len(archived)}")
    if failed:
        names = ", ".join(
            f"#{p['seq']} {p['change_id']}" + (f" — {p['reason']}" if p.get("reason") else "")
            for p in failed
        )
        lines.append(f"- Failed: {len(failed)} ({names})")
    else:
        lines.append("- Failed: 0")
    if skipped:
        names = ", ".join(f"#{p['seq']} {p['change_id']}" for p in skipped)
        lines.append(f"- Skipped: {len(skipped)} ({names})")
    else:
        lines.append("- Skipped: 0")
    if needs:
        names = ", ".join(
            f"#{p['seq']} {p['change_id']}" + (f" — {p['reason']}" if p.get("reason") else "")
            for p in needs
        )
        lines.append(f"- Needs Decision: {len(needs)} ({names})")
    lines.append("")

    top = _phase_durations_top(state, n=5)
    if top:
        lines.append("## Phase Duration Top 5")
        lines.append("")
        lines.append("| change | phase | duration |")
        lines.append("|--------|-------|----------|")
        for cid, ph, d in top:
            lines.append(f"| {cid} | {ph} | {_fmt_duration_ms(d)} |")
        lines.append("")

    lines.append("## Commit Chain")
    for p in progress:
        seq = p.get("seq")
        cid = p.get("change_id", "?")
        parts: list[str] = []
        impl = p.get("implement_commit")
        if impl:
            parts.append(f"implement={impl}")
        phases = p.get("phases") or {}
        fix_keys = [k for k in phases.keys() if re.match(r"^fix-r\d+$", k)]
        fix_keys.sort(key=lambda k: int(re.match(r"^fix-r(\d+)$", k).group(1)))
        for k in fix_keys:
            c = (phases.get(k) or {}).get("commit")
            if c:
                parts.append(f"{k}={c}")
        arc = p.get("archive_commit")
        if arc:
            parts.append(f"archive={arc}")
        if parts:
            lines.append(f"- #{seq} {cid}: " + ", ".join(parts))
    lines.append("")

    if failed or needs:
        lines.append("## Failed / Needs Decision")
        for p in failed + needs:
            bt = p.get("blocking_trend") or []
            bt_str = "→".join(str(x) for x in bt) if bt else ""
            lines.append(
                f"- #{p['seq']} {p['change_id']}: {p['status']}"
                + (f" (reason={p.get('reason')})" if p.get("reason") else "")
                + (f"; blocking trend {bt_str}" if bt_str else "")
            )
        lines.append("")

    change_count, fix_count = _categories_distribution(state)
    if change_count:
        lines.append("## Categories Distribution")
        for cat, cc in change_count.most_common():
            lines.append(f"- {cat}: {cc} changes, {fix_count[cat]} fix rounds total")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render(args: argparse.Namespace) -> None:
    """summary render。"""
    try:
        p = _paths.load_paths(args)
        state = _state.read_state(p.state_json)
    except (_paths.PathsError, FileNotFoundError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    out_path = p.run_dir / "run-summary.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = render_summary(state)
    out_path.write_text(text, encoding="utf-8")

    dur = _total_duration_ms_from_state(state)
    progress = state.get("progress") or []
    _io.emit(
        {
            "ok": True,
            "output": str(out_path),
            "duration_ms": dur,
            "archived": sum(1 for x in progress if x.get("status") == "archived"),
            "failed": sum(1 for x in progress if x.get("status") == "failed"),
            "skipped": sum(1 for x in progress if x.get("status") == "skipped-auto"),
        }
    )


def render_index_record(state: dict) -> dict:
    """从 state 派生 index.jsonl 单行记录。"""
    progress = state.get("progress") or []
    cc = state.get("cc_session") or {}
    return {
        "run_ts": state.get("run_ts"),
        "session_id": cc.get("session_id"),
        "transcript_path": cc.get("transcript_path"),
        "project_root": state.get("project_root"),
        "status": state.get("status"),
        "total_changes": len(progress),
        "archived": sum(1 for p in progress if p.get("status") == "archived"),
        "failed": sum(1 for p in progress if p.get("status") == "failed"),
        "skipped": sum(1 for p in progress if p.get("status") == "skipped-auto"),
        "changes": [
            {
                "change_id": p.get("change_id"),
                "seq": p.get("seq"),
                "rounds": p.get("total_rounds") or _last_round(p),
                "blocking_trend": p.get("blocking_trend") or [],
                "categories": p.get("categories_seen") or [],
                "final_status": p.get("status"),
            }
            for p in progress
        ],
        "started_at": state.get("started_at"),
        "ended_at": state.get("last_updated_at"),
    }


def _last_round(progress_entry: dict) -> int:
    phases = progress_entry.get("phases") or {}
    max_n = 0
    for k in phases.keys():
        m = re.match(r"^(?:fix|review)-r(\d+)$", k)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n


def index_append(args: argparse.Namespace) -> None:
    """index append：追加一行到 $NPC_INDEX_FILE。"""
    try:
        p = _paths.load_paths(args)
        state = _state.read_state(p.state_json)
    except (_paths.PathsError, FileNotFoundError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    record = render_index_record(state)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    p.index_file.parent.mkdir(parents=True, exist_ok=True)
    with p.index_file.open("a", encoding="utf-8") as f:
        f.write(line)

    _io.emit({"ok": True, "index_file": str(p.index_file), "appended_line_bytes": len(line.encode("utf-8"))})
