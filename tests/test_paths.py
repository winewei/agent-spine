"""paths 模块测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from npc import paths as _paths


def test_proj_key_mangling():
    assert _paths.proj_key_for(Path("/Users/you/code/foo")) == "-Users-you-code-foo"
    assert _paths.proj_key_for(Path("/")) == "-"


def test_proj_key_relative_path_rejected():
    with pytest.raises(_paths.PathsError):
        _paths.proj_key_for(Path("relative/path"))


def test_compute_paths_layout(fake_home: Path):
    p = _paths.compute_paths(
        Path("/Users/you/code/foo"), run_ts="2026-05-22-1545", home=fake_home
    )
    assert p.proj_key == "-Users-you-code-foo"
    assert p.task_log_dir == fake_home / "task_log" / "-Users-you-code-foo"
    assert p.run_dir == p.task_log_dir / "2026-05-22-1545"
    assert p.state_json == p.task_log_dir / "2026-05-22-1545-plan-state.json"
    assert p.state_md == p.task_log_dir / "2026-05-22-1545-plan-state.md"
    assert p.index_file == p.task_log_dir / "index.jsonl"
    assert p.schema_path == fake_home / "task_log" / ".new-plan-review-schema.json"
    assert p.run_events == p.run_dir / "run.events.jsonl"


def test_compute_paths_run_ts_default_format(fake_home: Path):
    p = _paths.compute_paths(Path("/Users/you/foo"), home=fake_home)
    # YYYY-MM-DD-HHMM
    import re

    assert re.match(r"^\d{4}-\d{2}-\d{2}-\d{4}$", p.run_ts)


def test_detect_repo_root(fake_repo: Path):
    root = _paths.detect_repo_root(fake_repo)
    assert root.resolve() == fake_repo.resolve()


def test_detect_repo_root_non_repo(tmp_path: Path):
    with pytest.raises(_paths.PathsError):
        _paths.detect_repo_root(tmp_path)


def test_ensure_dirs_creates_layout(computed_paths: _paths.Paths):
    assert computed_paths.task_log_dir.is_dir()
    assert computed_paths.run_dir.is_dir()
    assert computed_paths.schema_path.parent.is_dir()


def test_to_env_roundtrip(computed_paths: _paths.Paths, monkeypatch):
    for k, v in computed_paths.to_env().items():
        monkeypatch.setenv(k, v)
    p2 = _paths.load_paths_from_env()
    assert p2.repo_root == computed_paths.repo_root
    assert p2.proj_key == computed_paths.proj_key
    assert p2.run_ts == computed_paths.run_ts
    assert p2.state_json == computed_paths.state_json


def test_load_paths_missing_env(monkeypatch):
    for k in (
        "NPC_REPO_ROOT",
        "NPC_PROJ_KEY",
        "NPC_TASK_LOG_DIR",
        "NPC_RUN_TS",
        "NPC_RUN_DIR",
        "NPC_STATE_JSON",
        "NPC_STATE_MD",
        "NPC_INDEX_FILE",
        "NPC_SCHEMA_PATH",
        "NPC_RUN_EVENTS",
    ):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(_paths.PathsError) as ei:
        _paths.load_paths_from_env()
    assert "NPC_REPO_ROOT" in str(ei.value)


def test_base_for_zero_pads(computed_paths: _paths.Paths):
    base = _paths.base_for(computed_paths, 3, "add-foo")
    assert base.name == "003-add-foo"
    assert base.parent == computed_paths.run_dir


# ============================================================
# v0.2: run.json / active.json 持久化
# ============================================================


def test_write_and_read_run_json(computed_paths: _paths.Paths):
    target = _paths.write_run_json(computed_paths)
    assert target == computed_paths.run_dir / "run.json"
    assert target.is_file()
    restored = _paths.read_run_json(target)
    assert restored == computed_paths


def test_read_run_json_missing_field(tmp_path: Path):
    bad = tmp_path / "run.json"
    bad.write_text('{"schema_version":1,"repo_root":"/x"}', encoding="utf-8")
    with pytest.raises(_paths.PathsError) as ei:
        _paths.read_run_json(bad)
    assert "缺少字段" in str(ei.value)


def test_set_and_read_active(computed_paths: _paths.Paths):
    target = _paths.set_active(computed_paths.task_log_dir, computed_paths.run_ts)
    assert target == computed_paths.task_log_dir / "active.json"
    assert _paths.read_active(computed_paths.task_log_dir) == computed_paths.run_ts


def test_read_active_missing_returns_none(tmp_path: Path):
    assert _paths.read_active(tmp_path) is None


def test_load_paths_resolves_via_active_json(
    computed_paths: _paths.Paths, fake_repo: Path, monkeypatch
):
    """cwd 在 git 仓库 + active.json 指向有效 run.json → 不靠环境变量也能 resolve。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: computed_paths.task_log_dir.parent.parent))
    _paths.write_run_json(computed_paths)
    _paths.set_active(computed_paths.task_log_dir, computed_paths.run_ts)
    # 清空所有 NPC_* env，确保走文件路径
    for k in list(computed_paths.to_env().keys()):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(fake_repo)

    import argparse

    args = argparse.Namespace(state_json=None, run_ts=None, task_log_dir=None)
    p = _paths.load_paths(args)
    assert p.run_ts == computed_paths.run_ts
    assert p.run_dir == computed_paths.run_dir
    assert _paths.load_paths.last_source == "run_json_active"


