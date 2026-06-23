"""init_cmd 模块测试。

需要 chdir 到 fake_repo + 覆盖 Path.home 到 fake_home，否则会污染真实环境。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import init_cmd as _init


@pytest.fixture
def init_env(monkeypatch, fake_repo: Path, fake_home: Path):
    """切到 fake_repo 工作目录 + 把 Path.home 替换为 fake_home。"""
    monkeypatch.chdir(fake_repo)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_repo, fake_home


def test_ensure_portable_timeout_creates(tmp_path):
    target, created = _init.ensure_portable_timeout(home=tmp_path)
    assert created is True
    assert target.exists()
    assert target.stat().st_mode & 0o111  # executable
    content = target.read_text()
    assert "portable-timeout" in content


def test_ensure_portable_timeout_idempotent(tmp_path):
    _init.ensure_portable_timeout(home=tmp_path)
    target, created = _init.ensure_portable_timeout(home=tmp_path)
    assert created is False


def test_init_run_basic(init_env, capsys, make_args):
    _, home = init_env
    args = make_args(auto=False, fresh=False, shell_exports=False)
    _init.run(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert payload["repo_root"].endswith("/repo")
    assert payload["needs_resume"] is False
    assert payload["mode"] == "interactive"
    # 自举产物
    assert (home / "task_log" / ".new-plan-review-schema.json").exists()
    assert (home / ".local" / "bin" / "portable-timeout").exists()


def test_init_run_auto_mode_label(init_env, capsys, make_args):
    args = make_args(auto=True, fresh=False, shell_exports=False)
    _init.run(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["mode"] == "auto"


def test_init_shell_exports_format(init_env, capsys, make_args):
    args = make_args(auto=False, fresh=True, shell_exports=True)
    _init.run(args)
    out = capsys.readouterr().out
    assert "export NPC_REPO_ROOT=" in out
    assert "export NPC_STATE_JSON=" in out
    assert "export NPC_NEEDS_RESUME='false'" in out
    assert "export NPC_FRESH='true'" in out


def test_init_resume_detection(init_env, capsys, make_args):
    """有 in-progress 旧 run 时，init 应汇报 needs_resume=true 并复用其 run_ts。"""
    repo, home = init_env
    proj_key = "-" + str(repo).lstrip("/").replace("/", "-")
    task_log = home / "task_log" / proj_key
    task_log.mkdir(parents=True)
    old_state = task_log / "2026-05-01-1000-plan-state.json"
    old_state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_ts": "2026-05-01-1000",
                "status": "in-progress",
                "progress": [],
            }
        )
    )

    args = make_args(auto=False, fresh=False, shell_exports=False)
    _init.run(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["needs_resume"] is True
    assert payload["resume_state_json"] == str(old_state)
    assert payload["run_ts"] == "2026-05-01-1000"


def test_init_fresh_ignores_in_progress(init_env, capsys, make_args):
    repo, home = init_env
    proj_key = "-" + str(repo).lstrip("/").replace("/", "-")
    task_log = home / "task_log" / proj_key
    task_log.mkdir(parents=True)
    old_state = task_log / "2026-05-01-1000-plan-state.json"
    old_state.write_text(json.dumps({"status": "in-progress", "run_ts": "old"}))

    args = make_args(auto=False, fresh=True, shell_exports=False)
    _init.run(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["needs_resume"] is False
    assert payload["fresh"] is True
    # 新 run_ts 不会等于 "old"
    assert payload["run_ts"] != "old"


def test_init_writes_run_and_active_json(init_env, capsys, make_args):
    """v0.2: init 落 run.json 与 active.json，子命令可不依赖 env 自包含 resolve。"""
    _, home = init_env
    args = make_args(auto=False, fresh=True, shell_exports=False)
    _init.run(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    run_json = Path(payload["run_json"])
    active_json = Path(payload["active_json"])
    assert run_json.is_file()
    assert active_json.is_file()
    rj = json.loads(run_json.read_text())
    aj = json.loads(active_json.read_text())
    assert rj["run_ts"] == payload["run_ts"]
    assert rj["state_json"] == payload["state_json"]
    assert aj["current_run_ts"] == payload["run_ts"]


def test_init_shell_exports_emits_deprecation(init_env, capsys, make_args):
    args = make_args(auto=False, fresh=True, shell_exports=True)
    _init.run(args)
    err = capsys.readouterr().err
    assert "deprecated" in err.lower()


def test_init_non_git_repo(monkeypatch, tmp_path, capsys, make_args):
    """非 git 目录应报 exit 3。"""
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    monkeypatch.chdir(non_repo)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    args = make_args(auto=False, fresh=False, shell_exports=False)
    with pytest.raises(SystemExit) as ei:
        _init.run(args)
    assert ei.value.code == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "not_git_repo"
