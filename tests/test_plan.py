"""plan.py 测试：plan check（SDD 前置门）与 plan new-change（脚手架）。

注入假 runner 返回预设 openspec stdout；monkeypatch ``_resolve_repo_root`` /
``shutil.which`` 控制 repo 定位与依赖发现。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from npc import plan as _plan


# ============================================================
# 辅助：假 runner + args 工厂
# ============================================================


def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):
    """构造一个伪 subprocess.run，返回固定的 CompletedProcess。"""

    def _runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return _runner


def _check_args(make_args, **kw):
    kw.setdefault("change", "add-foo")
    kw.setdefault("phase", "implement")
    kw.setdefault("openspec_bin", None)
    return make_args(**kw)


def _new_args(make_args, **kw):
    kw.setdefault("change", "add-foo")
    kw.setdefault("description", None)
    kw.setdefault("schema", None)
    kw.setdefault("openspec_bin", None)
    return make_args(**kw)


@pytest.fixture
def _has_openspec(monkeypatch):
    """让 _find_openspec_bin 返回一个固定路径（不依赖系统是否真装 openspec）。"""
    monkeypatch.setattr(_plan.shutil, "which", lambda name: "/usr/bin/openspec")


@pytest.fixture
def _repo(tmp_path, monkeypatch):
    """让 _resolve_repo_root 返回一个临时 repo 目录。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_plan, "_resolve_repo_root", lambda args: repo)
    return repo


# ============================================================
# 纯函数：_parse_status_payload
# ============================================================


def test_parse_status_all_done_empty_missing():
    payload = {
        "artifacts": [
            {"id": "spec", "status": "done"},
            {"id": "tasks", "status": "done"},
        ]
    }
    assert _plan._parse_status_payload(payload, ["spec", "tasks"]) == []


def test_parse_status_some_not_done():
    payload = {
        "artifacts": [
            {"id": "spec", "status": "done"},
            {"id": "tasks", "status": "draft"},
        ]
    }
    assert _plan._parse_status_payload(payload, ["spec", "tasks"]) == ["tasks"]


def test_parse_status_missing_artifact_counts_as_not_done():
    payload = {"artifacts": [{"id": "spec", "status": "done"}]}
    assert _plan._parse_status_payload(payload, ["spec", "design"]) == ["design"]


def test_parse_status_empty_artifacts():
    assert _plan._parse_status_payload({}, ["spec"]) == ["spec"]


# ============================================================
# plan check：handler
# ============================================================


