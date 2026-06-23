"""Coder（执行体）子进程编排：把 implement / fix 阶段的 coder 子进程折进 npc。

对标 ``pipeline.run_review_round``，但方向相反——review 是验证闸门（恒留 premium
引擎），coder 是 bulk 生成工作（默认路由到廉价层 MiMo）。本模块是 Python 等价的
``spine-coder-mimo.sh``：

1. ``_do_phase_enter`` 标记 phase 进入
2. 复用 agent 模板渲染 prompt 文件 + 取引导语
3. 跑 backend 子进程（claude / mimo / codex），从 stdout 抽末尾 RESULT 行
4. 喂给 ``pipeline.record_implement`` / ``record_fix`` 完成状态装订

设计纪律（见 docs/principles.md 不变量 #1 生成⊥验证）：本模块只跑 coder（生成）。
review（验证闸门）绝不路由到与 coder 同源的后端。

测试注入点：``run_implement`` / ``run_fix`` 接受 ``runner`` 参数（默认
:func:`_default_runner`），便于在测试里注入假 runner，全程不真实调用 claude/codex。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import _io, agent as _agent, config, paths as _paths, pipeline as _pipeline, templates
from .config import Config, load_config
from .state import read_state


DEFAULT_MIMO_ENV_FILE = Path.home() / ".config" / "npc" / "mimo.env"
DEFAULT_MIMO_MODEL = "mimo-v2.5-pro"


# ============================================================
# Backend 抽象
# ============================================================


@dataclass(frozen=True)
class CoderRunResult:
    """coder 子进程一次执行的结果。"""

    stdout: str
    exit_code: int


# runner 签名：跑一个命令，注入 env 与 cwd，返回 CoderRunResult。
Runner = Callable[..., CoderRunResult]


def resolve_backend(cfg: Config, phase: str, override: str | None = None) -> str:
    """决定某 phase 的 coder backend。

    优先级：
    1. ``override``（CLI ``--backend``）
    2. per-phase 覆盖 ``[coder.phase].<phase>``（如只把 fix 给 mimo）
    3. 全局 ``[coder].backend``
    4. 默认 ``claude``

    **MiMo 默认不启用**：只有在 ``--backend mimo`` / ``[coder].backend="mimo"`` /
    ``[coder.phase].<phase>="mimo"`` 显式指定时才用 MiMo。mimo.env 是否存在不再
    自动触发路由（MiMo 较慢，按需开启）。
    """
    if override:
        return override
    return cfg.coder.backend_for_phase(phase) or "claude"


def _resolve_mimo_env_file(cfg: Config) -> Path:
    """解析 mimo_env_file 路径：配置覆盖 → 默认 ~/.config/npc/mimo.env。"""
    if cfg.coder.mimo_env_file:
        return Path(cfg.coder.mimo_env_file).expanduser()
    return DEFAULT_MIMO_ENV_FILE


def parse_env_file(text: str) -> dict[str, str]:
    """解析 mimo.env 形态的 ``export K=V`` 行为 dict。

    支持：
    - ``export K=V`` 与裸 ``K=V``
    - 行首空白、注释行（``#`` 开头）、空行
    - 值两侧的单/双引号会被剥除
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


# ============================================================
# 子进程发现与执行
# ============================================================


def _find_bin(name: str, override: str | None = None) -> str:
    if override:
        return override
    p = shutil.which(name)
    if not p:
        raise FileNotFoundError(
            f"未在 PATH 中找到 {name} 命令；请安装或在 [coder] bin 指定"
        )
    return p


def _default_runner(
    *,
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
) -> CoderRunResult:
    """生产用 runner：跑子进程并捕获 stdout。测试里以假 runner 替换之。"""
    proc = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CoderRunResult(stdout=proc.stdout, exit_code=proc.returncode)


def _build_claude_argv(claude_bin: str, prompt: str, model: str | None) -> list[str]:
    argv = [claude_bin, "-p", prompt]
    if model:
        argv += ["--model", model]
    argv += ["--permission-mode", "bypassPermissions"]
    return argv


class MimoEnvError(Exception):
    """mimo.env 读取失败（权限错误等）。CLI 层转 emit_error env(3)。

    与 ``FileNotFoundError``（→ dependency_missing exit 4）区分：缺文件是依赖未装，
    读不动文件（chmod 600 密钥权限错）是环境问题。
    """


