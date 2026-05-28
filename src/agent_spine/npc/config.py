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
class Config:
    """npc 顶层配置。"""

    review: ReviewEngineConfig = field(default_factory=ReviewEngineConfig)
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

    return Config(
        review=ReviewEngineConfig(
            engine=engine,
            codex_bin=_opt_str(codex_raw.get("bin"), "review.codex.bin", source),
            claude_bin=_opt_str(claude_raw.get("bin"), "review.claude.bin", source),
            claude_model=_opt_str(claude_raw.get("model"), "review.claude.model", source),
            claude_extra_args=tuple(extra_args_raw),
        ),
        source=source,
    )


def _opt_str(val: object, name: str, source: str) -> str | None:
    if val is None:
        return None
    if not isinstance(val, str):
        raise ConfigError(f"{name} 必须是字符串（{source}）")
    return val or None
