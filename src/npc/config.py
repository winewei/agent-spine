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
SUPPORTED_DISPATCH_VALUES = ("headless", "in-session")

# 内置默认分发表：按 backend 决定 dispatch 默认值
DISPATCH_DEFAULTS: dict[str, str] = {
    "claude": "in-session",
    "mimo": "headless",
    "codex": "headless",
}


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
    # 复杂度门阈值（plan 前置软门，不阻断 run）
    # complexity_breadth_threshold：跨领域广度阈值（顶层模块数），默认 3
    complexity_breadth_threshold: int = 3
    # complexity_files_threshold：辅助文件数阈值，默认 10
    complexity_files_threshold: int = 10
    # max_rounds_large：large change 的 review-fix 循环上限（普通 change 由调用方自行设默认值）
    max_rounds_large: int = 30
    # adversarial_round0：round-0 是否在既有 compliance pass 之外追加 diff-only 对抗式 pass。
    # 默认 True（开启新行为）；显式关闭可回退到 round-0 单通道（成本敏感场景）。
    # round_n != 0 时该配置无效（round>=1 恒单通道）。
    adversarial_round0: bool = True

    def __post_init__(self) -> None:
        if self.engine not in SUPPORTED_ENGINES:
            raise ConfigError(
                f"未知 review engine：{self.engine!r}（仅支持 {'/'.join(SUPPORTED_ENGINES)}）"
            )
        if not isinstance(self.complexity_breadth_threshold, int) or self.complexity_breadth_threshold < 1:
            raise ConfigError(
                f"[review].complexity_breadth_threshold 必须是整数 ≥1，得到：{self.complexity_breadth_threshold!r}"
            )
        if not isinstance(self.complexity_files_threshold, int) or self.complexity_files_threshold < 1:
            raise ConfigError(
                f"[review].complexity_files_threshold 必须是整数 ≥1，得到：{self.complexity_files_threshold!r}"
            )
        if not isinstance(self.max_rounds_large, int) or self.max_rounds_large < 1:
            raise ConfigError(
                f"[review].max_rounds_large 必须是整数 ≥1，得到：{self.max_rounds_large!r}"
            )
        if not isinstance(self.adversarial_round0, bool):
            raise ConfigError(
                f"[review].adversarial_round0 必须是 bool，得到：{self.adversarial_round0!r}"
            )


