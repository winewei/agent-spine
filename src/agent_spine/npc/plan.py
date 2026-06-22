"""npc plan —— SDD 阶段前置门 + change 脚手架。

两个子命令：

- ``npc plan check``：在进入 implement 前，确定性校验 openspec change 的
  ``applyRequires`` 所列产物是否都已 ``done``。绝不裸信 LLM 自报"已就绪"，而是
  实际调 ``openspec status --change <id> --json`` 解析其产物状态，emit 结构化判定。

- ``npc plan new-change``：调 ``openspec new change <id>`` 生成 change 脚手架，
  成功后扫描生成目录列出文件，emit 结构化结果。

退出码约定（与其余 npc 命令一致）：
- 0：ready / 成功
- 1：not ready / openspec 调用失败 / 非法 JSON
- 2：缺必需参数（--change）
- 3：非 git 仓库（repo 定位失败）
- 4：openspec 依赖缺失
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from . import _io
from . import paths as _paths


# ============================================================
# 共享：repo 定位 + openspec 发现（便于测试 monkeypatch）
# ============================================================


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """定位 repo_root。plan 只需 git 仓库（无需 active run / npc init）：

    优先 git toplevel；仅当 cwd 不在 git 仓库时回退 load_paths（兼容显式 --run-ts 调试）。
    """
    try:
        return _paths.detect_repo_root()
    except _paths.PathsError:
        return _paths.load_paths(args).repo_root


def _find_openspec_bin(override: str | None = None) -> str:
    """在 PATH 中发现 openspec 命令；找不到抛 FileNotFoundError（→ exit 4）。"""
    if override:
        return override
    p = shutil.which("openspec")
    if not p:
        raise FileNotFoundError("未在 PATH 中找到 openspec 命令")
    return p


# ============================================================
# 子命令 1：npc plan check
# ============================================================


def _parse_status_payload(payload: dict, apply_requires: list[str]) -> list[str]:
    """从 openspec status 的 artifacts 计算未 done 的 applyRequires 产物 id。

    纯函数：``artifacts`` 每项形如 ``{"id": ..., "status": ...}``；返回 missing 列表
    （applyRequires 中 status != "done" 或根本不存在于 artifacts 的产物 id）。
    """
    artifacts = payload.get("artifacts") or []
    status_by_id: dict[str, str] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        aid = item.get("id")
        if isinstance(aid, str):
            status_by_id[aid] = item.get("status")
    missing: list[str] = []
    for req in apply_requires:
        if status_by_id.get(req) != "done":
            missing.append(req)
    return missing


def run_check(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc plan check``：openspec status 解析 applyRequires 产物就绪度。

    ``runner`` 可注入（默认 :func:`subprocess.run`），测试用假 runner 返回预设 stdout。
    退出码：ready → 0；not ready → 1；openspec 缺失 → 4；
    openspec 调用失败/非法 JSON → 1；缺 --change → 2；非 git 仓库 → 3。
    """
    change = getattr(args, "change", None)
    if not change:
        _io.emit_error("invalid_args", "必须提供 --change", exit_code=2)
        return

    phase = getattr(args, "phase", None) or "implement"

    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    try:
        osp = _find_openspec_bin(getattr(args, "openspec_bin", None))
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return

    proc = runner(
        [osp, "status", "--change", change, "--json"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        _io.emit_error(
            "openspec_failed",
            f"openspec status 失败（exit={proc.returncode}）：{(proc.stderr or '').strip()[-1000:]}",
            exit_code=1,
        )
        return

    try:
        payload = json.loads(proc.stdout or "")
    except json.JSONDecodeError as e:
        _io.emit_error(
            "invalid_json",
            f"openspec status 输出不是合法 JSON：{e}",
            exit_code=1,
        )
        return
    if not isinstance(payload, dict):
        _io.emit_error(
            "invalid_json",
            "openspec status 输出顶层不是 JSON 对象",
            exit_code=1,
        )
        return

    apply_requires = payload.get("applyRequires") or []
    if not isinstance(apply_requires, list):
        apply_requires = []
    apply_requires = [a for a in apply_requires if isinstance(a, str)]

    missing = _parse_status_payload(payload, apply_requires)
    ready = len(missing) == 0
    _io.emit(
        {
            "ok": ready,
            "change": change,
            "phase": phase,
            "ready": ready,
            "apply_requires": apply_requires,
            "missing": missing,
        }
    )
    if not ready:
        raise SystemExit(1)


# ============================================================
# 子命令 2：npc plan new-change
# ============================================================


def _scaffold_files(change_dir: Path) -> list[str]:
    """扫描 change 目录，返回相对 change_dir 的文件路径列表（排序、含子目录）。"""
    if not change_dir.is_dir():
        return []
    files = [
        str(p.relative_to(change_dir))
        for p in sorted(change_dir.rglob("*"))
        if p.is_file()
    ]
    return files


def run_new_change(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc plan new-change``：调 openspec new change 生成脚手架并列出文件。

    ``runner`` 可注入（默认 :func:`subprocess.run`）。
    退出码：成功 → 0；openspec 缺失 → 4；openspec 失败 → 1（error 带 stderr 尾段）；
    缺 --change → 2；非 git 仓库 → 3。
    """
    change = getattr(args, "change", None)
    if not change:
        _io.emit_error("invalid_args", "必须提供 --change", exit_code=2)
        return

    description = getattr(args, "description", None)
    schema = getattr(args, "schema", None)

    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    try:
        osp = _find_openspec_bin(getattr(args, "openspec_bin", None))
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return

    cmd = [osp, "new", "change", change]
    if description:
        cmd += ["--description", description]
    if schema:
        cmd += ["--schema", schema]

    proc = runner(
        cmd,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        _io.emit_error(
            "openspec_failed",
            f"openspec new change 失败（exit={proc.returncode}）：{(proc.stderr or '').strip()[-1000:]}",
            exit_code=1,
        )
        return

    change_dir = repo_root / "openspec" / "changes" / change
    files = _scaffold_files(change_dir)
    _io.emit(
        {
            "ok": True,
            "change": change,
            "path": str(change_dir),
            "files": files,
        }
    )


# ============================================================
# CLI handler 入口
# ============================================================


def cli_check(args: argparse.Namespace) -> None:
    """``npc plan check`` handler。"""
    run_check(args)


def cli_new_change(args: argparse.Namespace) -> None:
    """``npc plan new-change`` handler。"""
    run_new_change(args)
