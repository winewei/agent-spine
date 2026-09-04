"""npc doctor：环境前置体检（基础工具）。

把"跑 npc 之前需要满足的一切前置条件"汇成一份结构化体检报告：

- 必备/可选可执行文件是否在 PATH（git / openspec / codex / claude / jq /
  portable-timeout）；
- 跨项目共享的 review schema 是否已自举；
- 成本路由 ``mimo.env`` 是否就绪（缺失只降级 warn，不视为 missing）；
- npc 配置是否能正常加载（失败降级 warn，不阻塞）；
- 路由在用的 coder provider 是否就绪（env_file 可读 + runner 可执行文件）；
- 工程级 ``docs/principles.md`` 是否在（warn 级）；
- 安装来源（本地 checkout / 远程 VCS）：本地安装时校验源码版本与已安装版本一致、
  且源码 HEAD 已合入 main（未合入的分支构建物用于生产 → warn）。

设计成"纯函数核 + 薄 handler"：:func:`gather_checks` 不做任何 I/O 输出、可注入
``which`` / ``home`` / ``repo_root``，便于单测；:func:`run` 只负责探测 repo_root、
调核、emit JSON、按 required 缺失决定退出码。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

from . import _io, config as _config, hosts as _hosts, paths as _paths


# (name, required) —— 在 PATH 中可执行文件的体检清单
_BIN_CHECKS: tuple[tuple[str, bool], ...] = (
    ("git", True),
    ("openspec", False),
    ("codex", False),
    ("claude", False),
    ("jq", False),
)

# 可执行文件状态：required 缺失记 "missing"，可选缺失记 "warn"
def _bin_status(found: bool, required: bool) -> str:
    if found:
        return "ok"
    return "missing" if required else "warn"


def _check_bin(name: str, *, required: bool, which) -> dict:
    """通用 PATH 可执行文件检查。"""
    resolved = which(name)
    found = resolved is not None
    status = _bin_status(found, required)
    detail = f"已找到：{resolved}" if found else f"未在 PATH 中找到 {name}"
    return {"name": name, "status": status, "detail": detail, "required": required}


def _check_portable_timeout(*, home: Path, which) -> dict:
    """portable-timeout：先查 PATH，再查 ~/.local/bin/portable-timeout。"""
    resolved = which("portable-timeout")
    if resolved is not None:
        return {
            "name": "portable-timeout",
            "status": "ok",
            "detail": f"已找到（PATH）：{resolved}",
            "required": False,
        }
    fallback = home / ".local" / "bin" / "portable-timeout"
    if fallback.is_file():
        if os.access(fallback, os.X_OK):
            return {
                "name": "portable-timeout",
                "status": "ok",
                "detail": f"已找到（自举位置）：{fallback}",
                "required": False,
            }
        return {
            "name": "portable-timeout",
            "status": "warn",
            "detail": f"portable-timeout 存在但不可执行（缺执行位）：{fallback}；运行 chmod +x 或 npc init 修复",
            "required": False,
        }
    return {
        "name": "portable-timeout",
        "status": "warn",
        "detail": "未找到 portable-timeout（PATH 与 ~/.local/bin 均无）；运行 npc init 自举",
        "required": False,
    }


def _check_schema(*, home: Path) -> dict:
    """review schema 文件是否已落盘。"""
    schema_path = home / "task_log" / _paths.SCHEMA_FILENAME
    if schema_path.is_file():
        if not os.access(schema_path, os.R_OK):
            return {
                "name": "schema",
                "status": "warn",
                "detail": f"review schema 存在但不可读：{schema_path}；检查文件权限",
                "required": False,
            }
        try:
            json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {
                "name": "schema",
                "status": "warn",
                "detail": f"review schema 存在但非法（JSON 解析失败）：{schema_path}：{e}",
                "required": False,
            }
        return {
            "name": "schema",
            "status": "ok",
            "detail": f"已存在：{schema_path}",
            "required": False,
        }
    return {
        "name": "schema",
        "status": "warn",
        "detail": f"review schema 缺失：{schema_path}；运行 npc init 自举",
        "required": False,
    }


def _check_mimo_env(*, home: Path) -> dict:
    """成本路由 mimo.env：存在则 ok 并标注成本路由可用，缺失为 warn（非 missing）。"""
    mimo_env = home / ".config" / "npc" / "mimo.env"
    if mimo_env.is_file():
        if not os.access(mimo_env, os.R_OK):
            return {
                "name": "mimo.env",
                "status": "warn",
                "detail": f"成本路由 mimo.env 存在但不可读：{mimo_env}；检查文件权限",
                "required": False,
            }
        return {
            "name": "mimo.env",
            "status": "ok",
            "detail": f"成本路由可用：{mimo_env}",
            "required": False,
        }
    return {
        "name": "mimo.env",
        "status": "warn",
        "detail": f"成本路由 mimo.env 缺失：{mimo_env}；coder 将走默认 premium 层",
        "required": False,
    }


def _check_config(*, home: Path, repo_root: Path) -> dict:
    """npc config 可加载性；加载失败降级 warn，不阻塞。"""
    try:
        cfg = _config.load_config(repo_root, home=home)
    except (_config.ConfigError, OSError, Exception) as e:
        return {
            "name": "config",
            "status": "warn",
            "detail": f"配置加载失败（将用内置默认）：[{type(e).__name__}] {e}",
            "required": False,
        }
    if cfg.source == "<default>":
        detail = "使用内置默认配置（未找到配置文件）"
    else:
        detail = f"配置可加载：{cfg.source}"
    return {
        "name": "config",
        "status": "ok",
        "detail": detail,
        "required": False,
    }


def _check_providers(*, home: Path, repo_root: Path, which) -> dict:
    """路由实际引用到的 provider 是否就绪（env_file 可读 + runner 可执行文件在 PATH）。

    只检查 coder 路由在用的 provider（effective + per-phase），未被引用的定义
    不产生噪音。任何一项不就绪降级 warn（doctor 不因可选后端阻塞）。
    """
    try:
        cfg = _config.load_config(repo_root, home=home)
    except Exception:
        return {
            "name": "providers",
            "status": "warn",
            "detail": "配置加载失败，跳过 provider 检查（见 config 检查项）",
            "required": False,
        }

    in_play = {cfg.coder.effective_backend}
    in_play.update(be for _ph, be in cfg.coder.phase_backends)

    problems: list[str] = []
    descriptions: list[str] = []
    runner_bins = {"claude-cli": "claude", "codex-cli": "codex"}
    for name in sorted(in_play):
        p = cfg.provider(name)
        if p is None:
            problems.append(f"{name}: 未注册（内置或 [providers.*]）")
            continue
        desc = f"{name}({p.runner}"
        if p.model:
            desc += f", model={p.model}"
        desc += ")"
        descriptions.append(desc)
        bin_name = p.bin or cfg.coder.bin or runner_bins[p.runner]
        if which(bin_name) is None and not Path(bin_name).expanduser().is_file():
            problems.append(f"{name}: 可执行文件 {bin_name} 不可用")
        if p.env_file:
            env_path = Path(p.env_file).expanduser()
            if not env_path.is_file():
                problems.append(f"{name}: env_file 缺失 {env_path}")
            elif not os.access(env_path, os.R_OK):
                problems.append(f"{name}: env_file 不可读 {env_path}")

    if problems:
        return {
            "name": "providers",
            "status": "warn",
            "detail": f"provider 未就绪：{'; '.join(problems)}",
            "required": False,
        }
    return {
        "name": "providers",
        "status": "ok",
        "detail": f"路由在用 provider 就绪：{', '.join(descriptions)}",
        "required": False,
    }


def _check_host(*, home: Path, repo_root: Path, env: dict | None = None) -> dict:
    """宿主解析结果（信息级，永不 missing）：名字、来源、session 识别能力。"""
    try:
        cfg = _config.load_config(repo_root, home=home)
        host = _hosts.resolve_host_from_config(cfg, env=env)
    except Exception:
        host = _hosts.resolve_host(env=env)
    if host.session_dir_template:
        cap = f"session 目录模板 {host.session_dir_template}"
    else:
        cap = "无 session 目录（session 识别仅走 by-cwd hook 索引）"
    return {
        "name": "host",
        "status": "ok",
        "detail": f"宿主 {host.name}（来源 {host.source}）；{cap}",
        "required": False,
    }


def _check_principles(*, repo_root: Path | None) -> dict:
    """工程级 docs/principles.md 是否在（warn 级）。"""
    if repo_root is None:
        return {
            "name": "principles.md",
            "status": "warn",
            "detail": "无法定位 repo_root，跳过 docs/principles.md 检查",
            "required": False,
        }
    principles = repo_root / "docs" / "principles.md"
    if principles.is_file():
        return {
            "name": "principles.md",
            "status": "ok",
            "detail": f"已存在：{principles}",
            "required": False,
        }
    return {
        "name": "principles.md",
        "status": "warn",
        "detail": f"docs/principles.md 缺失：{principles}",
        "required": False,
    }


_VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


def _install_check(status: str, detail: str) -> dict:
    return {"name": "install-source", "status": status, "detail": detail, "required": False}


def _git_out(repo: Path, *argv: str, run) -> str | None:
    """在 repo 下跑一条 git 命令，取 stdout（失败/超时/异常一律 None）。"""
    try:
        proc = run(
            ["git", *argv], cwd=str(repo), capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def _git_ok(repo: Path, *argv: str, run) -> bool:
    """只关心退出码的 git 命令（如 merge-base --is-ancestor）。"""
    try:
        proc = run(
            ["git", *argv], cwd=str(repo), capture_output=True, text=True, timeout=5
        )
    except Exception:
        return False
    return proc.returncode == 0


def _source_version(src: Path) -> str | None:
    """从源码 ``src/npc/__init__.py`` 抽 ``__version__``。"""
    try:
        text = (src / "src" / "npc" / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def _check_install_source(*, which, run=subprocess.run) -> dict:
    """安装来源体检：本地 checkout 安装是否来自已合入 main 的源码、版本是否一致。

    动机：`uv tool install --from <本地路径>` 会把未合并分支的构建物装到全局，
    此后 doctor 报告的能力与仓库 main 不一致（"机器上能跑但仓库里没有"）。
    """
    try:
        dist = importlib.metadata.distribution("npc")
        installed_version = dist.version
        raw = dist.read_text("direct_url.json")
    except Exception:
        return _install_check("warn", "无法读取安装来源（非 pip/uv 安装？）")
    if not raw:
        return _install_check("warn", "无法读取安装来源（非 pip/uv 安装？）")
    try:
        info = json.loads(raw)
    except ValueError:
        return _install_check("warn", "无法读取安装来源（direct_url.json 非法 JSON）")
    url = (info.get("url") or "").strip()
    if not url:
        return _install_check("warn", "无法读取安装来源（direct_url.json 缺 url 字段）")

    if not url.startswith("file://"):
        vcs = info.get("vcs_info") or {}
        commit = (vcs.get("commit_id") or "")[:7]
        suffix = f" @ {commit}" if commit else ""
        return _install_check("ok", f"远程安装：{url}{suffix}（已安装 {installed_version}）")

    src = Path(unquote(url[len("file://"):]))
    if not src.is_dir():
        return _install_check("warn", f"本地安装源码目录已不存在：{src}；无法校验与 main 的一致性")
    if which("git") is None:
        return _install_check("warn", f"本地安装：{src}；PATH 中无 git，无法校验分支与 main 的关系")

    branch = _git_out(src, "rev-parse", "--abbrev-ref", "HEAD", run=run)
    sha = _git_out(src, "rev-parse", "--short", "HEAD", run=run)
    if branch is None or sha is None:
        return _install_check("warn", f"本地安装：{src}；git 信息不可读（非 git 仓库或命令失败）")

    on_main = _git_ok(src, "merge-base", "--is-ancestor", "HEAD", "origin/main", run=run) or _git_ok(
        src, "merge-base", "--is-ancestor", "HEAD", "main", run=run
    )
    if branch != "main" or not on_main:
        return _install_check(
            "warn",
            f"已安装 {installed_version} 构建自 {src} 分支 {branch}"
            f"（{sha}，未合入 main）——请先合并再用于生产",
        )

    src_version = _source_version(src)
    if src_version is not None and src_version != installed_version:
        return _install_check(
            "warn",
            f"版本不一致：已安装 {installed_version}，源码 {src} 为 {src_version}"
            f"（{branch} {sha}）；运行 uv tool install --reinstall 重装",
        )
    return _install_check(
        "ok", f"本地安装：{src} @ {branch} {sha}，版本一致 ({installed_version})"
    )


def gather_checks(
    *,
    home: Path,
    repo_root: Path | None,
    which=shutil.which,
) -> list[dict]:
    """纯函数核：返回全部体检项。

    每项形如 ``{"name", "status", "detail", "required"}``，
    其中 ``status`` ∈ {"ok", "missing", "warn"}。不做任何输出，便于单测。

    config 检查需要 repo_root；缺省时回退到 cwd（由调用方在 run 中探测后传入）。
    """
    checks: list[dict] = []
    for name, required in _BIN_CHECKS:
        checks.append(_check_bin(name, required=required, which=which))
    checks.append(_check_portable_timeout(home=home, which=which))
    checks.append(_check_schema(home=home))
    checks.append(_check_mimo_env(home=home))
    cfg_root = repo_root if repo_root is not None else Path.cwd()
    checks.append(_check_config(home=home, repo_root=cfg_root))
    checks.append(_check_providers(home=home, repo_root=cfg_root, which=which))
    checks.append(_check_host(home=home, repo_root=cfg_root))
    checks.append(_check_install_source(which=which, run=subprocess.run))
    checks.append(_check_principles(repo_root=repo_root))
    return checks


def summarize(checks: list[dict]) -> dict:
    """把 checks 聚合为 summary：各状态计数 + 缺失的 required 名单。"""
    ok = sum(1 for c in checks if c["status"] == "ok")
    warn = sum(1 for c in checks if c["status"] == "warn")
    missing = sum(1 for c in checks if c["status"] == "missing")
    missing_required = [
        c["name"] for c in checks if c["required"] and c["status"] == "missing"
    ]
    return {
        "ok": ok,
        "warn": warn,
        "missing": missing,
        "missing_required": missing_required,
    }


def build_report(checks: list[dict]) -> dict:
    """组装最终 JSON 报告。ok 当且仅当无 required 缺失。"""
    summary = summarize(checks)
    return {
        "ok": not summary["missing_required"],
        "checks": checks,
        "summary": summary,
    }


def run(args: argparse.Namespace) -> None:
    """doctor 主入口：探测 repo_root → 体检 → emit 单行 JSON → 按 required 决定退出码。

    任一 required 项缺失：把完整报告（含 ``error``/``message`` 字段，调用方据此知道
    缺哪个）作为**唯一一行 JSON** emit 出去，随后以 exit 4（外部依赖缺失）退出。
    严守「stdout 单行 JSON」契约——不再追加第二行 error 体。
    """
    home = Path.home()

    # repo_root 探测失败不致命：principles/config 降级处理
    try:
        repo_root: Path | None = _paths.detect_repo_root()
    except _paths.PathsError:
        repo_root = None

    checks = gather_checks(home=home, repo_root=repo_root, which=shutil.which)
    report = build_report(checks)

    missing_required = report["summary"]["missing_required"]
    if missing_required:
        report["error"] = "dependency_missing"
        report["message"] = f"缺少必备前置：{', '.join(missing_required)}"
        _io.emit(report)
        raise SystemExit(4)

    _io.emit(report)
