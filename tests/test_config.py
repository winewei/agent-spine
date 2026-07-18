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
    # 分层合并后 source 列出全部命中层，高优先级在前
    assert cfg.source.startswith(str(repo / ".npc" / "config.toml"))
    assert str(home / ".config" / "npc" / "config.toml") in cfg.source


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


# ============================================================
# [providers.*]：自定义 provider 注册表 + 跨层合并
# ============================================================


def test_builtin_providers_present_by_default(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    cfg = _config.load_config(repo, home=home)
    assert {p.name for p in cfg.providers} == {"claude", "mimo", "codex"}
    mimo = cfg.provider("mimo")
    assert mimo.runner == "claude-cli"
    assert mimo.env_file == _config.DEFAULT_MIMO_ENV_FILE
    assert mimo.model == _config.DEFAULT_MIMO_MODEL
    assert cfg.provider("codex").runner == "codex-cli"
    assert cfg.provider("nope") is None


def test_custom_provider_parsed_and_routable(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(
        repo / ".npc" / "config.toml",
        '[providers.kimi]\nenv_file = "~/.config/npc/kimi.env"\nmodel = "kimi-k3"\n'
        '[coder]\nbackend = "kimi"\n',
    )
    cfg = _config.load_config(repo, home=home)
    kimi = cfg.provider("kimi")
    assert kimi.runner == "claude-cli"  # 默认 runner
    assert kimi.model == "kimi-k3"
    assert cfg.coder.backend == "kimi"


def test_provider_unknown_runner_rejected(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(repo / ".npc" / "config.toml", '[providers.x]\nrunner = "gemini-cli"\n')
    with pytest.raises(_config.ConfigError, match="runner"):
        _config.load_config(repo, home=home)


def test_global_providers_project_routing_merge(tmp_path: Path):
    """核心场景：全局定义 provider（凭据层），项目只写路由。"""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(
        home / ".config" / "npc" / "config.toml",
        '[providers.deepseek]\nenv_file = "~/.config/npc/deepseek.env"\nmodel = "deepseek-chat"\n'
        '[providers.qwen]\nenv_file = "~/.config/npc/qwen.env"\nmodel = "qwen3-coder-plus"\n',
    )
    _write(
        repo / ".npc" / "config.toml",
        '[coder]\nbackend = "deepseek"\n[coder.phase]\nfix = "qwen"\n',
    )
    cfg = _config.load_config(repo, home=home)
    assert cfg.coder.backend == "deepseek"
    assert cfg.coder.backend_for_phase("fix") == "qwen"
    assert cfg.provider("deepseek").model == "deepseek-chat"
    assert cfg.provider("qwen").model == "qwen3-coder-plus"


def test_project_routing_to_undefined_provider_rejected(tmp_path: Path):
    """项目路由引用了任何层都没定义的 provider → 加载期报错，不留到运行期。"""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(repo / ".npc" / "config.toml", '[coder]\nbackend = "kimi"\n')
    with pytest.raises(_config.ConfigError, match="kimi"):
        _config.load_config(repo, home=home)


def test_project_overrides_builtin_provider(tmp_path: Path):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(
        repo / ".npc" / "config.toml",
        '[providers.mimo]\nenv_file = "/custom/mimo.env"\nmodel = "mimo-next"\n',
    )
    cfg = _config.load_config(repo, home=home)
    assert cfg.provider("mimo").env_file == "/custom/mimo.env"
    assert cfg.provider("mimo").model == "mimo-next"


def test_legacy_coder_mimo_env_file_flows_into_provider(tmp_path: Path):
    """旧字段 [coder.mimo].env_file 覆盖内置 mimo provider 的 env_file（兼容通道）。"""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _write(
        repo / ".npc" / "config.toml",
        '[coder]\nbackend = "mimo"\n[coder.mimo]\nenv_file = "/legacy/mimo.env"\n',
    )
    cfg = _config.load_config(repo, home=home)
    assert cfg.coder.mimo_env_file == "/legacy/mimo.env"
    assert cfg.provider("mimo").env_file == "/legacy/mimo.env"


# ============================================================
# v1.7 [host] 配置
# ============================================================


def test_host_section_defaults_to_none(tmp_path):
    cfg = _config.load_config(tmp_path, home=tmp_path / "home")
    assert cfg.host.name is None
    assert cfg.host.session_dir is None


def test_host_section_parsed(tmp_path):
    (tmp_path / ".npc").mkdir()
    (tmp_path / ".npc" / "config.toml").write_text(
        '[host]\nname = "kimi"\nsession_dir = ".kimi/s/{proj_key}"\n',
        encoding="utf-8",
    )
    cfg = _config.load_config(tmp_path, home=tmp_path / "home")
    assert cfg.host.name == "kimi"
    assert cfg.host.session_dir == ".kimi/s/{proj_key}"


def test_host_session_dir_rejects_unknown_placeholder(tmp_path):
    (tmp_path / ".npc").mkdir()
    (tmp_path / ".npc" / "config.toml").write_text(
        '[host]\nsession_dir = ".kimi/{project}"\n', encoding="utf-8"
    )
    with pytest.raises(_config.ConfigError, match="不支持的占位符"):
        _config.load_config(tmp_path, home=tmp_path / "home")


def test_host_session_dir_rejects_malformed_template(tmp_path):
    (tmp_path / ".npc").mkdir()
    (tmp_path / ".npc" / "config.toml").write_text(
        '[host]\nsession_dir = ".kimi/{proj_key"\n', encoding="utf-8"
    )
    with pytest.raises(_config.ConfigError, match="模板语法错误"):
        _config.load_config(tmp_path, home=tmp_path / "home")


def test_host_session_dir_allows_literal_without_placeholder(tmp_path):
    (tmp_path / ".npc").mkdir()
    (tmp_path / ".npc" / "config.toml").write_text(
        '[host]\nsession_dir = ".kimi/sessions"\n', encoding="utf-8"
    )
    cfg = _config.load_config(tmp_path, home=tmp_path / "home")
    assert cfg.host.session_dir == ".kimi/sessions"


def test_host_section_must_be_table(tmp_path):
    (tmp_path / ".npc").mkdir()
    (tmp_path / ".npc" / "config.toml").write_text('host = "kimi"\n', encoding="utf-8")
    with pytest.raises(_config.ConfigError):
        _config.load_config(tmp_path, home=tmp_path / "home")