def _mimo_env(cfg: Config) -> dict[str, str]:
    """读取并解析 mimo_env_file，叠加到当前进程 env 之上返回。

    - 文件缺失 → ``FileNotFoundError``（CLI 转 dependency_missing exit 4）
    - 读取受阻（PermissionError 等，chmod 600 密钥很常见）→ ``MimoEnvError``（CLI 转 env exit 3）
    """
    import os

    env_file = _resolve_mimo_env_file(cfg)
    if not env_file.is_file():
        raise FileNotFoundError(
            f"mimo.env 缺失：{env_file}（请创建或把 backend 切回 claude）"
        )
    try:
        text = env_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        # is_file() 与 read 之间被删；按缺失处理
        raise
    except (OSError, PermissionError) as e:
        raise MimoEnvError(
            f"mimo.env 读取失败：{env_file}：{e}（密钥文件通常 chmod 600，请检查权限）"
        ) from e
    parsed = parse_env_file(text)
    merged = dict(os.environ)
    merged.update(parsed)
    return merged


# ============================================================
# Prompt 渲染（复用 templates，与 agent.prompt_render 等价）
# ============================================================


def _render_prompt_file(
    p: _paths.Paths,
    seq: int,
    change_id: str,
    base: Path,
    phase: str,
    round_n: int | None,
    implement_commit: str | None,
) -> tuple[Path, str]:
    """渲染 prompt 文件到 disk 并返回 (prompt_file, prompt_text)。

    与 ``agent.prompt_render`` 走同一套 templates；implement 走
    ``render_implementer``，fix 走 ``render_fixer``（含 blocking findings）。
    """
    if phase == "implement":
        prompt_file = base / "implement.prompt.md"
        text = templates.render_implementer(
            change_id=change_id, base=str(base), repo_root=str(p.repo_root)
        )
    else:  # fix
        if round_n is None:
            raise ValueError("fix 阶段必须提供 round")
        prompt_file = base / f"round-{round_n}.fix.prompt.md"
        review_path = base / f"round-{round_n - 1}.review.json"
        findings_md = ""
        categories_seen: list[str] = []
        blocking_trend: list[int] = []
        state = read_state(p.state_json)
        entry = state.get("progress", [{}])[seq - 1] if state.get("progress") else {}
        categories_seen = entry.get("categories_seen") or []
        blocking_trend = entry.get("blocking_trend") or []
        if review_path.is_file():
            import json

            from .fixer import render_findings
            from .review import parse_review

            review_data = json.loads(review_path.read_text(encoding="utf-8"))
            parsed = parse_review(review_data)
            findings_md = render_findings(parsed["blocking_findings"])
        text = templates.render_fixer(
            change_id=change_id,
            round_n=round_n,
            implement_commit=implement_commit or "",
            base=str(base),
            repo_root=str(p.repo_root),
            blocking_findings_md=findings_md,
            categories_seen=categories_seen,
            blocking_trend=blocking_trend,
        )
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text(text, encoding="utf-8")
    spawn_text = templates.render_spawn_prompt(
        phase=phase,
        change_id=change_id,
        prompt_file=str(prompt_file.resolve()),
        extension=None,
    )
    return prompt_file, spawn_text


# ============================================================
# 核心编排
# ============================================================


_FAILED_IMPLEMENT_RESULT = (
    "RESULT: commit=- tasks=0 tests=fail summary=- "
    "notes=coder 未产出 RESULT 行（backend 可能异常）"
)


def _failed_fix_result(round_n: int) -> str:
    return (
        f"RESULT: commit=- fixed=0 tests=fail summary=- "
        f"categories_scanned=- regressions_added=- "
        f"notes=coder 未产出 RESULT 行 r{round_n}（backend 可能异常）"
    )


def _extract_result_line(stdout: str, *, fallback: str) -> str:
    """从 stdout 末尾抽 RESULT 行；无则返回 fallback（合成失败 RESULT）。"""
    for line in reversed(stdout.splitlines()):
        if line.strip().startswith("RESULT:"):
            return line.strip()
    return fallback


