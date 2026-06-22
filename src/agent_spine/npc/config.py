"""npc 配置加载。

支持通过 TOML 配置文件指定 review 引擎（codex 或 claude），按以下优先级查找：

1. ``override_path``（CLI ``--config`` 显式传入）
2. ``<repo_root>/.npc/config.toml``（项目级配置；可入 git）
3. ``~/.config/npc/config.toml``（用户全局配置）
4. ``<HOME>/task_log/config.toml``（兼容 task_log 目录布局）

任何一级文件不存在则继续向下查找；全部缺失时返回内置默认（engine=codex，
其它字段为 ``None``，使用 PATH 中的 codex / claude）。

TOML 示例：

    [review]
    engine = "claude"          # codex | claude

    [review.codex]
    bin = "/Users/foo/bin/codex"

    [review.claude]
    bin = "claude"             # 可省略；默认 PATH 查找
    model = "claude-opus-4-7"  # 可省略；省略则使用 claude 的默认 model
    extra_args = ["--permission-mode", "default"]
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_FILENAME = "config.toml"
SUPPORTED_ENGINES = ("codex", "claude")
SUPPORTED_CODER_BACKENDS = ("claude", "mimo", "codex")


class ConfigError(Exception):
    """配置加载或校验失败。"""


@dataclass(frozen=True)
class ReviewEngineConfig:
    """review 引擎相关配置。"""

    engine: str = "codex"
    codex_bin: str | None = None
    claude_bin: str | None = None
    claude_model: str | None = None
    claude_extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.engine not in SUPPORTED_ENGINES:
            raise ConfigError(
                f"未知 review engine：{self.engine!r}（仅支持 {'/'.join(SUPPORTED_ENGINES)}）"
            )


@dataclass(frozen=True)
class CoderConfig:
    """coder（执行体）后端配置。成本路由：coder 默认走廉价层（mimo），但 review/决策恒留 premium。

    backend 取值见 :data:`SUPPORTED_CODER_BACKENDS`：
    - ``claude``：headless ``claude -p``（用 Claude 订阅 / 当前 session provider）
    - ``mimo``：headless ``claude -p`` + source ``mimo_env_file`` 路由到 MiMo（Anthropic 兼容）
    - ``codex``：``codex exec``
    """

    backend: str | None = None  # None = 未显式配置 → 默认 claude（不自动启用 mimo）
    mimo_env_file: str | None = None  # 省略则默认 ~/.config/npc/mimo.env
    model: str | None = None  # 如 mimo-v2.5-pro；省略走 backend 默认
    bin: str | None = None  # claude/codex 可执行文件覆盖
    # per-phase 后端覆盖（如只把 fix 给 mimo）。((phase, backend), ...)，保持 frozen 可哈希。
    phase_backends: tuple[tuple[str, str], ...] = ()

    @property
    def effective_backend(self) -> str:
        """未显式配置时的有效默认（claude）。供 check_routing 等只读消费者用。"""
        return self.backend or "claude"

    def backend_for_phase(self, phase: str) -> str | None:
        """该 phase 的显式后端（phase 覆盖 > 全局 backend）；都未设返回 None。"""
        for ph, be in self.phase_backends:
            if ph == phase:
                return be
        return self.backend

    def __post_init__(self) -> None:
        if self.backend is not None and self.backend not in SUPPORTED_CODER_BACKENDS:
            raise ConfigError(
                f"未知 coder backend：{self.backend!r}（仅支持 {'/'.join(SUPPORTED_CODER_BACKENDS)}）"
            )
        for ph, be in self.phase_backends:
            if be not in SUPPORTED_CODER_BACKENDS:
                raise ConfigError(
                    f"未知 coder phase 后端：[coder.phase].{ph}={be!r}"
                    f"（仅支持 {'/'.join(SUPPORTED_CODER_BACKENDS)}）"
                )


@dataclass(frozen=True)
class VerifyConfig:
    """质量门命令覆盖；任一省略则由 ``npc verify`` 按 repo 清单自动探测。"""

    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None
    build: str | None = None


@dataclass(frozen=True)
class Config:
    """npc 顶层配置。"""

    review: ReviewEngineConfig = field(default_factory=ReviewEngineConfig)
    coder: CoderConfig = field(default_factory=CoderConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    source: str = "<default>"


def candidate_config_paths(repo_root: Path, home: Path | None = None) -> list[Path]:
    """按优先级返回配置文件候选路径（不做存在性检查）。"""
    h = home or Path.home()
    return [
        repo_root / ".npc" / CONFIG_FILENAME,
        h / ".config" / "npc" / CONFIG_FILENAME,
        h / "task_log" / CONFIG_FILENAME,
    ]


def load_config(
    repo_root: Path,
    *,
    home: Path | None = None,
    override_path: Path | None = None,
) -> Config:
    """按优先级加载配置；全部缺失时返回默认。

    解析失败（非法 TOML、未知 engine）抛 :class:`ConfigError`，调用方负责转 emit_error。
    """
    if override_path is not None:
        if not override_path.is_file():
            raise ConfigError(f"显式 --config 文件不存在：{override_path}")
        return _parse(override_path)

    for path in candidate_config_paths(repo_root, home):
        if path.is_file():
            return _parse(path)
    return Config()


def _parse(path: Path) -> Config:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"读取配置失败：{path}：{e}") from e
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置 TOML 解析失败：{path}：{e}") from e
    return _build(data, source=str(path))


def _build(data: dict, source: str) -> Config:
    review_raw = data.get("review") or {}
    if not isinstance(review_raw, dict):
        raise ConfigError(f"[review] 节必须是 table（{source}）")

    engine = str(review_raw.get("engine", "codex"))

    codex_raw = review_raw.get("codex") or {}
    if not isinstance(codex_raw, dict):
        raise ConfigError(f"[review.codex] 节必须是 table（{source}）")

    claude_raw = review_raw.get("claude") or {}
    if not isinstance(claude_raw, dict):
        raise ConfigError(f"[review.claude] 节必须是 table（{source}）")

    extra_args_raw = claude_raw.get("extra_args") or []
    if not isinstance(extra_args_raw, list) or any(
        not isinstance(x, str) for x in extra_args_raw
    ):
        raise ConfigError(
            f"[review.claude].extra_args 必须是字符串数组（{source}）"
        )

    coder_raw = data.get("coder") or {}
    if not isinstance(coder_raw, dict):
        raise ConfigError(f"[coder] 节必须是 table（{source}）")
    coder_mimo = coder_raw.get("mimo") or {}
    coder_claude = coder_raw.get("claude") or {}
    coder_codex = coder_raw.get("codex") or {}
    for name, sub in (("mimo", coder_mimo), ("claude", coder_claude), ("codex", coder_codex)):
        if not isinstance(sub, dict):
            raise ConfigError(f"[coder.{name}] 节必须是 table（{source}）")
    backend_val = coder_raw.get("backend")
    backend = str(backend_val) if backend_val is not None else None
    # bin/model：按有效 backend 从对应子表取，回退顶层
    backend_sub = {"mimo": coder_mimo, "claude": coder_claude, "codex": coder_codex}.get(
        backend or "claude", {}
    )
    coder_bin = _opt_str(
        backend_sub.get("bin", coder_raw.get("bin")), f"coder.{backend}.bin", source
    )
    coder_model = _opt_str(
        backend_sub.get("model", coder_raw.get("model")), f"coder.{backend}.model", source
    )
    mimo_env = _opt_str(coder_mimo.get("env_file"), "coder.mimo.env_file", source)

    phase_raw = coder_raw.get("phase") or {}
    if not isinstance(phase_raw, dict):
        raise ConfigError(f"[coder.phase] 节必须是 table（{source}）")
    phase_backends_list: list[tuple[str, str]] = []
    for ph, be in phase_raw.items():
        if not isinstance(be, str):
            raise ConfigError(f"[coder.phase].{ph} 必须是字符串（{source}）")
        phase_backends_list.append((str(ph), be))
    phase_backends = tuple(sorted(phase_backends_list))

    verify_raw = data.get("verify") or {}
    if not isinstance(verify_raw, dict):
        raise ConfigError(f"[verify] 节必须是 table（{source}）")

    return Config(
        review=ReviewEngineConfig(
            engine=engine,
            codex_bin=_opt_str(codex_raw.get("bin"), "review.codex.bin", source),
            claude_bin=_opt_str(claude_raw.get("bin"), "review.claude.bin", source),
            claude_model=_opt_str(claude_raw.get("model"), "review.claude.model", source),
            claude_extra_args=tuple(extra_args_raw),
        ),
        coder=CoderConfig(
            backend=backend,
            mimo_env_file=mimo_env,
            model=coder_model,
            bin=coder_bin,
            phase_backends=phase_backends,
        ),
        verify=VerifyConfig(
            test=_opt_str(verify_raw.get("test"), "verify.test", source),
            lint=_opt_str(verify_raw.get("lint"), "verify.lint", source),
            typecheck=_opt_str(verify_raw.get("typecheck"), "verify.typecheck", source),
            build=_opt_str(verify_raw.get("build"), "verify.build", source),
        ),
        source=source,
    )


def _opt_str(val: object, name: str, source: str) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ConfigError(f"{name} 必须是字符串（{source}）")
    return val or None
