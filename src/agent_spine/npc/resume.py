"""续跑断点判定。

启动时扫 $NPC_TASK_LOG_DIR/*-plan-state.json 找 status=in-progress 最新一份，
然后扫该 state 的 progress[].phases 找第一个非 archived change 的断点 phase。

断点 phase 规则（schema_version=2）：
- 无 phases → implement
- phases.implement.status != done → implement
- 取最大编号 N 的 review-rN / fix-rN：
  · review-rN.status != done → review-rN
  · fix-rN.status != done → fix-rN
  · review-rN.status == done 且 blocking>0 → fix-r(N+1)
  · review-rN.status == done 且 blocking==0 → archive
- phases.archive.status != done → archive
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from . import _io, paths as _paths, state as _state


def find_latest_in_progress(task_log_dir: Path) -> Path | None:
    """扫 *-plan-state.json，按 mtime 返回 status=in-progress 的最新一份。"""
    if not task_log_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in task_log_dir.glob("*-plan-state.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("status") == "in-progress":
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _next_phase_for_entry(entry: dict) -> str:
    """根据 progress 条目的 phases 字典推断下一个 phase。"""
    phases = entry.get("phases") or {}
    if not phases:
        return "implement"

    impl = phases.get("implement") or {}
    if impl.get("status") != "done":
        return "implement"

    # 找最大编号的 review-rN / fix-rN
    max_n = -1
    for k in phases.keys():
        m = re.match(r"^(?:review|fix)-r(\d+)$", k)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n

    if max_n < 0:
        # 只有 implement done 但没进过 review，下一步 review-r0
        return "review-r0"

    review_key = f"review-r{max_n}"
    fix_key = f"fix-r{max_n}"
    review = phases.get(review_key)
    fix = phases.get(fix_key)

    # 先看 review-rN 是否完成
    if review is not None and review.get("status") != "done":
        return review_key
    if fix is not None and fix.get("status") != "done":
        return fix_key

    # review-rN 已 done：根据 blocking 决定下一步
    if review is not None and review.get("status") == "done":
        blocking = review.get("blocking", 0)
        if blocking and blocking > 0:
            return f"fix-r{max_n + 1}"
        # blocking == 0 → archive
        archive = phases.get("archive") or {}
        if archive.get("status") != "done":
            return "archive"

    # fix-rN done 但 review-rN 未启动：进入 review-rN
    if fix is not None and fix.get("status") == "done" and review is None:
        return review_key

    # archive 兜底
    archive = phases.get("archive") or {}
    if archive.get("status") != "done":
        return "archive"
    return "archive"


def _current_round_from_phases(phases: dict) -> int:
    max_n = 0
    for k in phases.keys():
        m = re.match(r"^(?:fix|review)-r(\d+)$", k)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n


def compute_resume(state: dict) -> dict:
    """从 state dict 推断续跑断点。"""
    progress = state.get("progress") or []
    completed = 0
    next_entry = None
    for entry in progress:
        if entry.get("status") == "archived":
            completed += 1
            continue
        next_entry = entry
        break

    if next_entry is None:
        # 全部 archived
        return {
            "needs_resume": False,
            "all_done": True,
            "completed_changes": completed,
            "total_changes": len(progress),
        }

    next_phase = _next_phase_for_entry(next_entry)
    phases = next_entry.get("phases") or {}
    cur_round = _current_round_from_phases(phases)

    return {
        "needs_resume": True,
        "completed_changes": completed,
        "total_changes": len(progress),
        "next_seq": next_entry.get("seq"),
        "next_change_id": next_entry.get("change_id"),
        "next_phase": next_phase,
        "current_round": cur_round,
        "blocking_trend": next_entry.get("blocking_trend", []),
        "rounds_since_strict_decrease": next_entry.get("rounds_since_strict_decrease", 0),
    }


def detect(args: argparse.Namespace) -> None:
    """resume detect。"""
    state_json_override = getattr(args, "state_json", None)
    if state_json_override:
        state_json = Path(state_json_override)
    else:
        # 优先：args.task_log_dir > cwd → repo_root → task_log_dir > env
        task_log_dir: Path | None = None
        explicit = getattr(args, "task_log_dir", None)
        if explicit:
            task_log_dir = Path(explicit)
        else:
            try:
                repo_root = _paths.detect_repo_root()
                task_log_dir = _paths.task_log_dir_for(repo_root)
            except _paths.PathsError:
                env_dir = os.environ.get("NPC_TASK_LOG_DIR")
                if env_dir:
                    task_log_dir = Path(env_dir)
        if task_log_dir is None:
            _io.emit_error(
                "env_missing",
                "未能定位 task_log_dir；请在 git 仓库内运行，或显式 --task-log-dir。",
                exit_code=3,
            )
            return
        latest = find_latest_in_progress(task_log_dir)
        if latest is None:
            _io.emit(
                {
                    "needs_resume": False,
                    "state_json": None,
                    "message": "没有找到 in-progress 旧 run",
                }
            )
            return
        state_json = latest

    try:
        state = _state.read_state(state_json)
    except (OSError, json.JSONDecodeError) as e:
        _io.emit_error("state_unreadable", f"读取 state 失败：{e}", exit_code=1)
        return

    info = compute_resume(state)
    info["state_json"] = str(state_json)
    info["last_updated_at"] = state.get("last_updated_at")
    _io.emit(info)
