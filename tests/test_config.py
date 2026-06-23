"""config.py 测试：候选路径优先级、TOML 解析、错误处理。"""

from __future__ import annotations

from pathlib import Path

import pytest

from npc import config as _config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_load_config_defaults_when_no_file(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    cfg = _config.load_config(repo, home=home)
    assert cfg.review.engine == "codex"
    assert cfg.review.codex_bin is None
    assert cfg.review.claude_bin is None
    assert cfg.review.claude_model is None
    assert cfg.review.claude_extra_args == ()
    assert cfg.source == "<default>"


def test_load_config_project_local_wins(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(
        repo / ".npc" / "config.toml",
        '[review]\nengine = "claude"\n[review.claude]\nbin = "/proj/claude"\n',
    )
    _write(
        home / ".config" / "npc" / "config.toml",
        '[review]\nengine = "codex"\n',
    )
    cfg = _config.load_config(repo, home=home)
    assert cfg.review.engine == "claude"
    assert cfg.review.claude_bin == "/proj/claude"
    assert cfg.source.endswith("repo/.npc/config.toml")


def test_load_config_user_global_fallback(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(
        home / ".config" / "npc" / "config.toml",
        '[review]\nengine = "claude"\n[review.claude]\nmodel = "claude-opus-4-7"\n'
        'extra_args = ["--permission-mode", "default"]\n',
    )
    cfg = _config.load_config(repo, home=home)
    assert cfg.review.engine == "claude"
    assert cfg.review.claude_model == "claude-opus-4-7"
    assert cfg.review.claude_extra_args == ("--permission-mode", "default")


def test_load_config_task_log_fallback(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(
        home / "task_log" / "config.toml",
        '[review]\nengine = "codex"\n[review.codex]\nbin = "/opt/codex"\n',
    )
    cfg = _config.load_config(repo, home=home)
    assert cfg.review.codex_bin == "/opt/codex"


def test_load_config_override_path(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    explicit = tmp_path / "other.toml"
    _write(explicit, '[review]\nengine = "claude"\n')
    cfg = _config.load_config(repo, override_path=explicit)
    assert cfg.review.engine == "claude"
    assert cfg.source == str(explicit)


def test_load_config_override_missing_raises(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(_config.ConfigError, match="不存在"):
        _config.load_config(repo, override_path=tmp_path / "nope.toml")


def test_load_config_invalid_engine_rejected(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(repo / ".npc" / "config.toml", '[review]\nengine = "gemini"\n')
    with pytest.raises(_config.ConfigError, match="未知 review engine"):
        _config.load_config(repo, home=home)


def test_load_config_invalid_extra_args_type(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(
        repo / ".npc" / "config.toml",
        '[review.claude]\nextra_args = [1, 2]\n',
    )
    with pytest.raises(_config.ConfigError, match="extra_args"):
        _config.load_config(repo, home=home)


def test_load_config_invalid_toml_syntax(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(repo / ".npc" / "config.toml", "[review\nengine = ?\n")
    with pytest.raises(_config.ConfigError, match="TOML 解析失败"):
        _config.load_config(repo, home=home)


# ============================================================
# [coder] backend + per-phase 路由（MiMo 默认关，按需开）
# ============================================================


def _write_cfg(tmp_path, body: str):
    import pathlib
    d = pathlib.Path(tmp_path) / ".npc"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(body, encoding="utf-8")
    return pathlib.Path(tmp_path)


def test_coder_backend_default_none(tmp_path):
    from npc.config import load_config
    cfg = load_config(tmp_path)  # 无配置文件
    assert cfg.coder.backend is None
    assert cfg.coder.effective_backend == "claude"
    assert cfg.coder.phase_backends == ()


def test_coder_phase_backends_parsed(tmp_path):
    from npc.config import load_config
    repo = _write_cfg(tmp_path, '[coder]\nbackend="claude"\n[coder.phase]\nfix="mimo"\n')
    cfg = load_config(repo)
    assert cfg.coder.backend == "claude"
    assert cfg.coder.backend_for_phase("fix") == "mimo"
    assert cfg.coder.backend_for_phase("implement") == "claude"


def test_coder_phase_unknown_backend_rejected(tmp_path):
    from npc.config import load_config, ConfigError
    repo = _write_cfg(tmp_path, '[coder.phase]\nfix="gpt5"\n')
    with pytest.raises(ConfigError):
        load_config(repo)


def test_coder_unknown_backend_rejected(tmp_path):
    from npc.config import load_config, ConfigError
    repo = _write_cfg(tmp_path, '[coder]\nbackend="gpt5"\n')
    with pytest.raises(ConfigError):
        load_config(repo)
