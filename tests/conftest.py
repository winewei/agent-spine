"""共享 fixtures。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from npc import paths as _paths


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """创建一个临时 git 仓库。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# fake\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """提供独立的 fake HOME 目录，避免污染真实 ~/task_log。"""
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def computed_paths(fake_repo: Path, fake_home: Path) -> _paths.Paths:
    """基于 fake_repo + fake_home 计算的 Paths（固定 run_ts 便于断言）。"""
    p = _paths.compute_paths(fake_repo, run_ts="2026-05-22-1545", home=fake_home)
    _paths.ensure_dirs(p)
    return p


@pytest.fixture
def env_setup(computed_paths: _paths.Paths, monkeypatch) -> _paths.Paths:
    """把 NPC_* 环境变量注入，模拟 init --shell-exports 后的状态。"""
    for k, v in computed_paths.to_env().items():
        monkeypatch.setenv(k, v)
    # session 字段默认空
    monkeypatch.setenv("NPC_SESSION_ID", "")
    monkeypatch.setenv("NPC_TRANSCRIPT_PATH", "")
    monkeypatch.setenv("NPC_SESSION_SOURCE", "test")
    monkeypatch.setenv("NPC_MODE", "interactive")
    monkeypatch.setenv("NPC_FRESH", "false")
    return computed_paths


@pytest.fixture
def make_args():
    """工厂 fixture：构造 argparse.Namespace，便于直接调用 handler。"""
    import argparse

    def _factory(**kwargs):
        ns = argparse.Namespace(state_json=None, run_ts=None, task_log_dir=None)
        for k, v in kwargs.items():
            setattr(ns, k, v)
        return ns

    return _factory


@pytest.fixture(autouse=True)
def isolate_telemetry(tmp_path: Path, monkeypatch) -> Path:
    """把 NPC_TELEMETRY_ROOT 隔离到 tmp，避免污染真实 ~/task_log/_telemetry。"""
    tel_root = tmp_path / "_telemetry"
    monkeypatch.setenv("NPC_TELEMETRY_ROOT", str(tel_root))
    return tel_root