def test_load_paths_explicit_run_ts(computed_paths: _paths.Paths, monkeypatch):
    _paths.write_run_json(computed_paths)
    for k in list(computed_paths.to_env().keys()):
        monkeypatch.delenv(k, raising=False)
    import argparse

    args = argparse.Namespace(
        state_json=None,
        run_ts=computed_paths.run_ts,
        task_log_dir=str(computed_paths.task_log_dir),
    )
    p = _paths.load_paths(args)
    assert p == computed_paths
    assert _paths.load_paths.last_source == "run_json_explicit"


def test_load_paths_state_json_override(computed_paths: _paths.Paths, monkeypatch, tmp_path: Path):
    _paths.write_run_json(computed_paths)
    override = tmp_path / "custom-state.json"
    import argparse

    args = argparse.Namespace(
        state_json=str(override),
        run_ts=computed_paths.run_ts,
        task_log_dir=str(computed_paths.task_log_dir),
    )
    p = _paths.load_paths(args)
    assert p.state_json == override
    # 其它字段保持不变
    assert p.run_dir == computed_paths.run_dir


def test_load_paths_fallback_to_env(computed_paths: _paths.Paths, monkeypatch, tmp_path: Path):
    """无 run.json，env 完整 → 回退 env。"""
    monkeypatch.chdir(tmp_path)  # 非 git 仓库
    for k, v in computed_paths.to_env().items():
        monkeypatch.setenv(k, v)
    import argparse

    args = argparse.Namespace(state_json=None, run_ts=None, task_log_dir=None)
    p = _paths.load_paths(args)
    assert p.run_ts == computed_paths.run_ts
    assert _paths.load_paths.last_source == "env"


def test_load_paths_all_missing_raises(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    for k in (
        "NPC_REPO_ROOT",
        "NPC_PROJ_KEY",
        "NPC_TASK_LOG_DIR",
        "NPC_RUN_TS",
        "NPC_RUN_DIR",
        "NPC_STATE_JSON",
        "NPC_STATE_MD",
        "NPC_INDEX_FILE",
        "NPC_SCHEMA_PATH",
        "NPC_RUN_EVENTS",
    ):
        monkeypatch.delenv(k, raising=False)
    import argparse

    args = argparse.Namespace(state_json=None, run_ts=None, task_log_dir=None)
    with pytest.raises(_paths.PathsError):
        _paths.load_paths(args)
