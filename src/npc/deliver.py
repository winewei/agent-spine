"""npc deliver / pr open —— 对外交付的机械命令。

定位：这是 npc 笼子里少数的"对外动作"。push 与开 PR 都会把工作推到远端、
对外可见，因此 npc 只提供**纯机械命令**——拼 argv、跑子进程、解析输出——
**绝不自作主张**决定要不要推。要不要交付由上层 skill 的人闸拍板，本模块只在
被显式调用时执行一次确定性动作。

两个子命令：

- ``npc deliver``：把当前分支 push 到远程。
- ``npc pr open``：用 ``gh pr create`` 开 PR，解析回 PR url。

退出码（与其余 npc 子命令对齐）：成功 0 / 业务失败 1 / 用法 2 /
非 git 仓库 3 / 依赖缺失（git/gh 未装）4。所有子进程经可注入 runner，便于单测。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

from . import _io
from . import paths as _paths


# 子进程 stderr 失败时回报的尾段长度（行）
STDERR_TAIL_LINES = 40

# gh pr create 成功后 stdout 里 PR url 的形态：
#   https://github.com/<owner>/<repo>/pull/<n>
_PR_URL_RE = re.compile(r"https://\S+?/pull/\d+")

# 凭据脱敏：URL 里 userinfo（``https://<token>@host``）形态的内嵌凭据，
# 在 stderr 回报前一律抹掉，绝不把 token 泄露进 JSON 输出。
_CRED_RE = re.compile(r"https?://[^@\s/]+@")


# ============================================================
# 共享：repo 定位（便于测试 monkeypatch，与 verify.py 同款）
# ============================================================


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """定位 repo_root。deliver 只需 git 仓库（无需 active run / npc init）：

    优先 git toplevel；仅当 cwd 不在 git 仓库时回退 load_paths（兼容显式 --run-ts）。
    """
    try:
        return _paths.detect_repo_root()
    except _paths.PathsError:
        return _paths.load_paths(args).repo_root


def _which(name: str) -> str | None:
    """查找可执行；包一层便于测试 monkeypatch。"""
    return shutil.which(name)


def _current_branch(repo_root: Path, runner=subprocess.run) -> str | None:
    """``git rev-parse --abbrev-ref HEAD`` 取当前分支名；失败/游离 HEAD 返回 None。

    子进程本身起不来（git 缺失/超时等）属于环境异常，转结构化错误（exit 1），
    绝不裸抛 OSError/TimeoutExpired 破坏 stdout 的单行 JSON 契约。
    """
    try:
        proc = runner(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _io.emit_error("subprocess_error", str(e), exit_code=1)
        return None
    if proc.returncode != 0:
        return None
    branch = (proc.stdout or "").strip()
    if not branch or branch == "HEAD":  # 空 / 游离 HEAD（detached）
        return None
    return branch


# ============================================================
# 纯函数：argv 构造（便于单测各分支）
# ============================================================


def build_push_argv(remote: str, branch: str, set_upstream: bool) -> list[str]:
    """构造 ``git push [-u] <remote> <branch>`` 的 argv（纯函数）。"""
    argv = ["git", "push"]
    if set_upstream:
        argv.append("-u")
    argv.extend([remote, branch])
    return argv


def build_gh_argv(
    title: str | None,
    body: str | None,
    base: str | None,
    draft: bool,
) -> list[str]:
    """构造 ``gh pr create ...`` 的 argv（纯函数）。

    - ``title`` 给定 → ``--title``。
    - ``body`` 非 None → ``--body``（``body_file`` 的读取在 :func:`run_pr_open`
      中完成并解析为 body 后传入，故此处只认已解析的 body）。
    - ``base`` 给定 → ``--base``。
    - ``draft`` 为真 → ``--draft``。

    注意：title/body 都缺时不强行注入空值，交给 gh 自己处理（gh 在非交互环境会
    自行报错），保持 npc 不自作主张的语义。
    """
    argv = ["gh", "pr", "create"]
    if title:
        argv.extend(["--title", title])
    if body is not None:
        argv.extend(["--body", body])
    if base:
        argv.extend(["--base", base])
    if draft:
        argv.append("--draft")
    return argv


def parse_pr_url(stdout: str) -> str | None:
    """从 ``gh pr create`` 的 stdout 里解析 PR url（纯函数）。"""
    if not stdout:
        return None
    m = _PR_URL_RE.search(stdout)
    return m.group(0) if m else None


def _stderr_tail(stderr: str, lines: int = STDERR_TAIL_LINES) -> str:
    """回报 stderr 末尾若干行，并对内嵌凭据脱敏。

    先按行取末尾（而非字符切，避免把多字节字符/单词从中间截断），再把
    ``https://<token>@host`` 形态的内嵌凭据替换为 ``https://<redacted>@``。
    """
    redacted = _CRED_RE.sub("https://<redacted>@", stderr or "")
    rows = redacted.splitlines()
    return "\n".join(rows[-lines:]).strip()


# ============================================================
# 子命令 1：npc deliver（push）
# ============================================================


def run_push(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc deliver``：把当前分支 push 到远程。

    ``runner`` 可注入（默认 :func:`subprocess.run`）。
    退出码：成功 → 0；push 失败 → 1；缺 git → 4；非 git 仓库 → 3。
    """
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("not_a_repo", f"未能定位 repo_root：{e}", exit_code=3)
        return

    if _which("git") is None:
        _io.emit_error("dependency_missing", "未在 PATH 中找到 git 命令", exit_code=4)
        return

    remote = getattr(args, "remote", None) or "origin"
    branch = getattr(args, "branch", None)
    if not branch:
        branch = _current_branch(repo_root, runner=runner)
    if not branch:
        _io.emit_error(
            "invalid_args",
            "未能确定当前分支（游离 HEAD？）；请显式传 --branch",
            exit_code=2,
        )
        return

    set_upstream = getattr(args, "set_upstream", True)
    argv = build_push_argv(remote, branch, set_upstream)
    try:
        proc = runner(
            argv,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _io.emit_error("subprocess_error", str(e), exit_code=1)
        return
    if proc.returncode != 0:
        _io.emit(
            {
                "ok": False,
                "remote": remote,
                "branch": branch,
                "pushed": False,
                "error": "push_failed",
                "stderr_tail": _stderr_tail(proc.stderr or ""),
            }
        )
        raise SystemExit(1)

    _io.emit({"ok": True, "remote": remote, "branch": branch, "pushed": True})


# ============================================================
# 子命令 2：npc pr open（gh pr create）
# ============================================================


def run_pr_open(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc pr open``：用 ``gh pr create`` 开 PR，解析回 PR url。

    ``runner`` 可注入。退出码：成功 → 0；gh 失败 → 1；缺 gh → 4；非 git 仓库 → 3；
    body_file 读取失败 → 2。
    """
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("not_a_repo", f"未能定位 repo_root：{e}", exit_code=3)
        return

    if _which("gh") is None:
        _io.emit_error(
            "dependency_missing",
            "未在 PATH 中找到 gh 命令；请先安装 GitHub CLI",
            exit_code=4,
        )
        return

    title = getattr(args, "title", None)
    body = getattr(args, "body", None)
    body_file = getattr(args, "body_file", None)
    base = getattr(args, "base", None)
    draft = bool(getattr(args, "draft", False))

    # body_file 优先于 --body：从文件读 body（如 run-summary.md）
    if body_file:
        bf = Path(body_file)
        try:
            body = bf.read_text(encoding="utf-8")
        except OSError as e:
            _io.emit_error(
                "invalid_args",
                f"读取 --body-file 失败：{body_file}：{e}",
                exit_code=2,
            )
            return

    argv = build_gh_argv(title, body, base, draft)
    try:
        proc = runner(
            argv,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _io.emit_error("subprocess_error", str(e), exit_code=1)
        return
    if proc.returncode != 0:
        _io.emit(
            {
                "ok": False,
                "title": title,
                "error": "gh_pr_create_failed",
                "stderr_tail": _stderr_tail(proc.stderr or ""),
            }
        )
        raise SystemExit(1)

    stdout = proc.stdout or ""
    pr_url = parse_pr_url(stdout)
    payload: dict = {"ok": True, "pr_url": pr_url, "title": title}
    if pr_url is None:
        # gh 报成功但 stdout 里没解析到 PR url：不静默 ok+null（人无从取 url），
        # 给 warn 提示 + 带上 stdout 末尾原文，便于人工捞 url。
        _io.warn("gh pr create 成功但未能从 stdout 解析到 PR url；见 raw_stdout_tail")
        payload["raw_stdout_tail"] = "\n".join(stdout.splitlines()[-STDERR_TAIL_LINES:]).strip()
    _io.emit(payload)


# ============================================================
# CLI handler 入口（与 cli.py 对接的稳定签名）
# ============================================================


def cli_deliver(args: argparse.Namespace) -> None:
    """``npc deliver`` handler：push 当前分支到远程。"""
    run_push(args)


def cli_pr_open(args: argparse.Namespace) -> None:
    """``npc pr open`` handler：开 PR。"""
    run_pr_open(args)
