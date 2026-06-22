"""npc git —— SDD git 卫生基石命令。

三个子命令，把"分支隔离 / 干净工作区 / 确定性提交"做成可复用的硬轨：

- ``npc git branch-for --change <id>``：确定性切到 ``change/<id>`` 分支
  （已存在则 checkout，否则 create + checkout）。
- ``npc git ensure-clean``：``git status --porcelain`` 判定工作区是否干净；
  脏则 exit 1 但仍打印 dirty_files，便于上层取列表。
- ``npc git commit``：``git add -A`` + ``git commit``。消息来源优先 ``--message``，
  否则由 ``--change`` / ``--phase`` 派生 conventional 消息。无改动可提交时
  committed=false 且 exit 0（不算失败）。

所有 git 调用走可注入 runner（默认 :func:`subprocess.run`），repo 用
:func:`paths.detect_repo_root`。退出码：成功 0 / 业务失败 1 / 用法错 2 /
非 git 仓库 3。
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from . import _io
from . import paths as _paths


# ============================================================
# 共享：repo 定位（便于测试 monkeypatch）
# ============================================================


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """定位 repo_root。git 卫生命令只需 git 仓库（无需 active run / npc init）：

    优先 git toplevel；仅当 cwd 不在 git 仓库时回退 load_paths（兼容显式 --run-ts）。
    """
    try:
        return _paths.detect_repo_root()
    except _paths.PathsError:
        return _paths.load_paths(args).repo_root


# ============================================================
# 纯函数：分支名 / 提交消息派生（便于单测）
# ============================================================


def branch_name_for(change_id: str) -> str:
    """把 change-id 转为确定性分支名 ``change/<change-id>``。"""
    return f"change/{change_id}"


def derive_commit_message(
    message: str | None,
    change: str | None,
    phase: str | None,
) -> str | None:
    """派生 commit 消息（纯函数）。

    优先级：
    1. ``message`` 显式给定（非空，去首尾空白后）→ 直接用。
    2. 否则若有 ``change``：派生 conventional 消息
       ``chore(spine): <phase> <change>``；无 phase 则省略 phase 段，
       得到 ``chore(spine): <change>``。
    3. 都没有 → ``None``（由调用方判定为用法错）。
    """
    if message is not None and message.strip():
        return message.strip()
    if change is not None and change.strip():
        change_v = change.strip()
        phase_v = phase.strip() if (phase is not None and phase.strip()) else None
        if phase_v:
            return f"chore(spine): {phase_v} {change_v}"
        return f"chore(spine): {change_v}"
    return None


# ============================================================
# git 原语（可注入 runner）
# ============================================================


def _branch_exists(repo_root: Path, branch: str, runner) -> bool:
    """git rev-parse --verify <branch>：本地分支是否已存在。"""
    proc = runner(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _current_branch(repo_root: Path, runner) -> str:
    """git rev-parse --abbrev-ref HEAD：当前分支名（detached 时返回 'HEAD'）。"""
    proc = runner(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return (proc.stdout or "").strip()


def _head_hash(repo_root: Path, runner) -> str:
    """git rev-parse HEAD：当前 HEAD 的完整 hash。"""
    proc = runner(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return (proc.stdout or "").strip()


# ============================================================
# 子命令 1：npc git branch-for
# ============================================================


def cli_branch_for(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc git branch-for --change <id>``：确定性切到 change 分支。

    已存在 → checkout（created=false）；否则 create + checkout（created=true）。
    退出码：成功 0；缺 --change → 2；非 git 仓库 → 3；git 操作失败 → 1。
    """
    change = getattr(args, "change", None)
    if change is None or not str(change).strip():
        _io.emit_error("usage_error", "branch-for 需要 --change <change-id>", exit_code=2)
        return

    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("not_a_repo", f"未能定位 repo_root（非 git 仓库？）：{e}", exit_code=3)
        return

    branch = branch_name_for(str(change).strip())
    exists = _branch_exists(repo_root, branch, runner)

    if exists:
        argv = ["git", "checkout", branch]
        created = False
    else:
        argv = ["git", "checkout", "-b", branch]
        created = True

    proc = runner(argv, cwd=str(repo_root), capture_output=True, text=True)
    if proc.returncode != 0:
        _io.emit(
            {
                "ok": False,
                "branch": branch,
                "created": created,
                "error": "git_checkout_failed",
                "stderr": (proc.stderr or "").strip(),
            }
        )
        raise SystemExit(1)

    _io.emit({"ok": True, "branch": branch, "created": created})


