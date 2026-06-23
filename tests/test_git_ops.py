"""git_ops.py 测试：branch-for / ensure-clean / commit 三个 SDD git 卫生命令。

纯函数（branch_name_for / derive_commit_message / parse_porcelain）直接测；
handler 用假 runner 模拟 git 输出 + monkeypatch _resolve_repo_root，验 emit/退出码。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from npc import git_ops as _git
from npc import paths as _paths


# ============================================================
# 假 runner 基础设施
# ============================================================


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


class ScriptedRunner:
    """按 git 子命令前缀脚本化返回值，并记录所有调用。

    rules: list[(predicate, CompletedProcess-factory)]；predicate 接收 argv 列表。
    default: 兜底返回（默认 returncode=0）。
    """

    def __init__(self, rules=None, default_rc=0):
        self.rules = rules or []
        self.default_rc = default_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        for predicate, factory in self.rules:
            if predicate(cmd):
                return factory(cmd)
        return _completed(cmd, self.default_rc)


def _patch_repo(monkeypatch, repo: Path):
    monkeypatch.setattr(_git, "_resolve_repo_root", lambda args: repo)


# ============================================================
# 纯函数：branch_name_for
# ============================================================


def test_branch_name_for_deterministic():
    assert _git.branch_name_for("add-foo") == "change/add-foo"
    assert _git.branch_name_for("x") == "change/x"
    # 确定性：同输入同输出
    assert _git.branch_name_for("add-foo") == _git.branch_name_for("add-foo")


# ============================================================
# 纯函数：derive_commit_message
# ============================================================


def test_derive_message_explicit_wins():
    assert _git.derive_commit_message("feat: x", "change-id", "implement") == "feat: x"


def test_derive_message_explicit_stripped():
    assert _git.derive_commit_message("  feat: x  ", None, None) == "feat: x"


def test_derive_message_from_change_and_phase():
    assert (
        _git.derive_commit_message(None, "add-foo", "implement")
        == "chore(spine): implement add-foo"
    )


def test_derive_message_from_change_only():
    assert _git.derive_commit_message(None, "add-foo", None) == "chore(spine): add-foo"


def test_derive_message_empty_phase_treated_as_none():
    assert _git.derive_commit_message(None, "add-foo", "   ") == "chore(spine): add-foo"


def test_derive_message_empty_message_falls_through_to_change():
    assert _git.derive_commit_message("   ", "add-foo", None) == "chore(spine): add-foo"


def test_derive_message_all_missing_returns_none():
    assert _git.derive_commit_message(None, None, None) is None
    assert _git.derive_commit_message("", "", "") is None


# ============================================================
# 纯函数：parse_porcelain
# ============================================================


def test_parse_porcelain_empty():
    assert _git.parse_porcelain("") == []
    assert _git.parse_porcelain("\n  \n") == []


def test_parse_porcelain_basic():
    out = " M src/a.py\n?? new.txt\nA  staged.py\n"
    assert _git.parse_porcelain(out) == ["src/a.py", "new.txt", "staged.py"]


def test_parse_porcelain_rename_takes_new_path():
    out = "R  old.py -> new.py\n"
    assert _git.parse_porcelain(out) == ["new.py"]


def test_parse_porcelain_z_basic():
    # -z 用 NUL 分隔，路径原样（不加引号）
    out = " M src/a.py\x00?? new.txt\x00A  staged.py\x00"
    assert _git.parse_porcelain(out) == ["src/a.py", "new.txt", "staged.py"]


def test_parse_porcelain_z_path_with_space():
    # 含空格的路径在 -z 模式下原样保留，不被截断
    out = ' M src/with space.py\x00?? a b c.txt\x00'
    assert _git.parse_porcelain(out) == ["src/with space.py", "a b c.txt"]


def test_parse_porcelain_z_rename_skips_old_path():
    # -z 下重命名形如 "R  new\0old\0"，old 侧需跳过
    out = "R  new name.py\x00old name.py\x00 M other.py\x00"
    assert _git.parse_porcelain(out) == ["new name.py", "other.py"]


def test_parse_porcelain_lines_quoted_path_unquoted():
    # 回退（无 NUL）：带引号的路径去掉首尾引号
    out = '?? "weird path.txt"\n'
    assert _git.parse_porcelain(out) == ["weird path.txt"]


# ============================================================
# 纯函数：change-id / commit message 校验
# ============================================================


def test_validate_change_id_accepts_safe():
    assert _git._validate_change_id("add-foo") is True
    assert _git._validate_change_id("a.b_c-1") is True


def test_validate_change_id_rejects_dotdot():
    assert _git._validate_change_id("../etc") is False
    assert _git._validate_change_id("a..b") is False


def test_validate_change_id_rejects_leading_dash():
    assert _git._validate_change_id("-rf") is False


def test_validate_change_id_rejects_space_and_special():
    assert _git._validate_change_id("a b") is False
    assert _git._validate_change_id("a;rm") is False
    assert _git._validate_change_id("") is False


def test_branch_name_for_rejects_invalid():
    with pytest.raises(ValueError):
        _git.branch_name_for("../evil")


def test_is_safe_commit_message():
    assert _git._is_safe_commit_message("feat: ok") is True
    assert _git._is_safe_commit_message("line1\nline2") is False
    assert _git._is_safe_commit_message("-rf") is False
    assert _git._is_safe_commit_message("#comment") is False


# ============================================================
# branch-for handler
# ============================================================


def test_branch_for_creates_when_absent(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    # rev-parse --verify 失败 → 分支不存在；checkout -b 成功
    runner = ScriptedRunner(
        rules=[
            (lambda c: "rev-parse" in c and "--verify" in c, lambda c: _completed(c, 1)),
            (lambda c: c[:3] == ["git", "checkout", "-b"], lambda c: _completed(c, 0)),
        ]
    )
    _git.cli_branch_for(make_args(change="add-foo"), runner=runner)
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "branch": "change/add-foo", "created": True}
    # 验证确实走了 checkout -b
    assert ["git", "checkout", "-b", "change/add-foo"] in runner.calls


def test_branch_for_checkout_when_exists(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    # rev-parse --verify 成功 → 分支已存在；checkout 成功
    runner = ScriptedRunner(
        rules=[
            (lambda c: "rev-parse" in c and "--verify" in c, lambda c: _completed(c, 0)),
        ]
    )
    _git.cli_branch_for(make_args(change="add-foo"), runner=runner)
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "branch": "change/add-foo", "created": False}
    assert ["git", "checkout", "change/add-foo"] in runner.calls
    assert ["git", "checkout", "-b", "change/add-foo"] not in runner.calls


def test_branch_for_missing_change_exit_2(tmp_path, make_args, capsys, monkeypatch):
    _patch_repo(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as ei:
        _git.cli_branch_for(make_args(change=None), runner=ScriptedRunner())
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "usage_error"


def test_branch_for_empty_change_exit_2(make_args, capsys, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _git.cli_branch_for(make_args(change="   "), runner=ScriptedRunner())
    assert ei.value.code == 2


def test_branch_for_checkout_failure_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    runner = ScriptedRunner(
        rules=[
            (lambda c: "rev-parse" in c and "--verify" in c, lambda c: _completed(c, 1)),
            (
                lambda c: c[:3] == ["git", "checkout", "-b"],
                lambda c: _completed(c, 128, stderr="fatal: boom"),
            ),
        ]
    )
    with pytest.raises(SystemExit) as ei:
        _git.cli_branch_for(make_args(change="add-foo"), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "git_checkout_failed"
    assert "boom" in out["stderr"]


def test_branch_for_invalid_change_id_dotdot_exit_2(make_args, capsys, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _git.cli_branch_for(make_args(change="../evil"), runner=ScriptedRunner())
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


def test_branch_for_invalid_change_id_leading_dash_exit_2(make_args, capsys, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _git.cli_branch_for(make_args(change="-rf"), runner=ScriptedRunner())
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_branch_for_invalid_change_id_space_exit_2(make_args, capsys, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _git.cli_branch_for(make_args(change="a b"), runner=ScriptedRunner())
    assert ei.value.code == 2


def test_branch_for_oserror_emits_json_exit_3(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)

    def _boom_runner(cmd, **kwargs):
        raise OSError("git binary not found")

    with pytest.raises(SystemExit) as ei:
        _git.cli_branch_for(make_args(change="add-foo"), runner=_boom_runner)
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "git_error"


def test_branch_for_not_a_repo_exit_3(make_args, capsys, monkeypatch):
    def _boom(args):
        raise _paths.PathsError("not a git repo")

    monkeypatch.setattr(_git, "_resolve_repo_root", _boom)
    with pytest.raises(SystemExit) as ei:
        _git.cli_branch_for(make_args(change="add-foo"), runner=ScriptedRunner())
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "not_a_repo"


# ============================================================
# ensure-clean handler
# ============================================================


def test_ensure_clean_when_clean_exit_0(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    runner = ScriptedRunner(
        rules=[(lambda c: "status" in c, lambda c: _completed(c, 0, stdout=""))]
    )
    # clean → 不抛 SystemExit
    _git.cli_ensure_clean(make_args(), runner=runner)
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "clean": True, "dirty_files": []}


def test_ensure_clean_when_dirty_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    porcelain = " M src/a.py\n?? b.txt\n"
    runner = ScriptedRunner(
        rules=[(lambda c: "status" in c, lambda c: _completed(c, 0, stdout=porcelain))]
    )
    with pytest.raises(SystemExit) as ei:
        _git.cli_ensure_clean(make_args(), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["clean"] is False
    assert out["dirty_files"] == ["src/a.py", "b.txt"]


def test_ensure_clean_status_failure_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    # git status 失败（returncode≠0）绝不能被当作 clean
    runner = ScriptedRunner(
        rules=[(lambda c: "status" in c, lambda c: _completed(c, 128, stderr="fatal: boom"))]
    )
    with pytest.raises(SystemExit) as ei:
        _git.cli_ensure_clean(make_args(), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "git_status_failed"
    assert "boom" in out["stderr"]


def test_ensure_clean_oserror_exit_3(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)

    def _boom_runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    with pytest.raises(SystemExit) as ei:
        _git.cli_ensure_clean(make_args(), runner=_boom_runner)
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "git_error"


def test_ensure_clean_dirty_z_path_with_space(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    porcelain = " M src/with space.py\x00?? b c.txt\x00"
    runner = ScriptedRunner(
        rules=[(lambda c: "status" in c, lambda c: _completed(c, 0, stdout=porcelain))]
    )
    with pytest.raises(SystemExit) as ei:
        _git.cli_ensure_clean(make_args(), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["dirty_files"] == ["src/with space.py", "b c.txt"]


def test_ensure_clean_not_a_repo_exit_3(make_args, capsys, monkeypatch):
    def _boom(args):
        raise _paths.PathsError("not a git repo")

    monkeypatch.setattr(_git, "_resolve_repo_root", _boom)
    with pytest.raises(SystemExit) as ei:
        _git.cli_ensure_clean(make_args(), runner=ScriptedRunner())
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "not_a_repo"


# ============================================================
# commit handler
# ============================================================


def _commit_runner(commit_rc=0, commit_out="", commit_err="", head="abc123", branch="change/x"):
    """构造一个支持 add / commit / rev-parse 的脚本化 runner。"""
    return ScriptedRunner(
        rules=[
            (lambda c: c[:2] == ["git", "add"], lambda c: _completed(c, 0)),
            (
                lambda c: c[:2] == ["git", "commit"],
                lambda c: _completed(c, commit_rc, stdout=commit_out, stderr=commit_err),
            ),
            (
                lambda c: "rev-parse" in c and "--abbrev-ref" in c,
                lambda c: _completed(c, 0, stdout=branch + "\n"),
            ),
            (
                lambda c: c == ["git", "rev-parse", "HEAD"],
                lambda c: _completed(c, 0, stdout=head + "\n"),
            ),
        ]
    )


def test_commit_with_explicit_message(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    runner = _commit_runner(head="deadbeef", branch="change/add-foo")
    _git.cli_commit(make_args(message="feat: add foo", change=None, phase=None), runner=runner)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["committed"] is True
    assert out["commit"] == "deadbeef"
    assert out["message"] == "feat: add foo"
    assert out["branch"] == "change/add-foo"
    # 确认 git add -A 与 git commit -m 都被调用
    assert ["git", "add", "-A"] in runner.calls
    assert ["git", "commit", "-m", "feat: add foo"] in runner.calls


def test_commit_derives_message_from_change_phase(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    runner = _commit_runner()
    _git.cli_commit(make_args(message=None, change="add-foo", phase="implement"), runner=runner)
    out = json.loads(capsys.readouterr().out)
    assert out["committed"] is True
    assert out["message"] == "chore(spine): implement add-foo"
    assert ["git", "commit", "-m", "chore(spine): implement add-foo"] in runner.calls


def test_commit_nothing_to_commit_exit_0(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    runner = _commit_runner(
        commit_rc=1, commit_out="nothing to commit, working tree clean\n"
    )
    # nothing-to-commit → 不抛 SystemExit（exit 0）
    _git.cli_commit(make_args(message="x", change=None, phase=None), runner=runner)
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "committed": False, "reason": "nothing-to-commit"}


def test_commit_real_failure_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    runner = _commit_runner(commit_rc=1, commit_err="fatal: pre-commit hook failed\n")
    with pytest.raises(SystemExit) as ei:
        _git.cli_commit(make_args(message="x", change=None, phase=None), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["committed"] is False
    assert out["error"] == "git_commit_failed"


def test_commit_missing_everything_exit_2(make_args, capsys, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _git.cli_commit(
            make_args(message=None, change=None, phase=None), runner=ScriptedRunner()
        )
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "usage_error"


def test_commit_add_failure_exit_1(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)
    runner = ScriptedRunner(
        rules=[(lambda c: c[:2] == ["git", "add"], lambda c: _completed(c, 1, stderr="boom"))]
    )
    with pytest.raises(SystemExit) as ei:
        _git.cli_commit(make_args(message="x", change=None, phase=None), runner=runner)
    assert ei.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "git_add_failed"


def test_commit_message_with_newline_rejected_exit_2(make_args, capsys, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _git.cli_commit(
            make_args(message="line1\nline2", change=None, phase=None),
            runner=ScriptedRunner(),
        )
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


def test_commit_message_leading_dash_rejected_exit_2(make_args, capsys, monkeypatch):
    with pytest.raises(SystemExit) as ei:
        _git.cli_commit(
            make_args(message="-rf boom", change=None, phase=None),
            runner=ScriptedRunner(),
        )
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_args"


def test_commit_oserror_emits_json_exit_3(tmp_path, make_args, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _patch_repo(monkeypatch, repo)

    def _boom_runner(cmd, **kwargs):
        raise OSError("git binary not found")

    with pytest.raises(SystemExit) as ei:
        _git.cli_commit(make_args(message="x", change=None, phase=None), runner=_boom_runner)
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"] == "git_error"


def test_commit_not_a_repo_exit_3(make_args, capsys, monkeypatch):
    def _boom(args):
        raise _paths.PathsError("not a git repo")

    monkeypatch.setattr(_git, "_resolve_repo_root", _boom)
    with pytest.raises(SystemExit) as ei:
        _git.cli_commit(make_args(message="x", change=None, phase=None), runner=ScriptedRunner())
    assert ei.value.code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "not_a_repo"


# ============================================================
# 集成：真实 git 仓库（端到端 smoke，验证默认 runner 与真实 git 协同）
# ============================================================


def test_branch_for_real_repo_end_to_end(fake_repo, make_args, capsys, monkeypatch):
    monkeypatch.setattr(_git, "_resolve_repo_root", lambda args: fake_repo)
    _git.cli_branch_for(make_args(change="real-change"), runner=subprocess.run)
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["branch"] == "change/real-change"
    assert out["created"] is True
    # 再次调用同分支 → checkout，created=false
    _git.cli_branch_for(make_args(change="real-change"), runner=subprocess.run)
    out2 = json.loads(capsys.readouterr().out)
    assert out2["created"] is False


def test_commit_real_repo_end_to_end(fake_repo, make_args, capsys, monkeypatch):
    monkeypatch.setattr(_git, "_resolve_repo_root", lambda args: fake_repo)
    (fake_repo / "newfile.txt").write_text("hello\n")
    _git.cli_commit(
        make_args(message=None, change="add-newfile", phase="implement"),
        runner=subprocess.run,
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["committed"] is True
    assert out["message"] == "chore(spine): implement add-newfile"
    assert len(out["commit"]) == 40  # 完整 sha
    # 紧接 ensure-clean 应判定干净
    _git.cli_ensure_clean(make_args(), runner=subprocess.run)
    clean_out = json.loads(capsys.readouterr().out)
    assert clean_out["clean"] is True


def test_commit_real_repo_nothing_to_commit(fake_repo, make_args, capsys, monkeypatch):
    monkeypatch.setattr(_git, "_resolve_repo_root", lambda args: fake_repo)
    # fake_repo 初始即 clean，无改动可提交
    _git.cli_commit(make_args(message="noop", change=None, phase=None), runner=subprocess.run)
    out = json.loads(capsys.readouterr().out)
    assert out["committed"] is False
    assert out["reason"] == "nothing-to-commit"
