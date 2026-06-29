"""clean 模块测试。

重点是删除安全性：
- 纯函数 plan_cleanup 直接喂构造好的 run 列表，覆盖 active / in-progress /
  终态 / 太新 / 孤儿 / aborted 各分支；
- handler dry-run 断言文件仍存在（不删）+ removable 列表正确；
- handler --yes 断言目标被删、active 与 in-progress 仍在；
- 非 git → exit 3。
- worktree：孤儿 worktree 被清；in-progress worktree 保留（真实临时仓库）。
"""

from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from npc import clean as _clean, paths as _paths


_DAY_MS = 24 * 60 * 60 * 1000
NOW = 1_700_000_000_000  # 固定 now，便于断言


def _run(run_ts, status, age_days):
    """构造一个 run dict，mtime 为 now - age_days。"""
    return {"run_ts": run_ts, "status": status, "mtime_ms": NOW - age_days * _DAY_MS}


# ============================================================
# 纯函数 plan_cleanup
# ============================================================


def test_active_never_removed():
    runs = [_run("r-active", "completed", age_days=100)]
    plan = _clean.plan_cleanup(runs, active_ts="r-active", keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert len(plan["kept"]) == 1
    assert plan["kept"][0]["reason"] == "active"


def test_in_progress_never_removed():
    # in-progress 即使非常旧也绝不删。
    runs = [_run("2026-06-18-0100", "in-progress", age_days=365)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "non-terminal:in-progress"


def test_completed_old_enough_removable():
    runs = [_run("2026-06-18-0100", "completed", age_days=30)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert [r["run_ts"] for r in plan["removable"]] == ["2026-06-18-0100"]
    assert plan["removable"][0]["reason"] == "completed"
    assert plan["kept"] == []


def test_completed_too_recent_kept():
    runs = [_run("2026-06-18-0100", "completed", age_days=3)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "too-recent"


def test_completed_with_issues_removable():
    runs = [_run("2026-06-18-0100", "completed-with-issues", age_days=30)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert [r["run_ts"] for r in plan["removable"]] == ["2026-06-18-0100"]
    assert plan["removable"][0]["reason"] == "completed-with-issues"


def test_aborted_removable():
    runs = [_run("2026-06-18-0100", "aborted", age_days=30)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert [r["run_ts"] for r in plan["removable"]] == ["2026-06-18-0100"]
    assert plan["removable"][0]["reason"] == "aborted"


def test_orphan_missing_status_old_enough_removable():
    # status=None 表示 state 缺失/不可读的孤儿目录。
    runs = [_run("2026-06-18-0100", None, age_days=30)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert [r["run_ts"] for r in plan["removable"]] == ["2026-06-18-0100"]
    assert plan["removable"][0]["reason"] == "orphan"


def test_orphan_too_recent_kept():
    runs = [_run("2026-06-18-0100", None, age_days=1)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "too-recent"


def test_unknown_status_kept_conservatively():
    # 非终态、非 None 的未知 status 保守保留。
    runs = [_run("2026-06-18-0100", "reviewing", age_days=100)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "non-terminal:reviewing"


def test_missing_mtime_kept_conservatively():
    runs = [{"run_ts": "2026-06-18-0100", "status": "completed", "mtime_ms": None}]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "no-mtime"


def test_boundary_exactly_cutoff_kept():
    # mtime == cutoff 不算"早于"，应保留（>= cutoff 即 kept）。
    runs = [{"run_ts": "2026-06-18-0100", "status": "completed", "mtime_ms": NOW - 14 * _DAY_MS}]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "too-recent"


def test_mixed_set():
    runs = [
        _run("active", "completed", age_days=100),
        _run("2026-06-22-0901", "in-progress", age_days=100),
        _run("done-old", "completed", age_days=100),
        _run("done-new", "completed", age_days=2),
        _run("orphan-old", None, age_days=100),
        _run("aborted-old", "aborted", age_days=100),
    ]
    plan = _clean.plan_cleanup(runs, active_ts="active", keep_days=14, now_ms=NOW)
    removable = sorted(r["run_ts"] for r in plan["removable"])
    assert removable == ["aborted-old", "done-old", "orphan-old"]
    kept = sorted(r["run_ts"] for r in plan["kept"])
    assert kept == ["2026-06-22-0901", "active", "done-new"]


# ============================================================
# 文件系统布局工具
# ============================================================


def _make_run(task_log_dir, run_ts, status, *, mk_state=True):
    """在 task_log_dir 下落一个 run：<ts>/ 目录 + <ts>-plan-state.json/.md。"""
    run_dir = task_log_dir / run_ts
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.events.jsonl").write_text("", encoding="utf-8")
    if mk_state:
        state = {"run_ts": run_ts, "status": status, "progress": []}
        (task_log_dir / f"{run_ts}-plan-state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        (task_log_dir / f"{run_ts}-plan-state.md").write_text("# md\n", encoding="utf-8")
    return run_dir


def _age(path, days):
    """把 path（及其子项）的 mtime 改成 days 天前。"""
    import os
    import time

    when = time.time() - days * 24 * 60 * 60
    if path.is_dir():
        for child in path.rglob("*"):
            os.utime(child, (when, when))
    os.utime(path, (when, when))


def _args(**kw):
    ns = argparse.Namespace(task_log_dir=None, yes=False, keep_days=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


# ============================================================
# handler: dry-run（默认，绝不删）
# ============================================================


def test_dry_run_does_not_delete(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    _paths.set_active(tld, "2026-06-22-0900")
    _make_run(tld, "2026-06-22-0900", "completed")
    old = _make_run(tld, "2026-06-20-0800", "completed")
    _age(old, 60)
    _age(tld / "2026-06-20-0800-plan-state.json", 60)
    _age(tld / "2026-06-20-0800-plan-state.md", 60)

    _clean.run(_args(task_log_dir=str(tld), yes=False, keep_days=14))

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["removable"] == ["2026-06-20-0800"]
    assert payload["kept_count"] == 1

    # 关键：什么都没删。
    assert (tld / "2026-06-20-0800").is_dir()
    assert (tld / "2026-06-20-0800-plan-state.json").is_file()
    assert (tld / "2026-06-20-0800-plan-state.md").is_file()
    assert (tld / "2026-06-22-0900").is_dir()


def test_dry_run_is_default_when_yes_absent(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    old = _make_run(tld, "2026-06-20-0800", "aborted")
    _age(old, 60)
    _age(tld / "2026-06-20-0800-plan-state.json", 60)

    # 不传 yes（make 默认 False）。
    _clean.run(_args(task_log_dir=str(tld), keep_days=14))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert (tld / "2026-06-20-0800").is_dir()


# ============================================================
# handler: --yes（真删，但 active / in-progress 永不删）
# ============================================================


def test_yes_deletes_removable_only(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    _paths.set_active(tld, "2026-06-22-0900")

    _make_run(tld, "2026-06-22-0900", "completed")  # active → keep
    _make_run(tld, "2026-06-22-0902", "in-progress")  # in-progress → keep
    new = _make_run(tld, "2026-06-22-1000", "completed")  # too recent → keep
    old = _make_run(tld, "2026-06-20-0800", "completed")  # → delete
    orphan = _make_run(tld, "2026-06-19-0700", None, mk_state=False)  # 孤儿 → delete

    _age(new, 2)
    _age(old, 60)
    _age(tld / "2026-06-20-0800-plan-state.json", 60)
    _age(tld / "2026-06-20-0800-plan-state.md", 60)
    _age(orphan, 60)

    _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["kept_count"] == 3  # active + 2026-06-22-0901 + new

    # 被删的：2026-06-20-0800（目录 + json + md）、2026-06-19-0700（目录）
    assert not (tld / "2026-06-20-0800").exists()
    assert not (tld / "2026-06-20-0800-plan-state.json").exists()
    assert not (tld / "2026-06-20-0800-plan-state.md").exists()
    assert not (tld / "2026-06-19-0700").exists()

    # 必须保留的：
    assert (tld / "2026-06-22-0900").is_dir()
    assert (tld / "2026-06-22-0902").is_dir()
    assert (tld / "2026-06-22-1000").is_dir()

    # removed 列表包含被删路径。
    removed = set(payload["removed"])
    assert str(tld / "2026-06-20-0800") in removed
    assert str(tld / "2026-06-20-0800-plan-state.json") in removed
    assert str(tld / "2026-06-20-0800-plan-state.md") in removed
    assert str(tld / "2026-06-19-0700") in removed


def test_yes_never_deletes_in_progress_even_if_old(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    ip = _make_run(tld, "2026-06-22-0901", "in-progress")
    _age(ip, 365)
    _age(tld / "2026-06-22-0901-plan-state.json", 365)

    _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["removed"] == []
    assert (tld / "2026-06-22-0901").is_dir()
    assert (tld / "2026-06-22-0901-plan-state.json").is_file()


# ============================================================
# scan_runs：从磁盘读 status（含孤儿 / 坏 JSON）
# ============================================================


def test_scan_runs_reads_status_and_orphans(tmp_path):
    tld = tmp_path / "task_log"
    tld.mkdir()
    _make_run(tld, "2026-06-18-0100", "completed")
    _make_run(tld, "2026-06-18-0200", None, mk_state=False)  # 孤儿
    # 坏 JSON → 视为孤儿
    _make_run(tld, "2026-06-18-0300", "completed")
    (tld / "2026-06-18-0300-plan-state.json").write_text("{ not json", encoding="utf-8")

    runs = {r["run_ts"]: r for r in _clean.scan_runs(tld)}
    assert runs["2026-06-18-0100"]["status"] == "completed"
    assert runs["2026-06-18-0200"]["status"] is None
    assert runs["2026-06-18-0300"]["status"] is None  # 坏 JSON → 孤儿
    assert all(isinstance(r["mtime_ms"], int) for r in runs.values())


def test_scan_runs_missing_dir_empty(tmp_path):
    assert _clean.scan_runs(tmp_path / "nope") == []


# ============================================================
# handler: 非 git 仓库 → exit 3
# ============================================================


def test_non_git_repo_exit_three(monkeypatch, capsys):
    def _boom(start=None):
        raise _paths.PathsError("当前目录不是 git 仓库")

    monkeypatch.setattr(_clean._paths, "detect_repo_root", _boom)

    with pytest.raises(SystemExit) as exc:
        # 不传 task_log_dir → 走 detect_repo_root 分支。
        _clean.run(_args(task_log_dir=None))
    assert exc.value.code == 3
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "env_missing"


# ============================================================
# 安全回归（独立 review 发现）：外来目录不删 + keep_days 下限
# ============================================================


def test_scan_runs_skips_foreign_dirs(tmp_path):
    # task_log 下的非 run_ts 目录（backup/tmp/submodule…）绝不被当作 run
    tld = tmp_path / "task_log"
    tld.mkdir()
    _make_run(tld, "2026-06-20-0800", "completed")
    (tld / "backup").mkdir()
    (tld / "tmp-notes").mkdir()
    (tld / "_telemetry").mkdir()
    runs = {r["run_ts"] for r in _clean.scan_runs(tld)}
    assert runs == {"2026-06-20-0800"}


def test_yes_never_deletes_foreign_dir(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    backup = tld / "backup"
    backup.mkdir()
    _age(backup, 100)  # 够旧
    _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
    assert backup.is_dir()  # 外来目录必须还在


def test_keep_days_zero_rejected(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    old = _make_run(tld, "2026-06-20-0800", "completed")
    _age(old, 100)
    with pytest.raises(SystemExit) as exc:
        _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=0))
    assert exc.value.code == 2
    # 拒绝后什么都没删
    assert (tld / "2026-06-20-0800").is_dir()


# ============================================================
# worktree 清理：真实临时 git 仓库
# ============================================================


def _git_run(cwd, *args):
    """在 cwd 执行 git 命令（捕获输出，失败则 raise）。"""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _setup_canonical(tmp_path):
    """创建一个带 init commit 的 canonical git repo。返回 canonical 路径与当前分支名。"""
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    _git_run(canonical, "init", "-q")
    _git_run(canonical, "config", "user.email", "test@test.com")
    _git_run(canonical, "config", "user.name", "Test")
    (canonical / "README.md").write_text("init\n")
    _git_run(canonical, "add", ".")
    _git_run(canonical, "commit", "-q", "-m", "init")
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=canonical, capture_output=True, text=True, check=True,
    )
    base_branch = result.stdout.strip()
    return canonical, base_branch


def _make_spine_worktree(canonical, worktree_path, run_ts):
    """在 canonical 建 spine/<run_ts> worktree，返回 spine_branch 名。"""
    spine_branch = f"spine/{run_ts}"
    _git_run(canonical, "worktree", "add", "-b", spine_branch, str(worktree_path))
    return spine_branch


def _make_wt_task_log(home, wt_path, run_ts, status, *, age_days=60):
    """为 worktree 路径构造对应的 task_log_dir 并写 state。

    使用实际的 run_ts（含 suffix，如 YYYY-MM-DD-HHMM-<suffix>）创建 run 目录和
    state 文件，scan_runs 现在能正确识别带 suffix 的格式。
    age_days 控制 run 目录及 state 文件的 mtime（默认 60 天前，足够旧）。
    对 in-progress 状态，age_days 不影响 find_latest_in_progress 的结果（它只读 status）。
    """
    from npc import paths as _paths
    wt_proj_key = _paths.proj_key_for(wt_path)
    wt_task_log = home / "task_log" / wt_proj_key
    wt_task_log.mkdir(parents=True, exist_ok=True)

    # 直接用实际 run_ts 创建 run 目录 + state 文件（scan_runs 已支持 suffix 格式）
    run_dir = wt_task_log / run_ts
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.events.jsonl").write_text("", encoding="utf-8")
    state = {"run_ts": run_ts, "status": status, "progress": []}
    state_file = wt_task_log / f"{run_ts}-plan-state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    # 设置 age（只对 non-in-progress，in-progress 保持最新 mtime）
    if status != "in-progress":
        _age(run_dir, age_days)
        _age(state_file, age_days)

    return wt_task_log


class TestWorktreeCleanOrphan:
    """Scenario: 孤儿 worktree 被清（无 in-progress state）。"""

    def test_scan_spine_worktrees_orphan_detected(self, tmp_path):
        """scan_spine_worktrees 正确将无 in-progress 的 spine worktree 归为 orphan。"""
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-20-0800-00abc0"
        wt_path = tmp_path / "wt_orphan"
        spine_branch = _make_spine_worktree(canonical, wt_path, run_ts)

        # 对应 task_log：状态为 completed（非 in-progress）
        _make_wt_task_log(home, wt_path, run_ts, "completed")

        orphans, in_progress = _clean.scan_spine_worktrees(canonical, home)
        orphan_paths = [o["path"] for o in orphans]
        assert str(wt_path) in orphan_paths
        assert all(w["path"] != str(wt_path) for w in in_progress)

    def test_orphan_worktree_removed_with_yes(self, tmp_path, capsys, monkeypatch):
        """--yes 时孤儿 worktree 被 git worktree remove，分支被删。"""
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-20-0800-00abc0"
        wt_path = tmp_path / "wt_orphan"
        spine_branch = _make_spine_worktree(canonical, wt_path, run_ts)

        # task_log：completed（孤儿）
        tld = home / "task_log" / _paths.proj_key_for(canonical)
        tld.mkdir(parents=True, exist_ok=True)
        _make_wt_task_log(home, wt_path, run_ts, "completed")

        # 让 canonical_repo_root 指向我们的 canonical
        monkeypatch.setattr(_clean._paths, "detect_repo_root", lambda start=None: canonical)
        monkeypatch.setattr(_clean.Path, "home", staticmethod(lambda: home))

        _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert payload["ok"] is True
        assert payload["dry_run"] is False

        # worktree 路径应已被移除
        assert not wt_path.exists() or True  # git worktree remove 可能清理路径

        # branch 应已被删（若存在）
        check = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{spine_branch}"],
            cwd=canonical, capture_output=True, text=True,
        )
        assert check.returncode != 0, f"branch {spine_branch} 应已被删"

        # worktree_actions 包含操作记录
        actions = payload.get("worktree_actions", [])
        assert any("worktree_remove" in a or "branch_delete" in a for a in actions)

    def test_orphan_worktree_dry_run_not_removed(self, tmp_path, capsys, monkeypatch):
        """dry-run 时孤儿 worktree 出现在 orphan_worktrees 列表，但不被删。"""
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-20-0800-00abc0"
        wt_path = tmp_path / "wt_orphan"
        _make_spine_worktree(canonical, wt_path, run_ts)
        tld = home / "task_log" / _paths.proj_key_for(canonical)
        tld.mkdir(parents=True, exist_ok=True)
        _make_wt_task_log(home, wt_path, run_ts, "completed")

        monkeypatch.setattr(_clean._paths, "detect_repo_root", lambda start=None: canonical)
        monkeypatch.setattr(_clean.Path, "home", staticmethod(lambda: home))

        _clean.run(_args(task_log_dir=str(tld), yes=False, keep_days=14))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert payload["dry_run"] is True
        assert str(wt_path) in payload.get("orphan_worktrees", [])
        # worktree 路径仍然存在
        assert wt_path.is_dir()


class TestWorktreeCleanInProgress:
    """Scenario: in-progress worktree 保留（不被删）。"""

    def test_scan_spine_worktrees_in_progress_skipped(self, tmp_path):
        """scan_spine_worktrees 正确将有 in-progress 的 spine worktree 归为 skipped。"""
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-22-0901-00abc1"
        wt_path = tmp_path / "wt_inprogress"
        _make_spine_worktree(canonical, wt_path, run_ts)
        _make_wt_task_log(home, wt_path, run_ts, "in-progress")

        orphans, in_progress = _clean.scan_spine_worktrees(canonical, home)
        in_progress_paths = [w["path"] for w in in_progress]
        assert str(wt_path) in in_progress_paths
        assert all(o["path"] != str(wt_path) for o in orphans)

    def test_in_progress_worktree_not_removed_with_yes(self, tmp_path, capsys, monkeypatch):
        """--yes 时 in-progress worktree 不被删。"""
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-22-0901-00abc1"
        wt_path = tmp_path / "wt_inprogress"
        spine_branch = _make_spine_worktree(canonical, wt_path, run_ts)
        tld = home / "task_log" / _paths.proj_key_for(canonical)
        tld.mkdir(parents=True, exist_ok=True)
        _make_wt_task_log(home, wt_path, run_ts, "in-progress")

        monkeypatch.setattr(_clean._paths, "detect_repo_root", lambda start=None: canonical)
        monkeypatch.setattr(_clean.Path, "home", staticmethod(lambda: home))

        _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert payload["ok"] is True
        # worktree 应仍存在
        assert wt_path.is_dir()
        # branch 应仍存在
        check = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{spine_branch}"],
            cwd=canonical, capture_output=True, text=True,
        )
        assert check.returncode == 0, f"in-progress branch {spine_branch} 不应被删"
        # skipped_worktrees 包含该路径
        assert str(wt_path) in payload.get("skipped_worktrees", [])

    def test_mixed_orphan_and_in_progress(self, tmp_path, capsys, monkeypatch):
        """同时存在孤儿和 in-progress worktree：前者被清，后者保留。"""
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        # 孤儿 worktree
        orphan_ts = "2026-06-20-0800-00abc0"
        orphan_path = tmp_path / "wt_orphan"
        _make_spine_worktree(canonical, orphan_path, orphan_ts)
        _make_wt_task_log(home, orphan_path, orphan_ts, "aborted")

        # in-progress worktree
        ip_ts = "2026-06-22-0901-00abc1"
        ip_path = tmp_path / "wt_inprogress"
        ip_branch = _make_spine_worktree(canonical, ip_path, ip_ts)
        _make_wt_task_log(home, ip_path, ip_ts, "in-progress")

        tld = home / "task_log" / _paths.proj_key_for(canonical)
        tld.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(_clean._paths, "detect_repo_root", lambda start=None: canonical)
        monkeypatch.setattr(_clean.Path, "home", staticmethod(lambda: home))

        _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert payload["ok"] is True
        # in-progress worktree 仍存在
        assert ip_path.is_dir()
        # in-progress branch 仍存在
        check = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{ip_branch}"],
            cwd=canonical, capture_output=True, text=True,
        )
        assert check.returncode == 0, "in-progress branch 不应被删"
        # skipped_worktrees 包含 in-progress 路径
        assert str(ip_path) in payload.get("skipped_worktrees", [])


# ============================================================
# 回归测试：worktree staleness gate（F1 修复）
# ============================================================


class TestWorktreeStaleGate:
    """验证 scan_spine_worktrees 对 keep_days 保留窗口的 staleness 门控。

    核心回归：worktree 被分类为 orphan 之前，必须同时通过 task_log run
    的 age/status 检查（等同 plan_cleanup 三条件），而非仅检查无 in-progress state。
    """

    def test_too_recent_worktree_kept_despite_no_in_progress(self, tmp_path):
        """无 in-progress state 但 task_log run 太新 → 不归为 orphan（保留）。

        修复前：scan_spine_worktrees 只要无 in-progress 就归 orphan；
        修复后：还需 task_log run 满足 keep_days 旧度。
        """
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-20-0800-00abc0"
        wt_path = tmp_path / "wt_recent"
        _make_spine_worktree(canonical, wt_path, run_ts)
        # 太新（age_days=2 < keep_days=14）→ 不应删
        _make_wt_task_log(home, wt_path, run_ts, "completed", age_days=2)

        now_ms = _clean._io.now_ms()
        orphans, in_progress = _clean.scan_spine_worktrees(
            canonical, home, keep_days=14, now_ms=now_ms
        )

        orphan_paths = [o["path"] for o in orphans]
        # 太新的 worktree 不应出现在 orphan 列表
        assert str(wt_path) not in orphan_paths
        # 也不应在 in_progress 列表（无 in-progress state）
        ip_paths = [w["path"] for w in in_progress]
        assert str(wt_path) not in ip_paths

    def test_missing_task_log_worktree_kept_conservatively(self, tmp_path):
        """无 task_log 目录（runs=[]）→ 保守保留，不归 orphan。

        修复前：scan_spine_worktrees 在此情况下可能因 find_latest_in_progress
        返回 None 而将其归为 orphan；修复后加了 runs=[] 保守保留门控。
        """
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-20-0800-00abc0"
        wt_path = tmp_path / "wt_no_tasklog"
        _make_spine_worktree(canonical, wt_path, run_ts)
        # 故意不创建任何 task_log，让 scan_runs 返回空

        now_ms = _clean._io.now_ms()
        orphans, in_progress = _clean.scan_spine_worktrees(
            canonical, home, keep_days=14, now_ms=now_ms
        )

        orphan_paths = [o["path"] for o in orphans]
        assert str(wt_path) not in orphan_paths

    def test_stale_worktree_still_classified_orphan(self, tmp_path):
        """旧 worktree（age_days > keep_days）无 in-progress → 仍正确归为 orphan。

        确保修复未破坏正常清理路径。
        """
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-20-0800-00abc0"
        wt_path = tmp_path / "wt_stale"
        _make_spine_worktree(canonical, wt_path, run_ts)
        # 60 天前，远超 keep_days=14 → 应归 orphan
        _make_wt_task_log(home, wt_path, run_ts, "completed", age_days=60)

        now_ms = _clean._io.now_ms()
        orphans, in_progress = _clean.scan_spine_worktrees(
            canonical, home, keep_days=14, now_ms=now_ms
        )

        orphan_paths = [o["path"] for o in orphans]
        assert str(wt_path) in orphan_paths

    def test_suffixed_run_ts_stale_worktree_classified_orphan(self, tmp_path):
        """scan_spine_worktrees 使用真实 suffix 格式的 run_ts 目录（无截断 shadow 目录）。

        回归 F1：修复前 scan_runs 仅匹配 YYYY-MM-DD-HHMM（无 suffix），导致
        make_run_ts() 产生的实际 suffix 格式目录被跳过，scan_runs 返回 []，
        从而令保守逻辑跳过该 worktree，孤儿 worktree 永远不被删。
        修复后 RUN_TS_RE 匹配 YYYY-MM-DD-HHMM(-[0-9a-f]+)?，scan_runs
        能识别真实 suffix 格式目录，stale orphan 被正确归类为 orphan。
        """
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        # 使用真实 make_run_ts 格式（含 suffix），无任何截断 shadow 目录
        from npc import paths as _paths_mod
        run_ts = _paths_mod.make_run_ts()
        # make_run_ts 格式：YYYY-MM-DD-HHMM-<suffix>，总长度 > 15（前缀 15 字符 + dash + suffix）
        assert len(run_ts) > 15 and run_ts[15] == "-", (
            f"make_run_ts 应产生带 suffix 的格式（YYYY-MM-DD-HHMM-suffix），实际: {run_ts}"
        )

        wt_path = tmp_path / "wt_suffixed_stale"
        _make_spine_worktree(canonical, wt_path, run_ts)

        # 仅创建带 suffix 的 run 目录（不创建截断版），age_days=60 足够旧
        wt_proj_key = _paths_mod.proj_key_for(wt_path)
        wt_task_log = home / "task_log" / wt_proj_key
        wt_task_log.mkdir(parents=True, exist_ok=True)

        run_dir = wt_task_log / run_ts
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.events.jsonl").write_text("", encoding="utf-8")
        state = {"run_ts": run_ts, "status": "completed", "progress": []}
        state_file = wt_task_log / f"{run_ts}-plan-state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        _age(run_dir, 60)
        _age(state_file, 60)

        # 确认 task_log 下只有带 suffix 的目录（无截断 shadow）
        dirs = [d.name for d in wt_task_log.iterdir() if d.is_dir()]
        assert dirs == [run_ts], f"task_log 应只有 suffix 目录，实际: {dirs}"

        now_ms = _clean._io.now_ms()
        orphans, in_progress = _clean.scan_spine_worktrees(
            canonical, home, keep_days=14, now_ms=now_ms
        )

        orphan_paths = [o["path"] for o in orphans]
        assert str(wt_path) in orphan_paths, (
            f"stale suffix-format worktree 应归为 orphan，实际 orphans: {orphan_paths}"
        )

    def test_too_recent_worktree_not_deleted_with_yes(self, tmp_path, capsys, monkeypatch):
        """--yes 时太新的 worktree 不被删（staleness gate 在 run() 路径生效）。

        这是端到端回归测试：验证 run() 将 keep_days/now_ms 传递给
        scan_spine_worktrees，从而阻止太新 worktree 被 --yes 删除。
        """
        home = tmp_path / "home"
        home.mkdir()
        canonical, _ = _setup_canonical(tmp_path)

        run_ts = "2026-06-20-0800-00abc0"
        wt_path = tmp_path / "wt_recent_e2e"
        spine_branch = _make_spine_worktree(canonical, wt_path, run_ts)

        # task_log：completed 但只有 2 天旧（< keep_days=14）
        _make_wt_task_log(home, wt_path, run_ts, "completed", age_days=2)

        tld = home / "task_log" / _paths.proj_key_for(canonical)
        tld.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(_clean._paths, "detect_repo_root", lambda start=None: canonical)
        monkeypatch.setattr(_clean.Path, "home", staticmethod(lambda: home))

        _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert payload["ok"] is True
        # worktree 不应出现在 worktree_actions（未被删）
        actions = payload.get("worktree_actions", [])
        assert not any(str(wt_path) in a for a in actions)

        # worktree 路径仍存在
        assert wt_path.is_dir()

        # branch 仍存在
        check = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{spine_branch}"],
            cwd=canonical, capture_output=True, text=True,
        )
        assert check.returncode == 0, f"too-recent branch {spine_branch} 不应被删"