# ============================================================
# 子命令 2：npc git ensure-clean
# ============================================================


def parse_porcelain(stdout: str) -> list[str]:
    """解析 ``git status --porcelain`` 输出为脏文件路径列表（纯函数）。

    porcelain 每行形如 ``XY <path>``（前两列状态码 + 空格 + 路径）。
    重命名 ``R  old -> new`` 取 new 路径侧；空行忽略。
    """
    files: list[str] = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        # porcelain v1：前两列状态码，第 3 列为分隔空格，其后为路径
        path = line[3:] if len(line) > 3 else line.strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path.strip())
    return files


def cli_ensure_clean(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc git ensure-clean``：判定工作区是否干净。

    ``git status --porcelain`` 空 = clean。脏 → exit 1（仍打印 JSON 含 dirty_files）。
    退出码：clean → 0；脏 → 1；非 git 仓库 → 3。
    """
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("not_a_repo", f"未能定位 repo_root（非 git 仓库？）：{e}", exit_code=3)
        return

    proc = runner(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    dirty_files = parse_porcelain(proc.stdout or "")
    clean = len(dirty_files) == 0
    _io.emit({"ok": clean, "clean": clean, "dirty_files": dirty_files})
    if not clean:
        raise SystemExit(1)


# ============================================================
# 子命令 3：npc git commit
# ============================================================


def cli_commit(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc git commit``：git add -A + git commit。

    消息：``--message`` 优先；否则由 ``--change`` / ``--phase`` 派生。
    无改动可提交 → committed=false，exit 0（不算失败）。
    退出码：成功 0；缺消息且无 change/phase → 2；非 git 仓库 → 3；commit 失败 → 1。
    """
    message = getattr(args, "message", None)
    change = getattr(args, "change", None)
    phase = getattr(args, "phase", None)

    final_msg = derive_commit_message(message, change, phase)
    if final_msg is None:
        _io.emit_error(
            "usage_error",
            "commit 需要 --message，或 --change（可选 --phase）以派生消息",
            exit_code=2,
        )
        return

    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("not_a_repo", f"未能定位 repo_root（非 git 仓库？）：{e}", exit_code=3)
        return

    # 暂存全部改动
    add_proc = runner(
        ["git", "add", "-A"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if add_proc.returncode != 0:
        _io.emit(
            {
                "ok": False,
                "committed": False,
                "error": "git_add_failed",
                "stderr": (add_proc.stderr or "").strip(),
            }
        )
        raise SystemExit(1)

    commit_proc = runner(
        ["git", "commit", "-m", final_msg],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if commit_proc.returncode != 0:
        combined = (commit_proc.stdout or "") + (commit_proc.stderr or "")
        if "nothing to commit" in combined.lower():
            _io.emit(
                {"ok": True, "committed": False, "reason": "nothing-to-commit"}
            )
            return
        _io.emit(
            {
                "ok": False,
                "committed": False,
                "error": "git_commit_failed",
                "stderr": (commit_proc.stderr or "").strip(),
            }
        )
        raise SystemExit(1)

    commit_hash = _head_hash(repo_root, runner)
    branch = _current_branch(repo_root, runner)
    _io.emit(
        {
            "ok": True,
            "committed": True,
            "commit": commit_hash,
            "message": final_msg,
            "branch": branch,
        }
    )