@dataclass(frozen=True)
class CoderConfig:
    """coder（执行体）后端配置。成本路由：coder 默认 claude，可显式配置走廉价层（mimo）；review/决策恒留 premium。

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
    # dispatch 全局覆盖（headless | in-session）；None = 使用内置默认表。
    dispatch: str | None = None
    # per-phase dispatch 覆盖。((phase, dispatch), ...)，保持 frozen 可哈希。
    phase_dispatches: tuple[tuple[str, str], ...] = ()
    # 同一 category 连续出现（逐轮不中断）达此阈值时，fix prompt 该 category 段落
    # 升级为强制穷举落点清单（change fix-prompt-exhaustive-sweep）。默认 2，整数 ≥1。
    category_streak_threshold: int = 2

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

    def dispatch_for_phase(self, phase: str, backend: str, cli_override: str | None = None) -> str:
        """解析某 phase 的 dispatch 值。

        优先级：CLI override → per-phase → 全局 → 内置默认（按 backend）。
        """
        if cli_override:
            return cli_override
        for ph, dp in self.phase_dispatches:
            if ph == phase:
                return dp
        if self.dispatch is not None:
            return self.dispatch
        return DISPATCH_DEFAULTS.get(backend, "headless")

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
        if self.dispatch is not None and self.dispatch not in SUPPORTED_DISPATCH_VALUES:
            raise ConfigError(
                f"未知 coder dispatch：{self.dispatch!r}（仅支持 {'/'.join(SUPPORTED_DISPATCH_VALUES)}）"
            )
        if (
            not isinstance(self.category_streak_threshold, int)
            or isinstance(self.category_streak_threshold, bool)
            or self.category_streak_threshold < 1
        ):
            raise ConfigError(
                f"[coder].category_streak_threshold 必须是整数 ≥1，得到："
                f"{self.category_streak_threshold!r}"
            )
        for ph, dp in self.phase_dispatches:
            if dp not in SUPPORTED_DISPATCH_VALUES:
                raise ConfigError(
                    f"未知 coder phase dispatch：[coder.dispatch.phase].{ph}={dp!r}"
                    f"（仅支持 {'/'.join(SUPPORTED_DISPATCH_VALUES)}）"
                )


@dataclass(frozen=True)
class SpecWriterConfig:
    """spec 生成方后端配置。语义与 :class:`CoderConfig` 同构，但刻意精简：

    spec 生成的分发方式恒为 in-session（由 ``spine-spec-writer`` 定死，见
    change ``spec-routing-invariant`` 的 design.md D2），因此本配置 MUST NOT
    含 ``dispatch`` / ``phase`` 字段——配置面积不随执行路径的自由度膨胀。
    """

    backend: str | None = None  # None = 未显式配置 → 默认 claude（安全默认值）
    bin: str | None = None
    model: str | None = None

    @property
    def effective_backend(self) -> str:
        """未显式配置时的有效默认（claude）。供 check_routing 等只读消费者用。"""
        return self.backend or "claude"

    def __post_init__(self) -> None:
        if self.backend is not None and self.backend not in SUPPORTED_CODER_BACKENDS:
            raise ConfigError(
                f"未知 spec_writer backend：{self.backend!r}"
                f"（仅支持 {'/'.join(SUPPORTED_CODER_BACKENDS)}）"
            )


@dataclass(frozen=True)
class SpecReviewConfig:
    """spec 验证方引擎配置。语义与 :class:`ReviewEngineConfig` 同构。

    ``gate_cmd``：确定性 gate 命令，argv 数组（如
    ``["uv", "run", "scripts/check_spec.py"]``）；``None`` = 未配置，跳过该门。
    npc 会在其后追加 ``--change <id>`` 两个 argv 元素并以 ``shell=False`` 执行，
    只读其 stdout JSON 的 ``ok``/``rule_hits`` 两个键（见 change
    ``spine-spec-writer`` design.md D3b）。

    ``max_rounds``：spec fix 循环的**最多 fix 次数**（不是 review 轮数）。
    语义与 code review 的 ``rounds_since_strict_decrease`` stale 检测无关——
    spec fix 循环 MUST NOT 复用该判据（design.md D4）。默认 3。
    """

    engine: str = "codex"  # 安全默认值：与既有 review 默认一致
    claude_bin: str | None = None
    claude_model: str | None = None
    gate_cmd: tuple[str, ...] | None = None
    max_rounds: int = 3

    def __post_init__(self) -> None:
        if self.engine not in SUPPORTED_ENGINES:
            raise ConfigError(
                f"未知 spec_review engine：{self.engine!r}（仅支持 {'/'.join(SUPPORTED_ENGINES)}）"
            )
        if self.gate_cmd is not None:
            if not isinstance(self.gate_cmd, tuple) or not all(
                isinstance(x, str) for x in self.gate_cmd
            ):
                raise ConfigError(
                    f"[spec_review].gate_cmd 必须是字符串数组（argv），得到：{self.gate_cmd!r}"
                )
            if len(self.gate_cmd) == 0:
                raise ConfigError("[spec_review].gate_cmd 不得为空数组")
        if not isinstance(self.max_rounds, int) or self.max_rounds < 0:
            raise ConfigError(
                f"[spec_review].max_rounds 必须是整数 ≥0，得到：{self.max_rounds!r}"
            )


@dataclass(frozen=True)
class VerifyConfig:
    """质量门命令覆盖；任一省略则由 ``npc verify`` 按 repo 清单自动探测。

    ``rerun_tests``：record 阶段是否对 coder 自报 tests=pass 做真实复跑验证。
    - ``True``：开启（推荐用于 --auto 档）
    - ``False``：关闭，沿用旧行为（裸信 coder 自报）
    - ``None``（默认）：由调用方根据运行模式决定（interactive=False，auto=True）
    """

    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None
    build: str | None = None
    rerun_tests: bool | None = None


@dataclass(frozen=True)
class SchedulerConfig:
    """并行调度配置（[scheduler] 节）。

    ``max_parallel``：同层最大并发 change 数，默认 3，整数 ≥1。
    ``max_evictions``：同一 change 的最大 merge queue 驱逐次数，默认 2，整数 ≥1。
    """

    max_parallel: int = 3
    max_evictions: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.max_parallel, int) or self.max_parallel < 1:
            raise ConfigError(
                f"[scheduler].max_parallel 必须是整数 ≥1，得到：{self.max_parallel!r}"
            )
        if not isinstance(self.max_evictions, int) or self.max_evictions < 1:
            raise ConfigError(
                f"[scheduler].max_evictions 必须是整数 ≥1，得到：{self.max_evictions!r}"
            )


@dataclass(frozen=True)
class Config:
    """npc 顶层配置。"""

    review: ReviewEngineConfig = field(default_factory=ReviewEngineConfig)
    coder: CoderConfig = field(default_factory=CoderConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    spec_writer: SpecWriterConfig = field(default_factory=SpecWriterConfig)
    spec_review: SpecReviewConfig = field(default_factory=SpecReviewConfig)
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

    dispatch_val = coder_raw.get("dispatch")
    coder_dispatch = str(dispatch_val) if dispatch_val is not None else None
    dispatch_phase_raw = coder_raw.get("dispatch_phase") or {}
    if not isinstance(dispatch_phase_raw, dict):
        raise ConfigError(f"[coder].dispatch_phase 节必须是 table（{source}）")
    phase_dispatches_list: list[tuple[str, str]] = []
    for ph, dp in dispatch_phase_raw.items():
        if not isinstance(dp, str):
            raise ConfigError(f"[coder].dispatch_phase.{ph} 必须是字符串（{source}）")
        phase_dispatches_list.append((str(ph), dp))
    phase_dispatches = tuple(sorted(phase_dispatches_list))

    category_streak_threshold_raw = coder_raw.get("category_streak_threshold", 2)
    if not isinstance(category_streak_threshold_raw, int) or isinstance(
        category_streak_threshold_raw, bool
    ):
        raise ConfigError(f"[coder].category_streak_threshold 必须是整数（{source}）")

    verify_raw = data.get("verify") or {}
    if not isinstance(verify_raw, dict):
        raise ConfigError(f"[verify] 节必须是 table（{source}）")

    spec_writer_raw = data.get("spec_writer") or {}
    if not isinstance(spec_writer_raw, dict):
        raise ConfigError(f"[spec_writer] 节必须是 table（{source}）")
    spec_writer_backend_val = spec_writer_raw.get("backend")
    spec_writer_backend = (
        str(spec_writer_backend_val) if spec_writer_backend_val is not None else None
    )
    spec_writer_bin = _opt_str(spec_writer_raw.get("bin"), "spec_writer.bin", source)
    spec_writer_model = _opt_str(spec_writer_raw.get("model"), "spec_writer.model", source)

    spec_review_raw = data.get("spec_review") or {}
    if not isinstance(spec_review_raw, dict):
        raise ConfigError(f"[spec_review] 节必须是 table（{source}）")
    spec_review_engine = str(spec_review_raw.get("engine", "codex"))
    spec_review_claude_bin = _opt_str(
        spec_review_raw.get("claude_bin"), "spec_review.claude_bin", source
    )
    spec_review_claude_model = _opt_str(
        spec_review_raw.get("claude_model"), "spec_review.claude_model", source
    )
    gate_cmd_raw = spec_review_raw.get("gate_cmd")
    if gate_cmd_raw is None:
        spec_review_gate_cmd: tuple[str, ...] | None = None
    else:
        if not isinstance(gate_cmd_raw, list) or any(
            not isinstance(x, str) for x in gate_cmd_raw
        ):
            raise ConfigError(
                f"[spec_review].gate_cmd 必须是字符串数组（{source}）"
            )
        spec_review_gate_cmd = tuple(gate_cmd_raw)
    spec_review_max_rounds_raw = spec_review_raw.get("max_rounds", 3)
    if not isinstance(spec_review_max_rounds_raw, int):
        raise ConfigError(f"[spec_review].max_rounds 必须是整数（{source}）")

    scheduler_raw = data.get("scheduler") or {}
    if not isinstance(scheduler_raw, dict):
        raise ConfigError(f"[scheduler] 节必须是 table（{source}）")
    sched_mp_raw = scheduler_raw.get("max_parallel", 3)
    sched_me_raw = scheduler_raw.get("max_evictions", 2)
    if not isinstance(sched_mp_raw, int):
        raise ConfigError(f"[scheduler].max_parallel 必须是整数（{source}）")
    if not isinstance(sched_me_raw, int):
        raise ConfigError(f"[scheduler].max_evictions 必须是整数（{source}）")

    # 复杂度门阈值与 large 预算
    complexity_breadth_raw = review_raw.get("complexity_breadth_threshold", 3)
    complexity_files_raw = review_raw.get("complexity_files_threshold", 10)
    max_rounds_large_raw = review_raw.get("max_rounds_large", 30)
    if not isinstance(complexity_breadth_raw, int):
        raise ConfigError(f"[review].complexity_breadth_threshold 必须是整数（{source}）")
    if not isinstance(complexity_files_raw, int):
        raise ConfigError(f"[review].complexity_files_threshold 必须是整数（{source}）")
    if not isinstance(max_rounds_large_raw, int):
        raise ConfigError(f"[review].max_rounds_large 必须是整数（{source}）")
    adversarial_round0_raw = review_raw.get("adversarial_round0", True)
    if not isinstance(adversarial_round0_raw, bool):
        raise ConfigError(f"[review].adversarial_round0 必须是 bool（{source}）")

    return Config(
        review=ReviewEngineConfig(
            engine=engine,
            codex_bin=_opt_str(codex_raw.get("bin"), "review.codex.bin", source),
            claude_bin=_opt_str(claude_raw.get("bin"), "review.claude.bin", source),
            claude_model=_opt_str(claude_raw.get("model"), "review.claude.model", source),
            claude_extra_args=tuple(extra_args_raw),
            complexity_breadth_threshold=complexity_breadth_raw,
            complexity_files_threshold=complexity_files_raw,
            max_rounds_large=max_rounds_large_raw,
            adversarial_round0=adversarial_round0_raw,
        ),
        coder=CoderConfig(
            backend=backend,
            mimo_env_file=mimo_env,
            model=coder_model,
            bin=coder_bin,
            phase_backends=phase_backends,
            dispatch=coder_dispatch,
            phase_dispatches=phase_dispatches,
            category_streak_threshold=category_streak_threshold_raw,
        ),
        verify=VerifyConfig(
            test=_opt_str(verify_raw.get("test"), "verify.test", source),
            lint=_opt_str(verify_raw.get("lint"), "verify.lint", source),
            typecheck=_opt_str(verify_raw.get("typecheck"), "verify.typecheck", source),
            build=_opt_str(verify_raw.get("build"), "verify.build", source),
            rerun_tests=_opt_bool(verify_raw.get("rerun_tests"), "verify.rerun_tests", source),
        ),
        scheduler=SchedulerConfig(
            max_parallel=sched_mp_raw,
            max_evictions=sched_me_raw,
        ),
        spec_writer=SpecWriterConfig(
            backend=spec_writer_backend,
            bin=spec_writer_bin,
            model=spec_writer_model,
        ),
        spec_review=SpecReviewConfig(
            engine=spec_review_engine,
            claude_bin=spec_review_claude_bin,
            claude_model=spec_review_claude_model,
            gate_cmd=spec_review_gate_cmd,
            max_rounds=spec_review_max_rounds_raw,
        ),
        source=source,
    )


def _opt_str(val: object, name: str, source: str) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ConfigError(f"{name} 必须是字符串（{source}）")
    return val or None


def _opt_bool(val: object, name: str, source: str) -> bool | None:
    if val is None:
        return None
    if not isinstance(val, bool):
        raise ConfigError(f"{name} 必须是 bool（{source}）")
    return val
