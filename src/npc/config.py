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

    [experience]               # OpenViking 经验层（可省略；默认全关）
    enabled = true
    env_file = "~/.openviking/npc-client.env"   # 凭据指针，不入 git
    write_gate = "verified"    # verified | any
    extraction_model_declared = "gpt-5.4"       # 供 doctor 比对成本分层

    [host]                     # 宿主 CLI（可省略；默认 env 探测：CLAUDECODE → claude，否则 generic）
    name = "generic"           # claude | generic | 任意自定义名
    session_dir = ".kimi/sessions/{proj_key}"  # 可选：为非 Claude 宿主补 session 目录模板

内置 provider（无需声明即可用，可被 ``[providers.*]`` 同名覆盖）：

- ``claude``：claude-cli，无 env_file（订阅 / 当前 provider）
- ``mimo``：claude-cli + ``~/.config/npc/mimo.env``，model=mimo-v2.5-pro
- ``codex``：codex-cli
"""

from __future__ import annotations

import string
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
class HostConfig:
    """宿主（调用 npc 的 agent CLI）配置。

    - ``name``：``claude`` / ``generic`` / 任意自定义名；None = 自动探测
      （``CLAUDECODE`` env → claude，否则 generic）。
    - ``session_dir``：相对 home 的 session 目录模板（``{proj_key}`` 占位），
      为非 Claude 宿主补 mtime 启发识别能力；None = 用宿主内置默认。
    """

    name: str | None = None
    session_dir: str | None = None


@dataclass(frozen=True)
class VerifyConfig:
    """质量门命令覆盖；任一省略则由 ``npc verify`` 按 repo 清单自动探测。"""

    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None
    build: str | None = None
    # strict：测试命令必须 exit 0；diff：整合后失败集合 ⊆ 整合前基线失败集合即通过
    # （适用于存在既有污染失败、以"零新增失败"判零回归的仓库）。
    test_baseline: str = "strict"


SUPPORTED_TEST_BASELINE_MODES: tuple[str, ...] = ("strict", "diff")

# 经验层写入闸门：verified = 只提交独立 review 通过且未被污染的轨迹；any = 只要 archived
SUPPORTED_WRITE_GATES: tuple[str, ...] = ("verified", "any")

DEFAULT_EXPERIENCE_ENV_FILE = "~/.openviking/npc-client.env"
DEFAULT_EXPERIENCE_ROOT_ENV_FILE = "~/.openviking/root.env"


@dataclass(frozen=True)
class ExperienceConfig:
    """OpenViking 经验层配置（旁路软失败增强，默认关闭）。

    凭据走 ``env_file`` + ``api_key_env`` 指针（对齐 :class:`ProviderConfig`），
    绝不入 git。``base_url`` 省略时依次回退 env 文件的 ``OPENVIKING_BASE_URL``
    与内置默认 ``http://127.0.0.1:1933``——解析在 :mod:`npc.experience` 完成，
    本 dataclass 只承载声明。
    """

    enabled: bool = False
    env_file: str = DEFAULT_EXPERIENCE_ENV_FILE
    base_url: str | None = None
    api_key_env: str = "OPENVIKING_API_KEY"
    root_env_file: str = DEFAULT_EXPERIENCE_ROOT_ENV_FILE
    timeout_recall_ms: int = 3000
    timeout_commit_ms: int = 5000
    inject_max_tokens_implement: int = 800
    inject_max_tokens_fix: int = 600
    score_threshold: float = 0.35
    quota_experiences: int = 3
    session_prefix: str = "npc"
    write_gate: str = "verified"
    # 不变量 4：抽取属"分析"，模型档位不得低于 coder，由人工声明、doctor 比对告警
    extraction_model_declared: str | None = None

    def __post_init__(self) -> None:
        if self.write_gate not in SUPPORTED_WRITE_GATES:
            raise ConfigError(
                f"experience.write_gate 不支持：{self.write_gate!r}"
                f"（合法值 = {'/'.join(SUPPORTED_WRITE_GATES)}）"
            )

    def inject_max_tokens(self, phase: str) -> int:
        """按 phase 取注入 token 预算；未知 phase 走 fix 档（更保守）。"""
        return (
            self.inject_max_tokens_implement
            if phase == "implement"
            else self.inject_max_tokens_fix
        )


@dataclass(frozen=True)
class Config:
    """npc 顶层配置。"""

    review: ReviewEngineConfig = field(default_factory=ReviewEngineConfig)
    coder: CoderConfig = field(default_factory=CoderConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    host: HostConfig = field(default_factory=HostConfig)
    experience: ExperienceConfig = field(default_factory=ExperienceConfig)
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
    test_baseline = _opt_str(verify_raw.get("test_baseline"), "verify.test_baseline", source) or "strict"
    if test_baseline not in SUPPORTED_TEST_BASELINE_MODES:
        raise ConfigError(
            f"verify.test_baseline 不支持：{test_baseline!r}"
            f"（合法值 = {'/'.join(SUPPORTED_TEST_BASELINE_MODES)}；{source}）"
        )

    host_raw = data.get("host") or {}
    if not isinstance(host_raw, dict):
        raise ConfigError(f"[host] 节必须是 table（{source}）")

    experience = _build_experience(data.get("experience"), source)

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
            test_baseline=test_baseline,
        ),
        host=HostConfig(
            name=_opt_str(host_raw.get("name"), "host.name", source),
            session_dir=_session_dir_template(host_raw.get("session_dir"), source),
        ),
        experience=experience,
        source=source,
    )


def _build_experience(raw: object, source: str) -> ExperienceConfig:
    """解析 ``[experience]`` 段；缺省时返回全默认（enabled=False）。"""
    if raw is None:
        return ExperienceConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"[experience] 节必须是 table（{source}）")

    defaults = ExperienceConfig()

    def _s(key: str, default: str) -> str:
        return _opt_str(raw.get(key), f"experience.{key}", source) or default

    return ExperienceConfig(
        enabled=_opt_bool(raw.get("enabled"), "experience.enabled", source, defaults.enabled),
        env_file=_s("env_file", defaults.env_file),
        base_url=_opt_str(raw.get("base_url"), "experience.base_url", source),
        api_key_env=_s("api_key_env", defaults.api_key_env),
        root_env_file=_s("root_env_file", defaults.root_env_file),
        timeout_recall_ms=_opt_int(
            raw.get("timeout_recall_ms"),
            "experience.timeout_recall_ms",
            source,
            defaults.timeout_recall_ms,
        ),
        timeout_commit_ms=_opt_int(
            raw.get("timeout_commit_ms"),
            "experience.timeout_commit_ms",
            source,
            defaults.timeout_commit_ms,
        ),
        inject_max_tokens_implement=_opt_int(
            raw.get("inject_max_tokens_implement"),
            "experience.inject_max_tokens_implement",
            source,
            defaults.inject_max_tokens_implement,
        ),
        inject_max_tokens_fix=_opt_int(
            raw.get("inject_max_tokens_fix"),
            "experience.inject_max_tokens_fix",
            source,
            defaults.inject_max_tokens_fix,
        ),
        score_threshold=_opt_float(
            raw.get("score_threshold"),
            "experience.score_threshold",
            source,
            defaults.score_threshold,
        ),
        quota_experiences=_opt_int(
            raw.get("quota_experiences"),
            "experience.quota_experiences",
            source,
            defaults.quota_experiences,
        ),
        session_prefix=_s("session_prefix", defaults.session_prefix),
        write_gate=_s("write_gate", defaults.write_gate),
        extraction_model_declared=_opt_str(
            raw.get("extraction_model_declared"),
            "experience.extraction_model_declared",
            source,
        ),
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


def _session_dir_template(val: object, source: str) -> str | None:
    """校验 ``host.session_dir`` 模板：仅允许 ``{proj_key}`` 占位符。

    在加载期拦截语法错误 / 未知占位符，避免 init / session 识别路径上的
    ``str.format`` 抛 KeyError（届时 run.json 可能已写盘，只能以 traceback 收场）。
    """
    tpl = _opt_str(val, "host.session_dir", source)
    if tpl is None:
        return None
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(tpl) if f is not None]
    except ValueError as e:
        raise ConfigError(f"host.session_dir 模板语法错误：{e}（{source}）")
    bad = [f for f in fields if f != "proj_key"]
    if bad:
        raise ConfigError(
            f"host.session_dir 含不支持的占位符 {{{bad[0]}}}"
            f"（仅支持 {{proj_key}}；{source}）"
        )
    return tpl


def _opt_str(val: object, name: str, source: str) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ConfigError(f"{name} 必须是字符串（{source}）")
    return val or None


def _opt_bool(val: object, name: str, source: str, default: bool) -> bool:
    if val is None:
        return default
    if not isinstance(val, bool):
        raise ConfigError(f"{name} 必须是布尔值（{source}）")
    return val


def _opt_int(val: object, name: str, source: str, default: int) -> int:
    if val is None:
        return default
    # bool 是 int 的子类，显式排除以免 true 被当成 1
    if isinstance(val, bool) or not isinstance(val, int):
        raise ConfigError(f"{name} 必须是整数（{source}）")
    if val <= 0:
        raise ConfigError(f"{name} 必须为正整数，实得 {val}（{source}）")
    return val


def _opt_float(val: object, name: str, source: str, default: float) -> float:
    if val is None:
        return default
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ConfigError(f"{name} 必须是数值（{source}）")
    return float(val)
