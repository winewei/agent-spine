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


# ============================================================
# [verify].test_baseline = "diff"
# ============================================================


def _setup_diff_cfg(fake_repo: Path) -> None:
    (fake_repo / ".npc").mkdir()
    (fake_repo / ".npc" / "config.toml").write_text(
        '[verify]\ntest = "fake-test"\ntest_baseline = "diff"\n'
    )
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "chore: npc config"], cwd=fake_repo, check=True)


def _pytest_like_output(failed: list[str]) -> str:
    rows = [f"FAILED {f}" for f in failed]
    rows.append(f"{len(failed)} failed, 10 passed in 1.0s")
    return "\n".join(rows) + "\n"


def test_integrate_diff_baseline_passes_when_failures_subset(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])
    _setup_diff_cfg(fake_repo)

    calls: list[str] = []

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k != "shell"})
        calls.append("test")
        # 基线与整合后失败集合一致（既有污染），exit 非 0
        out = _pytest_like_output(["tests/test_a.py::test_x[asyncio]", "tests/test_b.py::test_y"])
        return subprocess.CompletedProcess(argv, 1, stdout=out, stderr="")

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    assert out["ok"] is True, out
    assert calls == ["test", "test"], "diff 模式应整合前后各跑一次"
    assert out["verify_tests"] == "pass-baseline-diff"
    assert out["tests"] == {"mode": "diff", "failed": 2, "new_failures": []}
    assert (fake_repo / "feature.py").exists()


def test_integrate_diff_baseline_reverts_on_new_failure(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])
    _setup_diff_cfg(fake_repo)

    n = {"test": 0}

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k != "shell"})
        n["test"] += 1
        failed = ["tests/test_a.py::test_x"]
        if n["test"] == 2:
            failed.append("tests/test_new.py::test_regression")
        return subprocess.CompletedProcess(argv, 1, stdout=_pytest_like_output(failed), stderr="")

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    assert out["ok"] is False, out
    assert out["step"] == "verify-tests"
    assert out["tests"]["new_failures"] == ["tests/test_new.py::test_regression"]
    assert out["reverted"] is not None
    assert not (fake_repo / "feature.py").exists()
    assert _git(fake_repo, "status", "--porcelain") == ""


def test_integrate_verify_tests_failed_event_carries_diagnostics(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    """新增失败时 run event 必须带 cmd / tests.new_failures / tail，state 留 verify_tests_detail。"""
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])
    _setup_diff_cfg(fake_repo)

    n = {"test": 0}

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k != "shell"})
        n["test"] += 1
        failed = ["tests/test_a.py::test_x"]
        if n["test"] == 2:
            failed.append("tests/test_new.py::test_regression")
        return subprocess.CompletedProcess(argv, 1, stdout=_pytest_like_output(failed), stderr="")

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    assert out["ok"] is False

    events = [
        json.loads(line)
        for line in p.run_events.read_text().splitlines()
        if line.strip()
    ]
    ev = [e for e in events if e.get("event") == "integrate.verify_tests_failed"]
    assert len(ev) == 1
    ev = ev[0]
    assert ev["cmd"] == "fake-test"
    assert ev["tests"]["mode"] == "diff"
    assert ev["tests"]["new_failures"] == ["tests/test_new.py::test_regression"]
    assert "tests/test_new.py::test_regression" in ev["tail"]
    assert len(ev["tail"]) <= 4000
    assert ev["reverted"]

    entry = _state.read_state(p.state_json)["progress"][0]
    assert entry["verify_tests_detail"]["new_failures"] == ["tests/test_new.py::test_regression"]


def test_integrate_diff_baseline_unparseable_failure_reverts(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])
    _setup_diff_cfg(fake_repo)

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k != "shell"})
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="ImportError while loading conftest")

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    assert out["ok"] is False, out
    assert out["step"] == "verify-tests"
    assert out["tests"]["reason"] == "unparseable-failures"
    assert not (fake_repo / "feature.py").exists()


def test_integrate_strict_default_unchanged(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    """未配置 test_baseline 时仍是 strict：只跑一次，exit 0 才通过。"""
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])
    (fake_repo / ".npc").mkdir()
    (fake_repo / ".npc" / "config.toml").write_text('[verify]\ntest = "fake-test"\n')
    subprocess.run(["git", "add", "."], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "chore: npc config"], cwd=fake_repo, check=True)

    calls: list[str] = []

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k != "shell"})
        calls.append("test")
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    assert out["ok"] is True, out
    assert calls == ["test"]
    assert out["verify_tests"] == "pass"
    assert out["tests"] == {"mode": "strict", "failed": None, "new_failures": []}


