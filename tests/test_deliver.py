"""deliver.py 测试：deliver（push）与 pr open（gh pr create）两个对外动作。

- 纯函数 build_push_argv / build_gh_argv / parse_pr_url：各分支直接断言。
- handler：monkeypatch _resolve_repo_root + _which，注入假 runner，断言 emit/退出码。
"""

from __future__ import annotations

import json
import subprocess

import pytest

from npc import deliver as _deliver
from npc import paths as _paths


# ============================================================
# 假 runner 工厂
# ============================================================


def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):
    """返回固定结果的假 runner（忽略 argv）。"""

    def _runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return _runner


def _capturing_run(returncode: int, stdout: str = "", stderr: str = ""):
    """记录每次调用的假 runner，返回 (runner, calls)。"""
    calls: list[dict] = []

    def _runner(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return _runner, calls


def _raising_run(exc: Exception):
    """每次调用都抛出给定异常的假 runner（模拟子进程起不来）。"""

    def _runner(cmd, **kwargs):
        raise exc

    return _runner


def _branch_then(branch: str, push_rc: int = 0, push_stderr: str = ""):
    """组合 runner：第 1 次调用（rev-parse）返回分支，其余返回 push 结果。"""
    state = {"n": 0}

    def _runner(cmd, **kwargs):
        state["n"] += 1
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=branch + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, push_rc, stdout="", stderr=push_stderr)

    return _runner


# ============================================================
# 纯函数：build_push_argv
# ============================================================


def test_build_push_argv_with_upstream():
    assert _deliver.build_push_argv("origin", "feat/x", True) == [
        "git", "push", "-u", "origin", "feat/x",
    ]


def test_build_push_argv_without_upstream():
    assert _deliver.build_push_argv("upstream", "main", False) == [
        "git", "push", "upstream", "main",
    ]


# ============================================================
# 纯函数：build_gh_argv
# ============================================================


def test_build_gh_argv_minimal():
    assert _deliver.build_gh_argv(None, None, None, False) == ["gh", "pr", "create"]


def test_build_gh_argv_title_and_body():
    assert _deliver.build_gh_argv("My PR", "the body", None, False) == [
        "gh", "pr", "create", "--title", "My PR", "--body", "the body",
    ]


def test_build_gh_argv_base_and_draft():
    argv = _deliver.build_gh_argv("T", None, "develop", True)
    assert "--base" in argv and argv[argv.index("--base") + 1] == "develop"
    assert "--draft" in argv


def test_build_gh_argv_empty_body_still_passed():
    # body 为空串（已读取的空文件）仍应作为 --body 传入（区别于 None）
    argv = _deliver.build_gh_argv("T", "", None, False)
    assert "--body" in argv and argv[argv.index("--body") + 1] == ""


def test_build_gh_argv_rejects_body_file_kwarg():
    # body_file 已从签名移除：传它应当 TypeError（死参数不再被静默接受）
    with pytest.raises(TypeError):
        _deliver.build_gh_argv("T", None, None, False, body_file="x.md")


# ============================================================
# 纯函数：parse_pr_url
# ============================================================


def test_parse_pr_url_found():
    out = "https://github.com/owner/repo/pull/123\n"
    assert _deliver.parse_pr_url(out) == "https://github.com/owner/repo/pull/123"


def test_parse_pr_url_embedded_in_noise():
    out = "Creating pull request...\nhttps://github.com/o/r/pull/7 done\n"
    assert _deliver.parse_pr_url(out) == "https://github.com/o/r/pull/7"


def test_parse_pr_url_none_when_absent():
    assert _deliver.parse_pr_url("no url here") is None
    assert _deliver.parse_pr_url("") is None


# ============================================================
# deliver（push）handler
# ============================================================


