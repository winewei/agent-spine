"""State 自愈（``npc state repair``）。

当 git HEAD 与 task_log state 漂移（例：用户 ``git reset`` 后 task_log 仍记录
已 archived 的 commit 链），主 session 需要让 npc 把对应 progress 项重置为
``pending`` 并把 openspec archive 退回 active，以便后续 ``--auto`` 流程能从该
seq 起重新 implement、不再触碰已经不存在的 commit。

设计要点：
- **task_log append-only**：旧 base 目录整体 ``mv`` 到 ``<run_dir>/.repaired/...``
  作为审计留存，run.events.jsonl 不动；新事件 ``state.repair`` 追加。
- **state_json 上的 repair_log 数组单向增长**：每次 repair 追加一条，永不删除。
- **openspec 同步**：若 change_id 已被 ``openspec archive`` 到 archive/，
  自动 ``mv`` 回 active changes 目录，让重跑能找到 proposal/tasks/specs。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

import json

from . import _io, git_chain as _git_chain, paths as _paths, state as _state


def _append_run_event(run_events: Path, event: dict) -> None:
    """单流 append 到 run.events.jsonl（per-change events.jsonl 已随旧 base 搬走，不再可寻）。"""
    run_events.parent.mkdir(parents=True, exist_ok=True)
    with run_events.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def _resolve_target_seqs(
    repo_root: Path, state: dict, seqs_csv: str | None
) -> list[int]:
    """决定 repair 目标 seq 列表。

    - 显式 ``--seqs``：按 CSV 解析；越界/重复都过滤
    - 否则：跑一次 ``scan_state_drift``，取 drifted_seqs 的 seq
    """
    progress = state.get("progress") or []
    total = len(progress)
    if seqs_csv:
        out: list[int] = []
        for tok in seqs_csv.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                n = int(tok)
            except ValueError:
                raise ValueError(f"--seqs 包含非整数 token：{tok!r}")
            if 1 <= n <= total and n not in out:
                out.append(n)
        return out
    drift = _git_chain.scan_state_drift(repo_root, state)
    return [d["seq"] for d in drift.get("drifted_seqs", [])]


def _move_old_base(audit_root: Path, old_base_str: str | None, ts: str) -> str | None:
    """把旧 base 目录 mv 到 audit_root/<basename>-<ts>。返回新路径或 None。"""
    if not old_base_str:
        return None
    old_base = Path(old_base_str)
    if not old_base.is_dir():
        return None
    audit_root.mkdir(parents=True, exist_ok=True)
    target = audit_root / f"{old_base.name}-{ts}"
    # 避免冲突：极少出现，但保险
    n = 1
    while target.exists():
        target = audit_root / f"{old_base.name}-{ts}.{n}"
        n += 1
    shutil.move(str(old_base), str(target))
    return str(target)


def _restore_openspec_archive(repo_root: Path, change_id: str) -> bool:
    """若 openspec/changes/archive/<cid> 存在而 openspec/changes/<cid> 不存在，把它 mv 回来。

    返回是否真的搬动。
    """
    archive_dir = repo_root / "openspec" / "changes" / "archive" / change_id
    active_dir = repo_root / "openspec" / "changes" / change_id
    if archive_dir.is_dir() and not active_dir.is_dir():
        shutil.move(str(archive_dir), str(active_dir))
        return True
    return False


def _reset_progress_entry(seq: int, change_id: str) -> dict:
    """构造重置后的 progress 条目（保留 seq + change_id）。"""
    return {
        "seq": seq,
        "change_id": change_id,
        "status": "pending",
        "blocking_trend": [],
        "categories_seen": [],
        "rounds_since_strict_decrease": 0,
        "phases": {},
    }


def state_repair(args: argparse.Namespace) -> None:
    """``npc state repair [--seqs CSV] [--auto]``。

    --auto 当前为 noop（保留参数以匹配 skill --auto 调用形态；行为本就无交互）。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = _state.read_state(p.state_json)
    except FileNotFoundError as e:
        _io.emit_error("state_not_found", str(e), exit_code=3)
        return

    try:
        target_seqs = _resolve_target_seqs(p.repo_root, state, args.seqs)
    except ValueError as e:
        _io.emit_error("invalid_seqs", str(e), exit_code=2)
        return
    except RuntimeError as e:
        _io.emit_error("git_missing", str(e), exit_code=3)
        return

    if not target_seqs:
        _io.emit({"ok": True, "repaired": [], "message": "no drift detected; nothing to repair"})
        return

    progress = state.get("progress") or []
    audit_root = p.run_dir / ".repaired"
    ts_for_audit = _io.now_iso().replace(":", "-").replace("+", "_")

    # 先在内存里做 IO 副作用（mv 目录），收集 log；最后一次 update_state
    log_entries: list[dict[str, Any]] = []
    for seq in target_seqs:
        if not (1 <= seq <= len(progress)):
            continue
        entry = progress[seq - 1]
        change_id = entry.get("change_id") or "?"
        prev_status = entry.get("status")
        old_base = entry.get("base")
        audit_base = _move_old_base(audit_root, old_base, ts_for_audit)
        moved_back = _restore_openspec_archive(p.repo_root, change_id)
        log_entries.append(
            {
                "ts": _io.now_iso(),
                "seq": seq,
                "change_id": change_id,
                "previous_status": prev_status,
                "audit_base": audit_base,
                "openspec_moved_back": moved_back,
            }
        )

    target_set = {e["seq"] for e in log_entries}

    def mutate(state: dict) -> None:
        progress = state.get("progress") or []
        for seq in target_set:
            if not (1 <= seq <= len(progress)):
                continue
            cid = progress[seq - 1].get("change_id") or "?"
            progress[seq - 1] = _reset_progress_entry(seq, cid)
        log = state.setdefault("repair_log", [])
        for ent in log_entries:
            log.append(ent)

    _state.update_state(p.state_json, p.state_md, mutate)

    # run-level event stream：每个 repair seq 写一条 state.repair
    for ent in log_entries:
        _append_run_event(
            p.run_events,
            {
                "event": "state.repair",
                "ts": ent["ts"],
                "change_seq": ent["seq"],
                "change_id": ent["change_id"],
                "previous_status": ent["previous_status"],
                "audit_base": ent["audit_base"],
                "openspec_moved_back": ent["openspec_moved_back"],
            },
        )

    _io.emit({"ok": True, "repaired": log_entries, "audit_root": str(audit_root)})
