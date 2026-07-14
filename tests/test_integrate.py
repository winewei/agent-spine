"""integrate（v1.5 整合编排下沉）测试。

git 操作在 tmp fake repo 上真实执行；verify tests 通过注入 runner 打桩。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from npc import integrate as _integrate
from npc import state as _state


# ============================================================
# Helpers
# ============================================================


def _bootstrap_run(make_args, capsys, *change_ids: str) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def _git(repo: Path, *argv: str) -> str:
    out = subprocess.run(
        ["git", *argv], cwd=repo, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _side_branch_commit(repo: Path, fname: str = "feature.py", content: str = "x = 1\n") -> str:
    """在侧分支（模拟 worktree）上做一个 commit，回到原分支，返回其 hash。"""
    orig = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", "-b", "wt")
    (repo / fname).write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"feat: {fname}"], cwd=repo, check=True)
    wc = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", orig)
    return wc


def _manifest_for(tmp_path: Path, repo: Path, wc: str, files: list[str]) -> str:
    mf = tmp_path / "manifest.json"
    mf.write_text(
        json.dumps(
            {
                "cid": "add-foo",
                "commit": wc,
                "files_written": [{"path": str(repo / f)} for f in files],
            }
        )
    )
    return str(mf)


def _result_line(wc: str, summary: Path) -> str:
    return f"RESULT: commit={wc} tasks=3 tests=pass summary={summary} notes=-"


@pytest.fixture
def summary_file(tmp_path: Path) -> Path:
    f = tmp_path / "implement.summary.md"
    f.write_text("# done\n")
    return f


# ============================================================
# 纯函数
# ============================================================


def test_translate_result_word_boundary():
    line = "RESULT: commit=abc tasks=1 tests=pass summary=/s notes=commit=abc123"
    out = _integrate._translate_result(line, "abc", "def")
    assert "commit=def tasks=1" in out
    assert "commit=abc123" in out  # 前缀相同的长 hash 不被误伤


# ============================================================
# 主流程
# ============================================================


def test_integrate_happy_path(env_setup, fake_repo, make_args, capsys, tmp_path, summary_file):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    # main 前进一格（真实场景：前序 change 已整合），否则 cherry-pick 同父同树
    # 同秒会产出与 wc 相同的 commit 对象，测不出 hash 翻译
    (fake_repo / "other.txt").write_text("main moved\n")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "chore: main moved"], cwd=fake_repo, check=True)
    # manifest 指向 repo 外的稳定文件（summary_file）模拟 worktree 绝对路径
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest)
    assert out["ok"] is True, out
    assert out["worktree_commit"] == wc
    assert out["integrated_commit"] == _git(fake_repo, "rev-parse", "HEAD")
    assert out["integrated_commit"] != wc
    assert out["verify_tests"] == "skipped"  # fake repo 无测试清单
    # state 装订：implement_commit 是整合后 hash（不是 worktree hash）
    entry = _state.read_state(p.state_json)["progress"][0]
    assert entry["implement_commit"] == out["integrated_commit"]
    assert entry["status"] == "reviewing"
    # main 上文件真实存在
    assert (fake_repo / "feature.py").is_file()


def test_integrate_rejects_plan_only(env_setup, make_args, capsys, tmp_path):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    out = _integrate.run_integrate(
        p, 1, "RESULT: commit=- tasks=0 tests=fail summary=- notes=nothing", None
    )
    assert out["ok"] is False
    assert out["step"] == "verify-manifest"


def test_integrate_manifest_file_missing(env_setup, fake_repo, make_args, capsys, tmp_path, summary_file):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    mf = tmp_path / "manifest.json"
    mf.write_text(json.dumps({"files_written": [{"path": str(tmp_path / "ghost.py")}]}))

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), str(mf))
    assert out["ok"] is False
    assert out["step"] == "verify-manifest"
    assert out["reason"] == "files_missing"
    # 未走到 cherry-pick：main 不含 feature.py
    assert not (fake_repo / "feature.py").exists()


def test_integrate_cherry_pick_conflict_aborts_clean(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo, fname="README.md", content="conflict-side\n")
    # main 上同文件另一份改动 → cherry-pick 必冲突
    (fake_repo / "README.md").write_text("conflict-main\n")
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "main change"], cwd=fake_repo, check=True)
    head_before = _git(fake_repo, "rev-parse", "HEAD")
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest)
    assert out["ok"] is False
    assert out["step"] == "cherry-pick"
    # 现场收拾干净：HEAD 未动、工作树 clean
    assert _git(fake_repo, "rev-parse", "HEAD") == head_before
    assert _git(fake_repo, "status", "--porcelain") == ""


def test_integrate_verify_tests_failure_reverts(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])
    # 显式配置测试命令；注入 runner 让它必败（git 命令照常真实执行）。
    # 配置文件先入库，保证 revert 后工作树 clean 断言成立。
    (fake_repo / ".npc").mkdir()
    (fake_repo / ".npc" / "config.toml").write_text('[verify]\ntest = "fake-test"\n')
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "chore: npc config"], cwd=fake_repo, check=True)

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k != "shell"})
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="tests exploded")

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    assert out["ok"] is False, out
    assert out["step"] == "verify-tests"
    assert out["reverted"] is not None
    # revert 后 main 上 feature.py 已被摘除，工作树 clean
    assert not (fake_repo / "feature.py").exists()
    assert _git(fake_repo, "status", "--porcelain") == ""
    entry = _state.read_state(p.state_json)["progress"][0]
    assert entry["status"] == "failed"
    assert entry["reason"] == "verify-tests-failed"
