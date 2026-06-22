"""npc clean：清理 task_log 里陈旧/已中止的 run 目录与 state 文件。

基石工具，删除操作保守安全：默认 dry-run，只有 --yes 才真删。

可清理判定（保守，三条件全满足才删）：
  1. 该 run 不是 active（≠ active.json 的 current_run_ts）；
  2. 顶层 status ∈ {completed, completed-with-issues, aborted}
     或 state 文件缺失/不可读（孤儿目录）；
  3. 最后修改时间早于 keep_days 之前。

in-progress 的 run 绝不删；active run 绝不删。

CLI handler：run
纯函数：plan_cleanup（不碰文件系统，便于单测）
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

from . import _io, paths as _paths, state as _state


# 默认保留窗口（天）。
DEFAULT_KEEP_DAYS = 14

# run_ts 格式：YYYY-MM-DD-HHMM（与 paths.make_run_ts 一致）。
# 安全护栏：只有匹配此格式的目录才会被 clean 当作 run 候选。
RUN_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}-\d{4}")

# 可清理的顶层 status 集合。in-progress 永不在此集合。
_REMOVABLE_TOP_STATUS = frozenset({"completed", "completed-with-issues", "aborted"})

_MS_PER_DAY = 24 * 60 * 60 * 1000


# ----------------------------- 纯函数：清理计划 -----------------------------


def plan_cleanup(
    runs: list[dict],
    active_ts: str | None,
    keep_days: int,
    now_ms: int,
) -> dict:
    """根据 run 列表计算清理计划，纯函数，不碰文件系统。

    参数：
        runs: [{run_ts, status, mtime_ms}, ...]
              status 为顶层 status；None / 缺失视为孤儿（state 缺失或不可读）。
        active_ts: active.json 的 current_run_ts（None 表示无 active）。
        keep_days: 保留窗口天数；mtime 早于 now - keep_days 才可清理。
        now_ms: 当前毫秒时间戳。

    返回：
        {"removable": [{run_ts, status, mtime_ms, reason}, ...],
         "kept": [{run_ts, status, mtime_ms, reason}, ...]}

    安全判定（三条件全满足才可删）：
        1. run_ts != active_ts；
        2. status in _REMOVABLE_TOP_STATUS 或 status 为 None（孤儿）；
        3. mtime_ms < cutoff（now_ms - keep_days * 一天）。
    """
    cutoff = now_ms - keep_days * _MS_PER_DAY
    removable: list[dict] = []
    kept: list[dict] = []

    for run in runs:
        run_ts = run.get("run_ts")
        status = run.get("status")
        mtime_ms = run.get("mtime_ms")

        # 条件 1：active run 绝不删。
        if active_ts is not None and run_ts == active_ts:
            kept.append({**run, "reason": "active"})
            continue

        # 条件 2：必须是终态或孤儿。in-progress / 其它非终态绝不删。
        is_orphan = status is None
        if not is_orphan and status not in _REMOVABLE_TOP_STATUS:
            kept.append({**run, "reason": f"non-terminal:{status}"})
            continue

        # 条件 3：必须足够旧。mtime 缺失时保守保留（无法判定年龄）。
        if not isinstance(mtime_ms, int):
            kept.append({**run, "reason": "no-mtime"})
            continue
        if mtime_ms >= cutoff:
            kept.append({**run, "reason": "too-recent"})
            continue

        reason = "orphan" if is_orphan else status
        removable.append({**run, "reason": reason})

    return {"removable": removable, "kept": kept}


# ----------------------------- 文件系统扫描 -----------------------------


def _resolve_task_log_dir(args: argparse.Namespace) -> Path:
    """定位 task_log_dir：优先 --task-log-dir，否则 repo_root → task_log_dir。

    非 git 仓库 / 找不到 → PathsError（调用方转 exit 3）。
    """
    override = getattr(args, "task_log_dir", None)
    if override:
        return Path(override)
    repo_root = _paths.detect_repo_root()
    return _paths.task_log_dir_for(repo_root)


def _state_status(state_json: Path) -> str | None:
    """读取 state 文件的顶层 status。

    缺失 / 不可读 / 无 status 字段 → None（孤儿）。
    """
    try:
        state = _state.read_state(state_json)
    except (FileNotFoundError, ValueError, OSError):
        # ValueError 覆盖 json.JSONDecodeError。
        return None
    status = state.get("status")
    return status if isinstance(status, str) else None


def _dir_mtime_ms(run_dir: Path) -> int | None:
    """run 目录的最后修改时间（毫秒）。

    取目录自身 mtime 与其下所有文件 mtime 的最大值，避免空目录 mtime 误判太旧。
    """
    try:
        mtimes = [run_dir.stat().st_mtime]
    except OSError:
        return None
    for child in run_dir.rglob("*"):
        try:
            mtimes.append(child.stat().st_mtime)
        except OSError:
            continue
    return int(max(mtimes) * 1000)


def scan_runs(task_log_dir: Path) -> list[dict]:
    """扫描 task_log_dir 下所有 run，返回 [{run_ts, status, mtime_ms}, ...]。

    一个 run 由配对的 ``<ts>/`` 目录 + ``<ts>-plan-state.json`` 构成。
    只扫 run 目录（子目录）作为 run 的判定锚点；status 从对应 state 文件读取，
    缺失 state 的目录视为孤儿（status=None）。
    """
    if not task_log_dir.is_dir():
        return []

    runs: list[dict] = []
    for entry in sorted(task_log_dir.iterdir()):
        if not entry.is_dir():
            continue
        run_ts = entry.name
        # 安全：只把符合 run_ts 格式（YYYY-MM-DD-HHMM）的目录视为 run。
        # 杜绝把 task_log 下的外来目录（backup/、tmp/、submodule…）误当孤儿 run 删掉。
        if not RUN_TS_RE.fullmatch(run_ts):
            continue
        state_json = task_log_dir / f"{run_ts}-plan-state.json"
        runs.append(
            {
                "run_ts": run_ts,
                "status": _state_status(state_json),
                "mtime_ms": _dir_mtime_ms(entry),
            }
        )
    return runs


def _remove_run(task_log_dir: Path, run_ts: str) -> list[str]:
    """删除单个 run 的 run 目录 + -plan-state.json + -plan-state.md。

    返回实际删除的路径字符串列表。每个删除独立 try，保守容错。
    """
    removed: list[str] = []
    run_dir = task_log_dir / run_ts
    state_json = task_log_dir / f"{run_ts}-plan-state.json"
    state_md = task_log_dir / f"{run_ts}-plan-state.md"

    if run_dir.is_dir():
        try:
            shutil.rmtree(run_dir)
            removed.append(str(run_dir))
        except OSError as e:
            _io.warn(f"删除 run 目录失败：{run_dir}：{e}")
    for f in (state_json, state_md):
        if f.is_file():
            try:
                f.unlink()
                removed.append(str(f))
            except OSError as e:
                _io.warn(f"删除文件失败：{f}：{e}")
    return removed


# ----------------------------- CLI handler -----------------------------


def run(args: argparse.Namespace) -> None:
    """clean：扫描所有 run，计算可清理集；默认 dry-run，--yes 才真删。

    退出码 0；非 git 仓库 / 定位失败 → exit 3（env_missing）。
    """
    try:
        task_log_dir = _resolve_task_log_dir(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    keep_days = getattr(args, "keep_days", None)
    if keep_days is None:
        keep_days = DEFAULT_KEEP_DAYS
    # 安全：keep_days < 1 会把 cutoff 拉到 now，--yes 即刻删光所有终态 run。拒绝。
    if keep_days < 1:
        _io.emit_error(
            "invalid_args",
            f"--keep-days 必须 >= 1（收到 {keep_days}）：太小会删掉刚跑完的 run",
            exit_code=2,
        )
        return

    yes = bool(getattr(args, "yes", False))

    active_ts = _paths.read_active(task_log_dir)
    runs = scan_runs(task_log_dir)
    plan = plan_cleanup(runs, active_ts, keep_days, _io.now_ms())

    removable = plan["removable"]
    kept_count = len(plan["kept"])

    if not yes:
        # dry-run：绝不删任何东西。freed_estimate 给出将清理的 run 数量。
        _io.emit(
            {
                "ok": True,
                "dry_run": True,
                "removable": [r["run_ts"] for r in removable],
                "kept_count": kept_count,
                "freed_estimate": len(removable),
            }
        )
        return

    # --yes：真删。
    removed: list[str] = []
    for r in removable:
        removed.extend(_remove_run(task_log_dir, r["run_ts"]))

    _io.emit(
        {
            "ok": True,
            "dry_run": False,
            "removed": removed,
            "kept_count": kept_count,
        }
    )