def test_push_success(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/git")
    runner = _branch_then("feat/x", push_rc=0)
    _deliver.run_push(make_args(remote=None, branch=None, set_upstream=True), runner=runner)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["pushed"] is True
    assert out["remote"] == "origin"
    assert out["branch"] == "feat/x"


def test_push_explicit_remote_and_branch(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/git")
    runner, calls = _capturing_run(0)
    _deliver.run_push(
        make_args(remote="upstream", branch="release", set_upstream=False),
        runner=runner,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["remote"] == "upstream"
    assert out["branch"] == "release"
    # 显式 branch → 不调 rev-parse，直接 push
    assert calls[0]["cmd"] == ["git", "push", "upstream", "release"]


def test_push_failure_exit_1_with_stderr(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/git")
    runner = _branch_then("feat/x", push_rc=1, push_stderr="! [rejected] non-fast-forward\n")
    with pytest.raises(SystemExit) as ei:
        _deliver.run_push(make_args(remote=None, branch=None, set_upstream=True), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["pushed"] is False
    assert "rejected" in out["stderr_tail"]


def test_push_git_missing_exit_4(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: None)
    with pytest.raises(SystemExit) as ei:
        _deliver.run_push(make_args(remote=None, branch=None, set_upstream=True), runner=_fake_run(0))
    assert ei.value.code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "dependency_missing"


def test_push_not_a_repo_exit_3(make_args, capsys, monkeypatch):
    def _boom(args):
        raise _paths.PathsError("not a git repo")

    monkeypatch.setattr(_deliver, "_resolve_repo_root", _boom)
    with pytest.raises(SystemExit) as ei:
        _deliver.run_push(make_args(remote=None, branch=None, set_upstream=True), runner=_fake_run(0))
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "not_a_repo"


def test_push_detached_head_exit_2(tmp_path, make_args, capsys, monkeypatch):
    # rev-parse 返回 HEAD（游离）且无显式 --branch → 用法错误 exit 2
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/git")
    runner = _fake_run(0, stdout="HEAD\n")
    with pytest.raises(SystemExit) as ei:
        _deliver.run_push(make_args(remote=None, branch=None, set_upstream=True), runner=runner)
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


# ============================================================
# pr open handler
# ============================================================


def test_pr_open_success_parses_url(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/gh")
    runner = _fake_run(0, stdout="https://github.com/owner/repo/pull/123\n")
    _deliver.run_pr_open(
        make_args(title="My PR", body=None, body_file=None, base=None, draft=False),
        runner=runner,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["pr_url"] == "https://github.com/owner/repo/pull/123"
    assert out["title"] == "My PR"


def test_pr_open_gh_missing_exit_4(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: None)
    with pytest.raises(SystemExit) as ei:
        _deliver.run_pr_open(
            make_args(title=None, body=None, body_file=None, base=None, draft=False),
            runner=_fake_run(0),
        )
    assert ei.value.code == 4
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "dependency_missing"


def test_pr_open_gh_failure_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/gh")
    runner = _fake_run(1, stderr="GraphQL: pull request already exists\n")
    with pytest.raises(SystemExit) as ei:
        _deliver.run_pr_open(
            make_args(title="T", body=None, body_file=None, base=None, draft=False),
            runner=runner,
        )
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "gh_pr_create_failed"
    assert "already exists" in out["stderr_tail"]


def test_pr_open_not_a_repo_exit_3(make_args, capsys, monkeypatch):
    def _boom(args):
        raise _paths.PathsError("no run")

    monkeypatch.setattr(_deliver, "_resolve_repo_root", _boom)
    with pytest.raises(SystemExit) as ei:
        _deliver.run_pr_open(
            make_args(title=None, body=None, body_file=None, base=None, draft=False),
            runner=_fake_run(0),
        )
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "not_a_repo"


def test_pr_open_body_file_read_into_argv(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    summary = tmp_path / "run-summary.md"
    summary.write_text("## Summary\nDid the thing.\n", encoding="utf-8")
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/gh")
    runner, calls = _capturing_run(0, stdout="https://github.com/o/r/pull/9\n")
    _deliver.run_pr_open(
        make_args(title="T", body=None, body_file=str(summary), base=None, draft=False),
        runner=runner,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    argv = calls[0]["cmd"]
    assert "--body" in argv
    assert argv[argv.index("--body") + 1] == "## Summary\nDid the thing.\n"


def test_pr_open_body_file_missing_exit_2(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/gh")
    with pytest.raises(SystemExit) as ei:
        _deliver.run_pr_open(
            make_args(
                title="T", body=None, body_file=str(tmp_path / "nope.md"),
                base=None, draft=False,
            ),
            runner=_fake_run(0),
        )
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_pr_open_draft_and_base_flags_passed(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/gh")
    runner, calls = _capturing_run(0, stdout="https://github.com/o/r/pull/5\n")
    _deliver.run_pr_open(
        make_args(title="T", body=None, body_file=None, base="develop", draft=True),
        runner=runner,
    )
    argv = calls[0]["cmd"]
    assert "--draft" in argv
    assert "--base" in argv and argv[argv.index("--base") + 1] == "develop"


def test_push_runner_oserror_emits_json_not_crash(tmp_path, make_args, capsys, monkeypatch):
    # 注入抛 OSError 的 runner（rev-parse 这一步就炸）→ 不崩，产结构化 JSON + exit 1
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/git")
    runner = _raising_run(OSError("[Errno 2] No such file or directory: 'git'"))
    with pytest.raises(SystemExit) as ei:
        _deliver.run_push(make_args(remote=None, branch=None, set_upstream=True), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "subprocess_error"


def test_push_runner_oserror_on_push_step(tmp_path, make_args, capsys, monkeypatch):
    # 显式 branch → 跳过 rev-parse，push 那一步抛 OSError → 仍产 JSON 不崩
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/git")
    runner = _raising_run(OSError("boom"))
    with pytest.raises(SystemExit) as ei:
        _deliver.run_push(make_args(remote="origin", branch="feat/x", set_upstream=True), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "subprocess_error"


def test_pr_open_runner_timeout_emits_json_not_crash(tmp_path, make_args, capsys, monkeypatch):
    # 注入抛 TimeoutExpired 的 runner → 不崩，产结构化 JSON + exit 1
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/gh")
    runner = _raising_run(subprocess.TimeoutExpired(cmd="gh", timeout=5))
    with pytest.raises(SystemExit) as ei:
        _deliver.run_pr_open(
            make_args(title="T", body=None, body_file=None, base=None, draft=False),
            runner=runner,
        )
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "subprocess_error"


def test_push_failure_stderr_redacts_credentials(tmp_path, make_args, capsys, monkeypatch):
    # push 失败 stderr 含内嵌凭据 → 输出已脱敏，绝不含原始 token
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/git")
    leak = "fatal: unable to access 'https://tok123secret@github.com/o/r.git/': 403\n"
    runner = _branch_then("feat/x", push_rc=1, push_stderr=leak)
    with pytest.raises(SystemExit):
        _deliver.run_push(make_args(remote=None, branch=None, set_upstream=True), runner=runner)
    out = capsys.readouterr().out
    assert "tok123secret" not in out
    assert "<redacted>" in out


def test_pr_open_failure_stderr_redacts_credentials(tmp_path, make_args, capsys, monkeypatch):
    # gh 失败 stderr 含内嵌凭据 → 同样脱敏
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/gh")
    leak = "error: https://ghp_supersecret@github.com remote rejected\n"
    runner = _fake_run(1, stderr=leak)
    with pytest.raises(SystemExit):
        _deliver.run_pr_open(
            make_args(title="T", body=None, body_file=None, base=None, draft=False),
            runner=runner,
        )
    out = capsys.readouterr().out
    assert "ghp_supersecret" not in out
    assert "<redacted>" in out


def test_pr_open_success_but_no_url_warns_and_raw(tmp_path, make_args, capsys, monkeypatch):
    # gh 成功（rc 0）但 stdout 无 PR url → 不静默：pr_url=null + warn + raw_stdout_tail
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: "/usr/bin/gh")
    runner = _fake_run(0, stdout="Warning: something odd, no url emitted\n")
    _deliver.run_pr_open(
        make_args(title="T", body=None, body_file=None, base=None, draft=False),
        runner=runner,
    )
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["ok"] is True
    assert out["pr_url"] is None
    assert "raw_stdout_tail" in out
    assert "no url emitted" in out["raw_stdout_tail"]
    # warn 走 stderr，给人工信号，绝不静默
    assert "warn" in captured.err.lower()


def test_cli_deliver_and_pr_open_delegate(tmp_path, make_args, capsys, monkeypatch):
    # cli_* 入口稳定签名：仅委托给 run_* 默认 runner（覆盖 handler 包装层）
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(_deliver, "_resolve_repo_root", lambda args: repo)
    monkeypatch.setattr(_deliver, "_which", lambda name: None)  # 触发依赖缺失短路
    with pytest.raises(SystemExit) as ei:
        _deliver.cli_deliver(make_args(remote=None, branch=None, set_upstream=True))
    assert ei.value.code == 4
    capsys.readouterr()
    with pytest.raises(SystemExit) as ei2:
        _deliver.cli_pr_open(
            make_args(title=None, body=None, body_file=None, base=None, draft=False)
        )
    assert ei2.value.code == 4
