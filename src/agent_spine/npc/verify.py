"""npc verify —— 把"不裸信自报"做成确定性笼子。

两个子命令：

- ``npc verify tests``：真实复跑测试（质量门）。绝不读 LLM 的 RESULT 自报，
  而是在 repo_root 实际执行测试命令、捕获退出码与输出末尾，emit 结构化判定。
  这是"不裸信 RESULT"硬轨的家。

- ``npc verify routing``：把路由不变量编进代码（生成⊥验证 + MiMo 只许执行）。
  纯函数 :func:`check_routing` 校验 coder/review 后端配置，发现"自己评自己"
  或"MiMo 越权到 review"等违规则报 violation。
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path

from . import _io
from . import config as _config
from . import paths as _paths


# 输出末尾保留的行数（stdout/stderr 合并后取尾部）
TAIL_LINES = 30

# ============================================================
# 共享：repo 定位 + config 加载（便于测试 monkeypatch）
# ============================================================


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """定位 repo_root。verify 只需 git 仓库（无需 active run / npc init）：

    优先 git toplevel；仅当 cwd 不在 git 仓库时回退 load_paths（兼容显式 --run-ts 调试）。
    """
    try:
        return _paths.detect_repo_root()
    except _paths.PathsError:
        return _paths.load_paths(args).repo_root


def _load_cfg(repo_root: Path) -> _config.Config:
    """加载 npc 配置；失败抛 ConfigError。"""
    return _config.load_config(repo_root)


# ============================================================
# 子命令 1：npc verify tests
# ============================================================


def _has_make_test_target(makefile: Path) -> bool:
    """判断 Makefile 是否含 ``test:`` 目标（行首形如 ``test:`` 或 ``test :``）。"""
    try:
        text = makefile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        # 只认行首（列 0）的 ``test:`` / ``test :``，避免缩进的配方行误判。
        if line.startswith("test:") or line.startswith("test :"):
            return True
    return False


def _package_json_has_test_script(package_json: Path) -> bool:
    """判断 package.json 的 scripts.test 是否存在且非空。"""
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return False
    test = scripts.get("test")
    return isinstance(test, str) and bool(test.strip())


def resolve_test_cmd(repo_root: Path, cfg: _config.Config) -> str | None:
    """解析测试命令。纯函数，便于单测。

    优先级：
    1. ``cfg.verify.test`` 显式覆盖。
    2. Python：有 ``pyproject.toml`` 或 ``pytest.ini`` 或 ``tests/`` 目录
       → ``python3 -m pytest -q``。
    3. Node：有 ``package.json`` 且 ``scripts.test`` 非空 → ``npm test``。
    4. Make：有 ``Makefile`` 且含 ``test:`` 目标 → ``make test``。
    5. 都没有 → ``None``。
    """
    if cfg.verify.test:
        return cfg.verify.test

    if (
        (repo_root / "pyproject.toml").is_file()
        or (repo_root / "pytest.ini").is_file()
        or (repo_root / "tests").is_dir()
    ):
        return "python3 -m pytest -q"

    pkg = repo_root / "package.json"
    if pkg.is_file() and _package_json_has_test_script(pkg):
        return "npm test"

    makefile = repo_root / "Makefile"
    if makefile.is_file() and _has_make_test_target(makefile):
        return "make test"

    return None


def _tail(stdout: str, stderr: str, lines: int = TAIL_LINES) -> str:
    """合并 stdout/stderr 并取末尾 ``lines`` 行。"""
    combined = (stdout or "") + (stderr or "")
    rows = combined.splitlines()
    return "\n".join(rows[-lines:])


def run_tests(args: argparse.Namespace, runner=subprocess.run) -> None:
    """``npc verify tests``：在 repo_root 真实复跑测试命令。

    ``runner`` 可注入（默认 :func:`subprocess.run`），测试用假 runner。
    退出码：passed → 0（正常返回）；失败 → 1；无命令/定位失败 → 3。
    """
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    try:
        cfg = _load_cfg(repo_root)
    except _config.ConfigError as e:
        _io.emit_error("config_error", f"配置加载失败：{e}", exit_code=1)
        return

    cmd = resolve_test_cmd(repo_root, cfg)
    if cmd is None:
        _io.emit_error(
            "no_test_command",
            f"未能为 repo 探测到测试命令（无 pyproject/pytest.ini/tests/package.json/Makefile）：{repo_root}",
            exit_code=3,
        )
        return

    # 不裸信可写的 cfg.verify.test：用 shlex.split → argv 列表 + shell=False 执行，
    # 杜绝命令注入（``; rm -rf`` 等元字符不会被 shell 解释）。
    argv = shlex.split(cmd)
    proc = runner(
        argv,
        shell=False,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    passed = proc.returncode == 0
    _io.emit(
        {
            "ok": passed,
            "cmd": cmd,
            "exit_code": proc.returncode,
            "passed": passed,
            "tail": _tail(proc.stdout or "", proc.stderr or ""),
        }
    )
    if not passed:
        raise SystemExit(1)


# ============================================================
# 子命令 2：npc verify routing
# ============================================================


def _contains_mimo(value: str | None) -> bool:
    return value is not None and "mimo" in value.lower()


def check_routing(cfg: _config.Config) -> list[dict]:
    """校验路由不变量，返回 violations 列表（纯函数）。

    每项 ``{"rule", "detail"}``。规则：

    1. ``backend_unsupported`` / ``engine_unsupported``：coder.backend 与
       review.engine 必须在各自 SUPPORTED 列表（backend 用 effective_backend）。
    2. ``gen_not_orthogonal``：coder 与 review 解析到同一执行身份 → 等于自己评
       自己，违反 生成⊥验证。覆盖 (a) 都是 claude 且同 bin+model；(b) 都是 mimo。
    3. ``mimo_exec_only``：review 路由到 MiMo（engine 含 'mimo'，或 claude_model
       / claude_bin 含 'mimo'）→ 违反 MiMo 仅限 coder。合并为单条 violation。
    """
    violations: list[dict] = []
    coder = cfg.coder
    review = cfg.review
    effective_backend = coder.effective_backend

    # 规则 1：后端有效性（用 effective_backend，None 解析为 claude）
    if effective_backend not in _config.SUPPORTED_CODER_BACKENDS:
        violations.append(
            {
                "rule": "backend_unsupported",
                "detail": f"coder.backend={effective_backend!r} 不在支持列表 {_config.SUPPORTED_CODER_BACKENDS}",
            }
        )
    if review.engine not in _config.SUPPORTED_ENGINES:
        violations.append(
            {
                "rule": "engine_unsupported",
                "detail": f"review.engine={review.engine!r} 不在支持列表 {_config.SUPPORTED_ENGINES}",
            }
        )

    # 规则 2：gen ⊥ verify（coder 与 review 解析到同一执行身份 = 自己评自己）
    same_claude_identity = (
        effective_backend == "claude"
        and review.engine == "claude"
        and coder.bin == review.claude_bin
        and coder.model == review.claude_model
    )
    both_mimo = effective_backend == "mimo" and review.engine == "mimo"
    if same_claude_identity or both_mimo:
        violations.append(
            {
                "rule": "gen_not_orthogonal",
                "detail": "coder 与 review 解析到同一执行身份，等于自己评自己",
            }
        )

    # 规则 3：MiMo 只许执行（无条件顶层挡：engine 或 claude_bin/model 含 mimo）→ 单条
    if (
        _contains_mimo(review.engine)
        or _contains_mimo(review.claude_model)
        or _contains_mimo(review.claude_bin)
    ):
        violations.append(
            {
                "rule": "mimo_exec_only",
                "detail": "review 路由含 MiMo（engine/claude_bin/claude_model 含 'mimo'），违反 MiMo 仅限 coder",
            }
        )

    return violations


def run_routing(args: argparse.Namespace) -> None:
    """``npc verify routing``：emit 路由检查结果。

    退出码：无 violation → 0（正常返回）；有 → 1；config 加载失败 → 1。
    """
    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    try:
        cfg = _load_cfg(repo_root)
    except _config.ConfigError as e:
        _io.emit_error("config_error", f"配置加载失败：{e}", exit_code=1)
        return

    violations = check_routing(cfg)
    _io.emit(
        {
            "ok": len(violations) == 0,
            "coder_backend": cfg.coder.effective_backend,
            "review_engine": cfg.review.engine,
            "violations": violations,
        }
    )
    if violations:
        raise SystemExit(1)