# ============================================================
# main 互斥：内环在跑时拒绝整合
# ============================================================


def test_inner_loop_active_lists_only_main_mutating_phases():
    progress = [
        {"change_id": "a", "phases": {"implement": {"status": "in-progress"}}},
        {"change_id": "b", "phases": {"review-r1": {"status": "in-progress"}, "fix-r1": {"status": "done"}}},
        {"change_id": "c", "phases": {"fix-r2": {"status": "in-progress"}}},
        {"change_id": "d", "phases": {"archive": {"status": "in-progress"}}},
        {"change_id": "e", "phases": {"archive": {"status": "done"}}},
    ]
    active = _integrate.inner_loop_active(progress)
    assert [(x["seq"], x["change_id"], x["phase"]) for x in active] == [
        (2, "b", "review-r1"),
        (3, "c", "fix-r2"),
        (4, "d", "archive"),
    ]
    # 排除自身 seq
    assert [x["seq"] for x in _integrate.inner_loop_active(progress, exclude_seq=3)] == [2, 4]
    assert _integrate.inner_loop_active([]) == []


def test_integrate_refuses_while_main_lock_held(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    """main 锁被 change run 持有时，integrate 无副作用返回 inner-loop-active（含持有者与 phase 快照）。"""
    from npc import locks as _locks

    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", "add-bar")
    s = json.loads(p.state_json.read_text())
    s["progress"][1]["phases"] = {"fix-r1": {"status": "in-progress"}}
    p.state_json.write_text(json.dumps(s))
    head_before = _git(fake_repo, "rev-parse", "HEAD")
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])

    calls: list[str] = []

    def runner(argv, **kwargs):
        calls.append(argv[0])
        return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k != "shell"})

    holder = _locks.try_acquire(_locks.main_lock_path(p.task_log_dir), owner="change run seq=2")
    assert holder is not None
    try:
        out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    finally:
        _locks.release(holder)
    assert out["ok"] is False
    assert out["step"] == "inner-loop-active"
    assert out["reason"] == "main-busy"
    assert out["holder"]["owner"] == "change run seq=2"
    assert out["active"] == [{"seq": 2, "change_id": "add-bar", "phase": "fix-r1"}]
    assert calls == [], "未做任何 git / 测试调用"
    assert _git(fake_repo, "rev-parse", "HEAD") == head_before
    assert not (fake_repo / "feature.py").exists()

    # 锁释放后正常整合，且整合结束会放锁
    out2 = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    assert out2["ok"] is True, out2
    assert (fake_repo / "feature.py").exists()
    again = _locks.try_acquire(_locks.main_lock_path(p.task_log_dir), owner="probe")
    assert again is not None
    _locks.release(again)


def test_integrate_force_bypasses_main_lock(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    from npc import locks as _locks

    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])
    holder = _locks.try_acquire(_locks.main_lock_path(p.task_log_dir), owner="other")
    try:
        out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, force=True)
    finally:
        _locks.release(holder)
    assert out["ok"] is True, out


def test_integrate_strict_mode_reresolves_test_cmd_after_cherry_pick(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    """strict 模式：change 首次引入测试命令时，整合后必须探测到并真实复跑。"""
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    assert not (fake_repo / "pyproject.toml").exists()
    wc = _side_branch_commit(fake_repo, fname="pyproject.toml", content="[project]\nname='x'\n")
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])

    seen: list[list[str]] = []

    def runner(argv, **kwargs):
        if argv[0] == "git":
            return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k != "shell"})
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest, runner=runner)
    assert out["ok"] is True, out
    assert out["verify_tests"] == "pass", "整合后新出现 pyproject.toml，应探测到 pytest 并复跑"
    assert seen, "测试命令未被执行"


def test_integrate_ignores_own_and_implement_phases(
    env_setup, fake_repo, make_args, capsys, tmp_path, summary_file
):
    """自身 seq 的 in-progress 与他人的 implement in-progress 都不构成互斥。"""
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", "add-bar")
    s = json.loads(p.state_json.read_text())
    s["progress"][0]["phases"] = {"implement": {"status": "in-progress"}}
    s["progress"][1]["phases"] = {"implement": {"status": "in-progress"}}
    p.state_json.write_text(json.dumps(s))
    wc = _side_branch_commit(fake_repo)
    manifest = _manifest_for(tmp_path, tmp_path, wc, ["implement.summary.md"])
    out = _integrate.run_integrate(p, 1, _result_line(wc, summary_file), manifest)
    assert out["ok"] is True, out
