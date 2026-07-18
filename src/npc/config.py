"""npc 配置加载。

支持通过 TOML 配置文件指定 review 引擎与 coder provider，按以下优先级分层合并：

1. ``override_path``（CLI ``--config`` 显式传入；**只读该文件，不参与合并**）
2. ``<repo_root>/.npc/config.toml``（项目级配置；可入 git，只放路由不放凭据）
3. ``~/.config/npc/config.toml``（用户全局配置；provider 定义与凭据指针的家）
4. ``<HOME>/task_log/config.toml``（兼容 task_log 目录布局）

合并语义（v1.6 起）：2-4 层按「低优先级打底、高优先级覆盖」深合并——table 递归
合并、标量/数组整体覆盖。典型用法：全局定义 ``[providers.*]``（模型、env_file
凭据指针），项目 ``.npc/config.toml`` 只写 ``[coder] backend = "kimi"`` 选用。
全部缺失时返回内置默认（engine=codex，coder 默认 claude）。

TOML 示例：

    # ~/.config/npc/config.toml —— 全局：定义 provider（凭据经 env_file 注入）
    [providers.kimi]
    runner = "claude-cli"                  # claude-cli | codex-cli
    env_file = "~/.config/npc/kimi.env"    # ANTHROPIC_BASE_URL / AUTH_TOKEN
    model = "kimi-k3"

    [providers.deepseek]
    runner = "claude-cli"
    env_file = "~/.config/npc/deepseek.env"
    model = "deepseek-chat"

    # <repo>/.npc/config.toml —— 项目：只做路由
    [coder]
    backend = "kimi"
    [coder.phase]
    fix = "deepseek"

    [review]
    engine = "claude"          # codex | claude（review 恒留 premium，不接受 provider 名）

    [review.codex]
    bin = "/Users/foo/bin/codex"

    [review.claude]
    bin = "claude"             # 可省略；默认 PATH 查找
    model = "claude-opus-4-7"  # 可省略；省略则使用 claude 的默认 model
    extra_args = ["--permission-mode", "default"]

内置 provider（无需声明即可用，可被 ``[providers.*]`` 同名覆盖）：

- ``claude``：claude-cli，无 env_file（订阅 / 当前 provider）
- ``mimo``：claude-cli + ``~/.config/npc/mimo.env``，model=mimo-v2.5-pro
- ``codex``：codex-cli
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_FILENAME = "config.toml"
SUPPORTED_ENGINES = ("codex", "claude")
# 内置 provider 名（向后兼容旧 backend 枚举）；完整合法值 = 内置 + [providers.*] 自定义
SUPPORTED_CODER_BACKENDS = ("claude", "mimo", "codex")
SUPPORTED_RUNNERS = ("claude-cli", "codex-cli")

DEFAULT_MIMO_ENV_FILE = "~/.config/npc/mimo.env"
DEFAULT_MIMO_MODEL = "mimo-v2.5-pro"


class ConfigError(Exception):
    """配置加载或校验失败。"""


@dataclass(frozen=True)
class ProviderConfig:
    """一个可路由的模型 provider。

    - ``runner="claude-cli"``：经 ``claude -p`` 跑；``env_file`` 存在时 source 后注入
      子进程 env（Anthropic 兼容端点：``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_AUTH_TOKEN``），
      kimi / qwen / deepseek / MiMo 均走此通道。
    - ``runner="codex-cli"``：经 ``codex exec`` 跑（如 gpt codex）。
    """

    name: str
    runner: str = "claude-cli"
    env_file: str | None = None  # 凭据指针；只应出现在全局配置，勿入项目 git
    model: str | None = None
    bin: str | None = None

    def __post_init__(self) -> None:
        if self.runner not in SUPPORTED_RUNNERS:
            raise ConfigError(
                f"未知 provider runner：{self.runner!r}"
                f"（仅支持 {'/'.join(SUPPORTED_RUNNERS)}）"
            )


BUILTIN_PROVIDERS: tuple[ProviderConfig, ...] = (
    ProviderConfig(name="claude", runner="claude-cli"),
    ProviderConfig(
        name="mimo",
        runner="claude-cli",
        env_file=DEFAULT_MIMO_ENV_FILE,
        model=DEFAULT_MIMO_MODEL,
    ),
    ProviderConfig(name="codex", runner="codex-cli"),
)


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

    # backend 合法性不再由本 dataclass 校验：合法值 = 内置 + [providers.*] 自定义，
    # 需要 provider 注册表才能判定，统一在 _build（load 时）与 verify.check_routing 里做。


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
    providers: tuple[ProviderConfig, ...] = BUILTIN_PROVIDERS
    source: str = "<default>"

    def provider(self, name: str) -> ProviderConfig | None:
        """按名取 provider；未注册返回 None。"""
        for p in self.providers:
            if p.name == name:
                return p
        return None


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
    """分层加载配置；全部缺失时返回默认。

    - ``override_path``：只读该文件，不与其它层合并（显式即全量，保持可预测）。
    - 其余候选层按「低优先级打底、高优先级覆盖」深合并（见 :func:`_deep_merge`），
      使「全局定义 [providers.*]、项目只写路由」成为可能。

    解析失败（非法 TOML、未知 engine/provider 引用）抛 :class:`ConfigError`，
    调用方负责转 emit_error。
    """
    if override_path is not None:
        if not override_path.is_file():
            raise ConfigError(f"显式 --config 文件不存在：{override_path}")
        return _build(_read_toml(override_path), source=str(override_path))

    merged: dict = {}
    sources: list[str] = []  # 低优先级在前
    for path in reversed(candidate_config_paths(repo_root, home)):
        if path.is_file():
            _deep_merge(merged, _read_toml(path))
            sources.append(str(path))
    if not sources:
        return Config()
    # source 高优先级在前，便于人读「谁覆盖谁」
    return _build(merged, source=" <- ".join(reversed(sources)))


def _read_toml(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"读取配置失败：{path}：{e}") from e
    try:
        return tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"配置 TOML 解析失败：{path}：{e}") from e


def _deep_merge(base: dict, overlay: dict) -> None:
    """把 overlay 深合并进 base（原地）：table 递归合并，标量/数组整体覆盖。"""
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val


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

    providers = _build_providers(data.get("providers"), source, mimo_env=mimo_env)
    provider_names = {p.name for p in providers}
    if backend is not None and backend not in provider_names:
        raise ConfigError(
            f"未知 coder backend：{backend!r}"
            f"（合法值 = 内置 {'/'.join(SUPPORTED_CODER_BACKENDS)} 或 [providers.*] 自定义；{source}）"
        )
    for ph, be in phase_backends:
        if be not in provider_names:
            raise ConfigError(
                f"未知 coder phase 后端：[coder.phase].{ph}={be!r}"
                f"（合法值 = 内置 {'/'.join(SUPPORTED_CODER_BACKENDS)} 或 [providers.*] 自定义；{source}）"
            )

    verify_raw = data.get("verify") or {}
    if not isinstance(verify_raw, dict):
        raise ConfigError(f"[verify] 节必须是 table（{source}）")

    return Config(
        providers=providers,
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


def _build_providers(
    raw: object, source: str, *, mimo_env: str | None = None
) -> tuple[ProviderConfig, ...]:
    """内置 provider + ``[providers.*]`` 自定义（同名覆盖内置）。

    ``mimo_env``：旧字段 ``[coder.mimo].env_file`` 的兼容通道——存在则覆盖内置
    mimo provider 的 env_file，让 doctor / verify 等只读 providers 的消费者与
    coder 实际行为一致。
    """
    by_name: dict[str, ProviderConfig] = {p.name: p for p in BUILTIN_PROVIDERS}
    if mimo_env:
        base = by_name["mimo"]
        by_name["mimo"] = ProviderConfig(
            name="mimo", runner=base.runner, env_file=mimo_env, model=base.model
        )
    if raw is None:
        return tuple(by_name.values())
    if not isinstance(raw, dict):
        raise ConfigError(f"[providers] 节必须是 table（{source}）")
    for name, sub in raw.items():
        if not isinstance(sub, dict):
            raise ConfigError(f"[providers.{name}] 节必须是 table（{source}）")
        runner_val = sub.get("runner", "claude-cli")
        if not isinstance(runner_val, str):
            raise ConfigError(f"[providers.{name}].runner 必须是字符串（{source}）")
        try:
            by_name[str(name)] = ProviderConfig(
                name=str(name),
                runner=runner_val,
                env_file=_opt_str(sub.get("env_file"), f"providers.{name}.env_file", source),
                model=_opt_str(sub.get("model"), f"providers.{name}.model", source),
                bin=_opt_str(sub.get("bin"), f"providers.{name}.bin", source),
            )
        except ConfigError as e:
            raise ConfigError(f"[providers.{name}]：{e}（{source}）") from e
    return tuple(by_name.values())


def _opt_str(val: object, name: str, source: str) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ConfigError(f"{name} 必须是字符串（{source}）")
    return val or None
