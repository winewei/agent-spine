"""hosts 模块单元测试（v1.7 宿主抽象）。"""

from __future__ import annotations

from pathlib import Path

from npc import hosts


def test_resolve_default_is_generic_without_env():
    h = hosts.resolve_host(env={})
    assert h.name == "generic"
    assert h.source == "default"
    assert h.session_dir_template is None
    assert h.settings_grant is False


def test_resolve_env_detects_claude():
    h = hosts.resolve_host(env={"CLAUDECODE": "1"})
    assert h.name == "claude"
    assert h.source == "env"
    assert h.settings_grant is True
    assert h.session_dir_template == hosts.CLAUDE_SESSION_DIR_TEMPLATE


def test_config_name_overrides_env():
    h = hosts.resolve_host("generic", env={"CLAUDECODE": "1"})
    assert h.name == "generic"
    assert h.source == "config"
    assert h.settings_grant is False


def test_unknown_host_name_gets_generic_capabilities():
    h = hosts.resolve_host("kimi", env={})
    assert h.name == "kimi"
    assert h.settings_grant is False
    assert h.session_dir_template is None


def test_custom_session_dir_enables_mtime_scan(tmp_path: Path):
    h = hosts.resolve_host("kimi", ".kimi/sessions/{proj_key}", env={})
    d = h.session_dir(tmp_path, "-proj-x")
    assert d == tmp_path / ".kimi" / "sessions" / "-proj-x"


def test_claude_session_dir_layout(tmp_path: Path):
    h = hosts.resolve_host("claude", env={})
    assert h.session_dir(tmp_path, "-p") == tmp_path / ".claude" / "projects" / "-p"


def test_generic_session_dir_is_none(tmp_path: Path):
    h = hosts.resolve_host("generic", env={})
    assert h.session_dir(tmp_path, "-p") is None


def test_session_dir_override_on_claude(tmp_path: Path):
    h = hosts.resolve_host("claude", "custom/{proj_key}", env={})
    assert h.session_dir(tmp_path, "-p") == tmp_path / "custom" / "-p"


def test_resolve_from_config(tmp_path: Path):
    from npc import config as _config

    (tmp_path / ".npc").mkdir()
    (tmp_path / ".npc" / "config.toml").write_text(
        '[host]\nname = "kimi"\nsession_dir = ".kimi/s/{proj_key}"\n', encoding="utf-8"
    )
    cfg = _config.load_config(tmp_path, home=tmp_path / "home")
    h = hosts.resolve_host_from_config(cfg, env={})
    assert h.name == "kimi"
    assert h.session_dir_template == ".kimi/s/{proj_key}"
