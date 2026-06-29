"""finalize-ff-merge-teardown 测试。

覆盖 Tasks 3.1–3.6：
  3.1 FF 干净：合成功 + 拆树 + 删分支（临时仓库真实 worktree 周期）
  3.2 base 分叉：保留 + merged_back=false + reason
  3.3 非 completed：保留全部
  3.4 --no-worktree：跳过
  3.5 幂等：worktree 已删时不报错
  3.6 pytest 全绿（文件整体通过即代表 3.6）

git_ops 三个新封装（Task 1.1 / 1.2）也通过 mock runner 单测。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from npc import git_ops as _git
from npc import paths as _paths
from npc import state as _state


# ============================================================
# 辅助：真实 git 操作
# ============================================================


def _git_run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _setup_canonical_with_worktree(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """搭建 canonical repo + worktree。

    返回 (canonical_root, worktree_root, base_branch, spine_branch)。
    canonical 已有一个 init commit on base_branch；
    spine 分支在 worktree 中再加一个 commit。
    """
    canonical = tmp_path / "canonical"
    canonical.mkdir()

    _git_run(canonical, "init", "-q")
    _git_run(canonical, "config", "user.email", "t@t.t")
    _git_run(canonical, "config", "user.name", "T")
    (canonical / "file.txt").write_text("init\n")
    _git_run(canonical, "add", ".")
    _git_run(canonical, "commit", "-q", "-m", "init")

    # 获取当前分支名（可能是 main 或 master）
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=canonical, capture_output=True, text=True, check=True,
    )
    base_branch = result.stdout.strip()

    spine_branch = "spine/2026-06-26-1234"
    worktree_path = tmp_path / "worktree"

    # 在 canonical 上建 worktree
    _git_run(canonical, "worktree", "add", "-b", spine_branch, str(worktree_path))

    # 在 worktree 中加一个提交
    _git_run(worktree_path, "config", "user.email", "t@t.t")
    _git_run(worktree_path, "config", "user.name", "T")
    (worktree_path / "work.txt").write_text("work done\n")
    _git_run(worktree_path, "add", ".")
    _git_run(worktree_path, "commit", "-q", "-m", "coder work")

    return canonical, worktree_path, base_branch, spine_branch


def _make_state_json(paths: _paths.Paths, status: str = "completed") -> None:
    """写一个满足 finalize 需要的 state.json（所有 progress 已 archived）。"""
    state = {
        "schema_version": 2,
        "run_ts": paths.run_ts,
        "started_at": "2026-06-26T10:00:00+08:00",
        "last_updated_at": "2026-06-26T10:00:00+08:00",
        "mode": "worktree",
        "fresh": False,
        "status": "in-progress",
        "project_root": str(paths.repo_root),
        "proj_key": paths.proj_key,
        "git_head_at_start": "abc1234",
        "cc_session": {"session_id": None, "transcript_path": None, "source": "test"},
        "plan_order": ["change-a"],
        "progress": [
            {
                "seq": 1,
                "change_id": "change-a",
                "status": "archived",
                "blocking_trend": [],
                "categories_seen": [],
                "rounds_since_strict_decrease": 0,
                "phases": {},
            }
        ],
    }
    if status == "completed-with-issues":
        # 把第一个改成 failed，这样结果是 completed-with-issues
        state["progress"][0]["status"] = "failed"

    paths.task_log_dir.mkdir(parents=True, exist_ok=True)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    _state.write_state(paths.state_json, paths.state_md, state)


def _build_worktree_paths(
    canonical: Path,
    worktree: Path,
    base_branch: str,
    spine_branch: str,
    home: Path,
) -> _paths.Paths:
    """构造 worktree 模式的 Paths 对象（模拟 npc init --worktree 后的 run.json）。"""
    run_ts = "2026-06-26-1234-test"
    canonical_proj_key = _paths.proj_key_for(canonical)
    worktree_proj_key = _paths.proj_key_for(worktree)
    task_log_dir = home / "task_log" / worktree_proj_key
    run_dir = task_log_dir / run_ts
    state_json = task_log_dir / f"{run_ts}-plan-state.json"
    state_md = task_log_dir / f"{run_ts}-plan-state.md"
    index_file = task_log_dir / "index.jsonl"
    schema_path = home / "task_log" / _paths.SCHEMA_FILENAME
    run_events = run_dir / "run.events.jsonl"

    p = _paths.Paths(
        repo_root=worktree,
        proj_key=worktree_proj_key,
        task_log_dir=task_log_dir,
        run_ts=run_ts,
        run_dir=run_dir,
        state_json=state_json,
        state_md=state_md,
        index_file=index_file,
        schema_path=schema_path,
        run_events=run_events,
        canonical_repo_root=canonical,
        canonical_proj_key=canonical_proj_key,
        base_branch=base_branch,
        spine_branch=spine_branch,
    )
    task_log_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_events.touch(exist_ok=True)
    return p


# ============================================================
# Task 1.1：merge_ff_only 单元测试（mock runner）
# ============================================================


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


class ScriptedRunner:
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


def test_merge_ff_only_success():
    """merge_ff_only：runner 均返回 0 → 返回 (True, '')。"""
    runner = ScriptedRunner(default_rc=0)
    ok, reason = _git.merge_ff_only(
        Path("/fake"), "main", "spine/ts", runner
    )
    assert ok is True
    assert reason == ""
    cmds = [" ".join(c) for c in runner.calls]
    assert any("checkout" in c and "main" in c for c in cmds)
    assert any("merge" in c and "--ff-only" in c and "spine/ts" in c for c in cmds)


def test_merge_ff_only_checkout_fail():
    """merge_ff_only：checkout 失败 → 返回 (False, reason)，不尝试 merge。"""
    runner = ScriptedRunner(
        rules=[
            (lambda c: "checkout" in c, lambda c: _completed(c, 1, stderr="err")),
        ]
    )
    ok, reason = _git.merge_ff_only(Path("/fake"), "main", "spine/ts", runner)
    assert ok is False
    assert "切换到" in reason or "失败" in reason
    # 不应调用 merge
    assert not any("merge" in " ".join(c) for c in runner.calls)


def test_merge_ff_only_merge_fail():
    """merge_ff_only：merge 失败（已分叉）→ 返回 (False, reason)。"""
    runner = ScriptedRunner(
        rules=[
            (lambda c: "checkout" in c, lambda c: _completed(c, 0)),
            (lambda c: "merge" in c, lambda c: _completed(c, 1, stderr="Not possible to fast-forward")),
        ]
    )
    ok, reason = _git.merge_ff_only(Path("/fake"), "main", "spine/ts", runner)
    assert ok is False
    assert reason != ""


# ============================================================
# Task 1.2：worktree_remove / branch_delete 单元测试
# ============================================================


def test_worktree_remove_success(tmp_path: Path):
    """worktree_remove：worktree 路径存在，runner 返回 0 → 成功。"""
    wt = tmp_path / "wt"
    wt.mkdir()
    runner = ScriptedRunner(default_rc=0)
    ok, reason = _git.worktree_remove(Path("/canonical"), wt, runner)
    assert ok is True
    assert reason == ""
    assert any("worktree" in " ".join(c) and "remove" in " ".join(c) for c in runner.calls)


def test_worktree_remove_already_gone(tmp_path: Path):
    """worktree_remove：worktree 路径不存在 → 幂等成功，不调用 git。"""
    wt = tmp_path / "nonexistent"
    runner = ScriptedRunner(default_rc=0)
    ok, reason = _git.worktree_remove(Path("/canonical"), wt, runner)
    assert ok is True
    assert runner.calls == []


def test_worktree_remove_fail(tmp_path: Path):
    """worktree_remove：runner 失败 → 返回 (False, reason)。"""
    wt = tmp_path / "wt"
    wt.mkdir()
    runner = ScriptedRunner(default_rc=1)
    ok, reason = _git.worktree_remove(Path("/canonical"), wt, runner)
    assert ok is False
    assert reason != ""


def test_branch_delete_success():
    """branch_delete：分支存在，runner 均返回 0 → 成功。"""
    runner = ScriptedRunner(default_rc=0)
    ok, reason = _git.branch_delete(Path("/canonical"), "spine/ts", runner)
    assert ok is True
    assert reason == ""


def test_branch_delete_already_gone():
    """branch_delete：rev-parse 失败（分支不存在）→ 幂等成功。"""
    runner = ScriptedRunner(
        rules=[
            (lambda c: "rev-parse" in c, lambda c: _completed(c, 1)),
        ]
    )
    ok, reason = _git.branch_delete(Path("/canonical"), "spine/ts", runner)
    assert ok is True
    # 不应调用 branch -d
    assert not any("branch" in " ".join(c) and "-d" in " ".join(c) for c in runner.calls)


def test_branch_delete_fail():
    """branch_delete：分支存在但 -d 失败 → 返回 (False, reason)。"""
    runner = ScriptedRunner(
        rules=[
            (lambda c: "rev-parse" in c, lambda c: _completed(c, 0)),
            (lambda c: "branch" in c and "-d" in c, lambda c: _completed(c, 1, stderr="not fully merged")),
        ]
    )
    ok, reason = _git.branch_delete(Path("/canonical"), "spine/ts", runner)
    assert ok is False
    assert reason != ""


# ============================================================
# Task 3.1：FF 干净路径（真实 git worktree）
# ============================================================


def test_finalize_ff_clean_path(tmp_path: Path, monkeypatch, capsys):
    """3.1：status=completed + FF 可行 → merged_back=True, worktree_removed=True."""
    canonical, worktree, base_branch, spine_branch = _setup_canonical_with_worktree(tmp_path)
    home = tmp_path / "home"

    p = _build_worktree_paths(canonical, worktree, base_branch, spine_branch, home)
    _make_state_json(p, status="completed")

    args = argparse.Namespace(
        state_json=str(p.state_json),
        run_ts=None,
        task_log_dir=None,
    )

    # patch load_paths 返回我们的 Paths
    monkeypatch.setattr(_paths, "load_paths", lambda a: p)

    _state.finalize(args)

    captured = capsys.readouterr()
    output_lines = [line for line in captured.out.splitlines() if line.strip()]
    # 解析最后一行 JSON 输出
    assert output_lines, "没有任何输出"
    last = json.loads(output_lines[-1])

    assert last["ok"] is True
    assert last["final_status"] == "completed"
    assert last["merged_back"] is True
    assert last.get("worktree_removed") is True

    # worktree 目录已被移除
    assert not worktree.exists()

    # spine 分支已被删除
    check = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{spine_branch}"],
        cwd=canonical, capture_output=True, text=True,
    )
    assert check.returncode != 0, "spine 分支应该已被删除"

    # task_log 记账保留
    assert p.state_json.exists()


# ============================================================
# Task 3.2：base 已分叉
# ============================================================


def test_finalize_ff_diverged(tmp_path: Path, monkeypatch, capsys):
    """3.2：base 在 run 期间前进 → merged_back=False, worktree + branch 保留。"""
    canonical, worktree, base_branch, spine_branch = _setup_canonical_with_worktree(tmp_path)
    home = tmp_path / "home"

    # 在 canonical base_branch 上加一个新提交（制造分叉）
    _git_run(canonical, "checkout", base_branch)
    (canonical / "extra.txt").write_text("diverged\n")
    _git_run(canonical, "add", ".")
    _git_run(canonical, "commit", "-q", "-m", "diverge canonical")

    p = _build_worktree_paths(canonical, worktree, base_branch, spine_branch, home)
    _make_state_json(p, status="completed")

    args = argparse.Namespace(state_json=str(p.state_json), run_ts=None, task_log_dir=None)

    monkeypatch.setattr(_paths, "load_paths", lambda a: p)

    _state.finalize(args)

    captured = capsys.readouterr()
    output_lines = [line for line in captured.out.splitlines() if line.strip()]
    last = json.loads(output_lines[-1])
    assert last["ok"] is True
    assert last["merged_back"] is False
    assert "reason" in last
    assert last.get("spine_branch") == spine_branch

    # worktree 保留
    assert worktree.exists()
    # spine 分支保留
    check = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{spine_branch}"],
        cwd=canonical, capture_output=True, text=True,
    )
    assert check.returncode == 0, "spine 分支应该保留"


# ============================================================
# Task 3.3：非 completed 状态保留全部
# ============================================================


def test_finalize_non_completed_no_merge(tmp_path: Path, monkeypatch, capsys):
    """3.3：status=completed-with-issues → 不触发合并逻辑，worktree 保留。"""
    canonical, worktree, base_branch, spine_branch = _setup_canonical_with_worktree(tmp_path)
    home = tmp_path / "home"

    p = _build_worktree_paths(canonical, worktree, base_branch, spine_branch, home)
    _make_state_json(p, status="completed-with-issues")

    args = argparse.Namespace(state_json=str(p.state_json), run_ts=None, task_log_dir=None)

    monkeypatch.setattr(_paths, "load_paths", lambda a: p)

    _state.finalize(args)

    captured = capsys.readouterr()
    output_lines = [line for line in captured.out.splitlines() if line.strip()]
    last = json.loads(output_lines[-1])
    assert last["ok"] is True
    assert last["final_status"] == "completed-with-issues"
    # 没有 merged_back 字段（未触发合并逻辑）
    assert "merged_back" not in last

    # worktree 保留
    assert worktree.exists()


# ============================================================
# Task 3.4：--no-worktree（无 spine_branch）跳过
# ============================================================


def test_finalize_no_worktree_skips_merge(tmp_path: Path, monkeypatch, capsys):
    """3.4：非 worktree 模式（spine_branch=None）→ 跳过合并逻辑。"""
    # 用普通仓库（非 worktree 模式）
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_run(repo, "init", "-q")
    _git_run(repo, "config", "user.email", "t@t.t")
    _git_run(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("x\n")
    _git_run(repo, "add", ".")
    _git_run(repo, "commit", "-q", "-m", "init")

    home = tmp_path / "home"
    p = _paths.compute_paths(repo, run_ts="2026-06-26-test", home=home)
    _paths.ensure_dirs(p)
    assert p.spine_branch is None  # 确认非 worktree 模式

    _make_state_json(p, status="completed")

    args = argparse.Namespace(state_json=str(p.state_json), run_ts=None, task_log_dir=None)

    monkeypatch.setattr(_paths, "load_paths", lambda a: p)

    _state.finalize(args)

    captured = capsys.readouterr()
    output_lines = [line for line in captured.out.splitlines() if line.strip()]
    last = json.loads(output_lines[-1])
    assert last["ok"] is True
    assert last["final_status"] == "completed"
    # 没有 merged_back（跳过了合并逻辑）
    assert "merged_back" not in last


# ============================================================
# Task 3.5：幂等 —— worktree 已删时不报错
# ============================================================


def test_finalize_idempotent_worktree_already_removed(tmp_path: Path, monkeypatch, capsys):
    """3.5：worktree 路径已被手动删（但 spine 分支仍存在）→ 幂等成功，不报错。

    具体场景：worktree 目录被意外删除，finalize 仍能完成 ff-merge 并安全处理
    worktree_remove（路径不存在时幂等）。
    """
    import shutil

    canonical, worktree, base_branch, spine_branch = _setup_canonical_with_worktree(tmp_path)
    home = tmp_path / "home"

    # 手动删掉 worktree 目录（但保留 spine 分支）
    shutil.rmtree(worktree, ignore_errors=True)
    # prune 清理悬空引用（让 canonical 知道 worktree 已不存在）
    subprocess.run(["git", "worktree", "prune"], cwd=canonical, capture_output=True)

    p = _build_worktree_paths(canonical, worktree, base_branch, spine_branch, home)
    _make_state_json(p, status="completed")

    args = argparse.Namespace(state_json=str(p.state_json), run_ts=None, task_log_dir=None)

    monkeypatch.setattr(_paths, "load_paths", lambda a: p)

    # 不应抛异常
    _state.finalize(args)

    captured = capsys.readouterr()
    output_lines = [line for line in captured.out.splitlines() if line.strip()]
    last = json.loads(output_lines[-1])
    assert last["ok"] is True
    # ff-merge 成功（spine 分支仍存在，base 可 ff）
    assert last.get("merged_back") is True
    # worktree 路径已不存在 → worktree_remove 幂等返回 True
    assert last.get("worktree_removed") is True


# ============================================================
# Task 3.6 覆盖：git_ops 新函数通过 mock 全路径
# ============================================================


def test_git_ops_all_new_functions_covered():
    """确认三个新函数（merge_ff_only / worktree_remove / branch_delete）均存在且可调用。"""
    assert callable(_git.merge_ff_only)
    assert callable(_git.worktree_remove)
    assert callable(_git.branch_delete)