def _run_backend(
    cfg: Config,
    backend: str,
    *,
    spawn_text: str,
    repo_root: Path,
    backend_override_bin: str | None,
    runner: Runner,
    timeout: int | None,
) -> tuple[CoderRunResult, str | None]:
    """按 backend 跑 coder 子进程；返回 (CoderRunResult, model)。

    缺可执行文件抛 FileNotFoundError（调用方转 emit_error dependency_missing）。
    """
    if backend in ("claude", "mimo"):
        claude_bin = _find_bin("claude", backend_override_bin or cfg.coder.bin)
        if backend == "mimo":
            model = cfg.coder.model or DEFAULT_MIMO_MODEL
            env = _mimo_env(cfg)
        else:
            model = cfg.coder.model
            env = None
        argv = _build_claude_argv(claude_bin, spawn_text, model)
        result = runner(argv=argv, cwd=repo_root, env=env, timeout=timeout)
        return result, model
    if backend == "codex":
        # TODO: codex exec 路径（参考 pipeline._codex_exec / engines.CodexEngine）。
        # coder 经 codex 的 headless 编排尚未实现；当前明确报错而非静默退化。
        raise NotImplementedError(
            "coder backend=codex 尚未实现；请使用 claude / mimo，或参考 "
            "engines.CodexEngine 补齐 codex exec 路径"
        )
    raise ValueError(f"未知 coder backend：{backend!r}")