def test_check_ready_exit0(make_args, capsys, _repo, _has_openspec):
    stdout = json.dumps(
        {
            "applyRequires": ["spec", "tasks"],
            "artifacts": [
                {"id": "spec", "status": "done"},
                {"id": "tasks", "status": "done"},
            ],
        }
    )
    # ready → 不抛 SystemExit（退出码 0 = 正常返回），且 argv 含 --change <id>
    captured = {}

    def _runner(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    _plan.run_check(_check_args(make_args), runner=_runner)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["ready"] is True
    assert out["change"] == "add-foo"
    assert out["phase"] == "implement"
    assert out["apply_requires"] == ["spec", "tasks"]
    assert out["missing"] == []
    # happy path 也断言 argv 形态正确（--change 紧跟 change-id）
    argv = captured["argv"]
    assert "--change" in argv
    assert argv[argv.index("--change") + 1] == "add-foo"


def test_check_leading_dash_change_exit2(make_args, capsys, _repo, _has_openspec):
    # change 以 '-' 开头 → 参数注入防护，exit 2，且不应调用 runner
    def _boom_runner(argv, **kwargs):
        raise AssertionError("runner 不应被调用")

    with pytest.raises(SystemExit) as ei:
        _plan.run_check(_check_args(make_args, change="--force"), runner=_boom_runner)
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_check_subprocess_oserror_json_intact(make_args, capsys, _repo, _has_openspec):
    # runner 抛 OSError → 不裸抛、JSON 契约不崩，exit 1
    def _raises(argv, **kwargs):
        raise OSError("boom: no such file")

    with pytest.raises(SystemExit) as ei:
        _plan.run_check(_check_args(make_args), runner=_raises)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "subprocess_error"


def test_check_not_ready_exit1_with_missing(make_args, capsys, _repo, _has_openspec):
    stdout = json.dumps(
        {
            "applyRequires": ["spec", "tasks", "design"],
            "artifacts": [
                {"id": "spec", "status": "done"},
                {"id": "tasks", "status": "draft"},
            ],
        }
    )
    with pytest.raises(SystemExit) as ei:
        _plan.run_check(_check_args(make_args), runner=_fake_run(0, stdout=stdout))
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["ready"] is False
    # tasks(draft) + design(缺失) 都算 missing
    assert out["missing"] == ["tasks", "design"]


def test_check_custom_phase_echoed(make_args, capsys, _repo, _has_openspec):
    stdout = json.dumps({"applyRequires": [], "artifacts": []})
    _plan.run_check(
        _check_args(make_args, phase="review"),
        runner=_fake_run(0, stdout=stdout),
    )
    out = json.loads(capsys.readouterr().out)
    assert out["phase"] == "review"
    assert out["ready"] is True


def test_check_invalid_json_exit1(make_args, capsys, _repo, _has_openspec):
    with pytest.raises(SystemExit) as ei:
        _plan.run_check(
            _check_args(make_args), runner=_fake_run(0, stdout="not json {{{")
        )
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "invalid_json"


def test_check_openspec_call_failed_exit1(make_args, capsys, _repo, _has_openspec):
    with pytest.raises(SystemExit) as ei:
        _plan.run_check(
            _check_args(make_args),
            runner=_fake_run(2, stderr="change not found\n"),
        )
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "openspec_failed"


def test_check_openspec_missing_exit4(make_args, capsys, _repo, monkeypatch):
    monkeypatch.setattr(_plan.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as ei:
        _plan.run_check(_check_args(make_args), runner=_fake_run(0, stdout="{}"))
    assert ei.value.code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "dependency_missing"


def test_check_missing_change_exit2(make_args, capsys):
    with pytest.raises(SystemExit) as ei:
        _plan.run_check(_check_args(make_args, change=None), runner=_fake_run(0))
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_check_non_git_repo_exit3(make_args, capsys, _has_openspec, monkeypatch):
    from npc import paths as _paths

    def _boom(args):
        raise _paths.PathsError("not a git repo")

    monkeypatch.setattr(_plan, "_resolve_repo_root", _boom)
    with pytest.raises(SystemExit) as ei:
        _plan.run_check(_check_args(make_args), runner=_fake_run(0, stdout="{}"))
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "env_missing"


# ============================================================
# plan new-change：handler
# ============================================================


def _seed_change_dir(repo: Path, change: str) -> Path:
    d = repo / "openspec" / "changes" / change
    d.mkdir(parents=True)
    (d / "proposal.md").write_text("# proposal\n")
    (d / "tasks.md").write_text("# tasks\n")
    specs = d / "specs"
    specs.mkdir()
    (specs / "spec.md").write_text("# spec\n")
    return d


def test_new_change_success_lists_files_exit0(make_args, capsys, _repo, _has_openspec):
    # 假 runner 不真正生成文件；测试里预建目录模拟 openspec 产物
    change_dir = _seed_change_dir(_repo, "add-foo")
    _plan.run_new_change(_new_args(make_args), runner=_fake_run(0))
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["change"] == "add-foo"
    assert out["path"] == str(change_dir)
    assert set(out["files"]) == {"proposal.md", "tasks.md", "specs/spec.md"}


def test_new_change_passes_description_and_schema(make_args, capsys, _repo, _has_openspec):
    captured = {}

    def _runner(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    _seed_change_dir(_repo, "add-foo")
    _plan.run_new_change(
        _new_args(make_args, description="do a thing", schema="myschema"),
        runner=_runner,
    )
    argv = captured["argv"]
    assert "new" in argv and "change" in argv and "add-foo" in argv
    assert "--description" in argv and "do a thing" in argv
    assert "--schema" in argv and "myschema" in argv


def test_new_change_no_optional_flags_when_absent(make_args, capsys, _repo, _has_openspec):
    captured = {}

    def _runner(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    _seed_change_dir(_repo, "add-foo")
    _plan.run_new_change(_new_args(make_args), runner=_runner)
    assert "--description" not in captured["argv"]
    assert "--schema" not in captured["argv"]


def test_new_change_openspec_failed_exit1_with_stderr(make_args, capsys, _repo, _has_openspec):
    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(
            _new_args(make_args),
            runner=_fake_run(1, stderr="change already exists\n"),
        )
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "openspec_failed"
    assert "change already exists" in out["message"]


def test_new_change_openspec_missing_exit4(make_args, capsys, _repo, monkeypatch):
    monkeypatch.setattr(_plan.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(_new_args(make_args), runner=_fake_run(0))
    assert ei.value.code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "dependency_missing"


def test_new_change_missing_change_exit2(make_args, capsys):
    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(_new_args(make_args, change=None), runner=_fake_run(0))
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_new_change_non_git_repo_exit3(make_args, capsys, _has_openspec, monkeypatch):
    from npc import paths as _paths

    def _boom(args):
        raise _paths.PathsError("not a git repo")

    monkeypatch.setattr(_plan, "_resolve_repo_root", _boom)
    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(_new_args(make_args), runner=_fake_run(0))
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "env_missing"


def test_new_change_leading_dash_change_exit2(make_args, capsys, _repo, _has_openspec):
    def _boom_runner(argv, **kwargs):
        raise AssertionError("runner 不应被调用")

    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(
            _new_args(make_args, change="--evil"), runner=_boom_runner
        )
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_new_change_leading_dash_description_exit2(make_args, capsys, _repo, _has_openspec):
    def _boom_runner(argv, **kwargs):
        raise AssertionError("runner 不应被调用")

    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(
            _new_args(make_args, description="--inject"), runner=_boom_runner
        )
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_new_change_leading_dash_schema_exit2(make_args, capsys, _repo, _has_openspec):
    def _boom_runner(argv, **kwargs):
        raise AssertionError("runner 不应被调用")

    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(
            _new_args(make_args, schema="--inject"), runner=_boom_runner
        )
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


@pytest.mark.parametrize("bad", ["../escape", "foo/bar", "a/../b", ".."])
def test_new_change_path_traversal_exit2(make_args, capsys, _repo, _has_openspec, bad):
    def _boom_runner(argv, **kwargs):
        raise AssertionError("runner 不应被调用")

    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(_new_args(make_args, change=bad), runner=_boom_runner)
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_new_change_subprocess_oserror_json_intact(make_args, capsys, _repo, _has_openspec):
    def _raises(argv, **kwargs):
        raise OSError("boom: cannot exec")

    with pytest.raises(SystemExit) as ei:
        _plan.run_new_change(_new_args(make_args), runner=_raises)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "subprocess_error"


def test_new_change_empty_dir_lists_no_files(make_args, capsys, _repo, _has_openspec):
    # openspec 成功但目录未建（边界）：files 为空，仍 ok
    _plan.run_new_change(_new_args(make_args), runner=_fake_run(0))
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["files"] == []


# ============================================================
# cli_* handler 薄封装
# ============================================================


def test_cli_check_with_runner_monkeypatch(make_args, capsys, _repo, _has_openspec, monkeypatch):
    stdout = json.dumps({"applyRequires": ["spec"], "artifacts": [{"id": "spec", "status": "done"}]})
    orig = _plan.run_check
    monkeypatch.setattr(
        _plan, "run_check", lambda args: orig(args, runner=_fake_run(0, stdout=stdout))
    )
    _plan.cli_check(_check_args(make_args))
    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is True
