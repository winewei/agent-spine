"""clean 模块测试。

重点是删除安全性：
- 纯函数 plan_cleanup 直接喂构造好的 run 列表，覆盖 active / in-progress /
  终态 / 太新 / 孤儿 / aborted 各分支；
- handler dry-run 断言文件仍存在（不删）+ removable 列表正确；
- handler --yes 断言目标被删、active 与 in-progress 仍在；
- 非 git → exit 3。
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_spine.npc import clean as _clean, paths as _paths


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
    runs = [_run("r1", "in-progress", age_days=365)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "non-terminal:in-progress"


def test_completed_old_enough_removable():
    runs = [_run("r1", "completed", age_days=30)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert [r["run_ts"] for r in plan["removable"]] == ["r1"]
    assert plan["removable"][0]["reason"] == "completed"
    assert plan["kept"] == []


def test_completed_too_recent_kept():
    runs = [_run("r1", "completed", age_days=3)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "too-recent"


def test_completed_with_issues_removable():
    runs = [_run("r1", "completed-with-issues", age_days=30)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert [r["run_ts"] for r in plan["removable"]] == ["r1"]
    assert plan["removable"][0]["reason"] == "completed-with-issues"


def test_aborted_removable():
    runs = [_run("r1", "aborted", age_days=30)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert [r["run_ts"] for r in plan["removable"]] == ["r1"]
    assert plan["removable"][0]["reason"] == "aborted"


def test_orphan_missing_status_old_enough_removable():
    # status=None 表示 state 缺失/不可读的孤儿目录。
    runs = [_run("r1", None, age_days=30)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert [r["run_ts"] for r in plan["removable"]] == ["r1"]
    assert plan["removable"][0]["reason"] == "orphan"


def test_orphan_too_recent_kept():
    runs = [_run("r1", None, age_days=1)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "too-recent"


def test_unknown_status_kept_conservatively():
    # 非终态、非 None 的未知 status 保守保留。
    runs = [_run("r1", "reviewing", age_days=100)]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "non-terminal:reviewing"


def test_missing_mtime_kept_conservatively():
    runs = [{"run_ts": "r1", "status": "completed", "mtime_ms": None}]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "no-mtime"


def test_boundary_exactly_cutoff_kept():
    # mtime == cutoff 不算"早于"，应保留（>= cutoff 即 kept）。
    runs = [{"run_ts": "r1", "status": "completed", "mtime_ms": NOW - 14 * _DAY_MS}]
    plan = _clean.plan_cleanup(runs, active_ts=None, keep_days=14, now_ms=NOW)
    assert plan["removable"] == []
    assert plan["kept"][0]["reason"] == "too-recent"


def test_mixed_set():
    runs = [
        _run("active", "completed", age_days=100),
        _run("inprog", "in-progress", age_days=100),
        _run("done-old", "completed", age_days=100),
        _run("done-new", "completed", age_days=2),
        _run("orphan-old", None, age_days=100),
        _run("aborted-old", "aborted", age_days=100),
    ]
    plan = _clean.plan_cleanup(runs, active_ts="active", keep_days=14, now_ms=NOW)
    removable = sorted(r["run_ts"] for r in plan["removable"])
    assert removable == ["aborted-old", "done-old", "orphan-old"]
    kept = sorted(r["run_ts"] for r in plan["kept"])
    assert kept == ["active", "done-new", "inprog"]


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
    _paths.set_active(tld, "active-run")
    _make_run(tld, "active-run", "completed")
    old = _make_run(tld, "old-run", "completed")
    _age(old, 60)
    _age(tld / "old-run-plan-state.json", 60)
    _age(tld / "old-run-plan-state.md", 60)

    _clean.run(_args(task_log_dir=str(tld), yes=False, keep_days=14))

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["removable"] == ["old-run"]
    assert payload["kept_count"] == 1

    # 关键：什么都没删。
    assert (tld / "old-run").is_dir()
    assert (tld / "old-run-plan-state.json").is_file()
    assert (tld / "old-run-plan-state.md").is_file()
    assert (tld / "active-run").is_dir()


def test_dry_run_is_default_when_yes_absent(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    old = _make_run(tld, "old-run", "aborted")
    _age(old, 60)
    _age(tld / "old-run-plan-state.json", 60)

    # 不传 yes（make 默认 False）。
    _clean.run(_args(task_log_dir=str(tld), keep_days=14))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert (tld / "old-run").is_dir()


# ============================================================
# handler: --yes（真删，但 active / in-progress 永不删）
# ============================================================


def test_yes_deletes_removable_only(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    _paths.set_active(tld, "active-run")

    _make_run(tld, "active-run", "completed")  # active → keep
    _make_run(tld, "inprog-run", "in-progress")  # in-progress → keep
    new = _make_run(tld, "new-run", "completed")  # too recent → keep
    old = _make_run(tld, "old-run", "completed")  # → delete
    orphan = _make_run(tld, "orphan-run", None, mk_state=False)  # 孤儿 → delete

    _age(new, 2)
    _age(old, 60)
    _age(tld / "old-run-plan-state.json", 60)
    _age(tld / "old-run-plan-state.md", 60)
    _age(orphan, 60)

    _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["kept_count"] == 3  # active + inprog + new

    # 被删的：old-run（目录 + json + md）、orphan-run（目录）
    assert not (tld / "old-run").exists()
    assert not (tld / "old-run-plan-state.json").exists()
    assert not (tld / "old-run-plan-state.md").exists()
    assert not (tld / "orphan-run").exists()

    # 必须保留的：
    assert (tld / "active-run").is_dir()
    assert (tld / "inprog-run").is_dir()
    assert (tld / "new-run").is_dir()

    # removed 列表包含被删路径。
    removed = set(payload["removed"])
    assert str(tld / "old-run") in removed
    assert str(tld / "old-run-plan-state.json") in removed
    assert str(tld / "old-run-plan-state.md") in removed
    assert str(tld / "orphan-run") in removed


def test_yes_never_deletes_in_progress_even_if_old(tmp_path, capsys):
    tld = tmp_path / "task_log"
    tld.mkdir()
    ip = _make_run(tld, "inprog", "in-progress")
    _age(ip, 365)
    _age(tld / "inprog-plan-state.json", 365)

    _clean.run(_args(task_log_dir=str(tld), yes=True, keep_days=14))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["removed"] == []
    assert (tld / "inprog").is_dir()
    assert (tld / "inprog-plan-state.json").is_file()


# ============================================================
# scan_runs：从磁盘读 status（含孤儿 / 坏 JSON）
# ============================================================


def test_scan_runs_reads_status_and_orphans(tmp_path):
    tld = tmp_path / "task_log"
    tld.mkdir()
    _make_run(tld, "r1", "completed")
    _make_run(tld, "r2", None, mk_state=False)  # 孤儿
    # 坏 JSON → 视为孤儿
    _make_run(tld, "r3", "completed")
    (tld / "r3-plan-state.json").write_text("{ not json", encoding="utf-8")

    runs = {r["run_ts"]: r for r in _clean.scan_runs(tld)}
    assert runs["r1"]["status"] == "completed"
    assert runs["r2"]["status"] is None
    assert runs["r3"]["status"] is None  # 坏 JSON → 孤儿
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