def run_implement(
    p: _paths.Paths,
    seq: int,
    change_id: str,
    *,
    backend: str | None = None,
    timeout: int | None = None,
    config_path: Path | None = None,
    runner: Runner = _default_runner,
) -> dict:
    """跑完整 implement coder：phase enter → 渲染 prompt → backend 子进程 → record。"""
    cfg = load_config(p.repo_root, override_path=config_path)
    selected = resolve_backend(cfg, "implement", backend)

    _pipeline._do_phase_enter(p, seq, "implement")

    # enter 之后必须保证配对 exit：从渲染到 backend 子进程整段兜底，
    # 任何异常都先把 phase 落 failed 再走错误返回（避免 phase 悬挂在 in-progress）。
    try:
        return _do_implement_body(
            p, seq, change_id, cfg, selected, runner=runner, timeout=timeout
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        return _fail_phase(
            p, seq, "implement", selected,
            reason="coder-timeout" if isinstance(e, subprocess.TimeoutExpired) else "coder-subprocess",
            error="coder-timeout" if isinstance(e, subprocess.TimeoutExpired) else "coder-subprocess-error",
            detail=str(e),
        )
    except (FileNotFoundError, NotImplementedError, ValueError, MimoEnvError):
        # 这些由 CLI 层映射成专用 exit code，需保持原异常类型；但 phase 不能悬挂。
        _pipeline._do_phase_exit(
            p, seq, "implement", status="failed",
            extra={"reason": "coder-setup-error"},
            progress_updates={"status": "failed", "reason": "coder-setup-error"},
        )
        raise


def _do_implement_body(
    p: _paths.Paths,
    seq: int,
    change_id: str,
    cfg: Config,
    selected: str,
    *,
    runner: Runner,
    timeout: int | None,
) -> dict:
    state = read_state(p.state_json)
    entry = state.get("progress", [{}])[seq - 1] if state.get("progress") else {}
    base = Path(entry.get("base") or _paths.base_for(p, seq, change_id))

    _, spawn_text = _render_prompt_file(
        p, seq, change_id, base, "implement", None, None
    )

    result, model = _run_backend(
        cfg,
        selected,
        spawn_text=spawn_text,
        repo_root=p.repo_root,
        backend_override_bin=None,
        runner=runner,
        timeout=timeout,
    )

    result_line = _extract_result_line(
        result.stdout, fallback=_FAILED_IMPLEMENT_RESULT
    )
    record = _pipeline.record_implement(p, seq, result_line)
    return {
        **record,
        "backend": selected,
        "model": model,
        "coder_exit": result.exit_code,
    }


def _fail_phase(
    p: _paths.Paths,
    seq: int,
    phase: str,
    selected: str,
    *,
    reason: str,
    error: str,
    detail: str,
    progress_status: str = "failed",
) -> dict:
    """phase 落 failed 并返回标准错误 dict（不裸抛，让 CLI emit JSON exit 1）。"""
    _pipeline._do_phase_exit(
        p, seq, phase, status="failed",
        extra={"reason": reason, "error": detail[:2000]},
        progress_updates={"status": progress_status, "reason": reason},
    )
    return {
        "ok": False,
        "seq": seq,
        "error": error,
        "reason": reason,
        "detail": detail[:2000],
        "backend": selected,
    }


def run_fix(
    p: _paths.Paths,
    seq: int,
    change_id: str,
    round_n: int,
    *,
    backend: str | None = None,
    timeout: int | None = None,
    config_path: Path | None = None,
    runner: Runner = _default_runner,
) -> dict:
    """跑完整 fix coder：phase enter → 渲染 prompt → backend 子进程 → record。"""
    cfg = load_config(p.repo_root, override_path=config_path)
    selected = resolve_backend(cfg, "fix", backend)

    phase = f"fix-r{round_n}"
    _pipeline._do_phase_enter(p, seq, phase)

    try:
        return _do_fix_body(
            p, seq, change_id, round_n, cfg, selected, runner=runner, timeout=timeout
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
        return _fail_phase(
            p, seq, phase, selected,
            reason="coder-timeout" if isinstance(e, subprocess.TimeoutExpired) else "coder-subprocess",
            error="coder-timeout" if isinstance(e, subprocess.TimeoutExpired) else "coder-subprocess-error",
            detail=str(e),
            progress_status="needs-user-decision",
        )
    except (FileNotFoundError, NotImplementedError, ValueError, MimoEnvError):
        _pipeline._do_phase_exit(
            p, seq, phase, status="failed",
            extra={"reason": "coder-setup-error"},
            progress_updates={"status": "needs-user-decision", "reason": "coder-setup-error"},
        )
        raise


def _do_fix_body(
    p: _paths.Paths,
    seq: int,
    change_id: str,
    round_n: int,
    cfg: Config,
    selected: str,
    *,
    runner: Runner,
    timeout: int | None,
) -> dict:
    state = read_state(p.state_json)
    entry = state.get("progress", [{}])[seq - 1] if state.get("progress") else {}
    implement_commit = entry.get("implement_commit")
    base = Path(entry.get("base") or _paths.base_for(p, seq, change_id))

    _, spawn_text = _render_prompt_file(
        p, seq, change_id, base, "fix", round_n, implement_commit
    )

    result, model = _run_backend(
        cfg,
        selected,
        spawn_text=spawn_text,
        repo_root=p.repo_root,
        backend_override_bin=None,
        runner=runner,
        timeout=timeout,
    )

    result_line = _extract_result_line(
        result.stdout, fallback=_failed_fix_result(round_n)
    )
    record = _pipeline.record_fix(p, seq, round_n, result_line)
    return {
        **record,
        "backend": selected,
        "model": model,
        "coder_exit": result.exit_code,
    }


# ============================================================
# CLI handlers
# ============================================================


def _resolve_change_id(p: _paths.Paths, seq: int, explicit: str | None) -> str:
    """从 state.progress[seq-1] 取 change_id；explicit 给定则校验一致。"""
    state = read_state(p.state_json)
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
    found = progress[seq - 1].get("change_id")
    if not found:
        raise ValueError(f"seq={seq} 的 progress 缺少 change_id")
    if explicit is not None and explicit != found:
        raise ValueError(
            f"--change-id={explicit} 与 state 中 seq={seq} 的 change_id={found} 不一致"
        )
    return found


def _emit_and_exit(result: dict) -> None:
    _io.emit(result)
    if not result.get("ok", False):
        import sys

        sys.exit(1)


def cli_implement_run(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        change_id = _resolve_change_id(p, args.seq, getattr(args, "change_id", None))
    except (FileNotFoundError, ValueError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = run_implement(
            p,
            args.seq,
            change_id,
            backend=getattr(args, "backend", None),
            timeout=getattr(args, "timeout", None),
            config_path=Path(args.config) if getattr(args, "config", None) else None,
        )
    except config.ConfigError as e:
        _io.emit_error("invalid_config", str(e), exit_code=2)
        return
    except MimoEnvError as e:
        _io.emit_error("env_error", str(e), exit_code=3)
        return
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return
    except NotImplementedError as e:
        _io.emit_error("not_implemented", str(e), exit_code=2)
        return
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)


def cli_fix_run(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    if getattr(args, "round_n", None) is None:
        _io.emit_error("missing_round", "fix run 必须提供 --round", exit_code=2)
        return
    try:
        change_id = _resolve_change_id(p, args.seq, getattr(args, "change_id", None))
    except (FileNotFoundError, ValueError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = run_fix(
            p,
            args.seq,
            change_id,
            args.round_n,
            backend=getattr(args, "backend", None),
            timeout=getattr(args, "timeout", None),
            config_path=Path(args.config) if getattr(args, "config", None) else None,
        )
    except config.ConfigError as e:
        _io.emit_error("invalid_config", str(e), exit_code=2)
        return
    except MimoEnvError as e:
        _io.emit_error("env_error", str(e), exit_code=3)
        return
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return
    except NotImplementedError as e:
        _io.emit_error("not_implemented", str(e), exit_code=2)
        return
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)
