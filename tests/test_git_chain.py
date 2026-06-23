"""git_chain 模块测试。"""

from __future__ import annotations

import json
import subprocess

import pytest

from npc import git_chain as _gc, state as _state


def _commit(repo, msg, file="f.txt", content=""):
    (repo / file).write_text(content + msg + "\n")
    subprocess.run(["git", "add", file], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
    return (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )


def test_collect_expected_commits_ordering():
    entry = {
        "implement_commit": "IMPL",
        "phases": {
            "implement": {"status": "done", "commit": "IMPL"},
            "review-r0": {"status": "done"},
            "fix-r1": {"status": "done", "commit": "FIX1"},
            "review-r1": {"status": "done"},
            "fix-r3": {"status": "done", "commit": "FIX3"},
            "fix-r2": {"status": "done", "commit": "FIX2"},
        },
    }
    expected = _gc.collect_expected_commits(entry)
    assert expected == ["IMPL", "FIX1", "FIX2", "FIX3"]


def test_collect_expected_commits_empty():
    assert _gc.collect_expected_commits({"phases": {}}) == []


def test_is_ancestor_yes(fake_repo):
    c1 = _commit(fake_repo, "c1")
    c2 = _commit(fake_repo, "c2")
    assert _gc.is_ancestor(fake_repo, c1) is True
    assert _gc.is_ancestor(fake_repo, c2) is True


def test_is_ancestor_no(fake_repo, tmp_path):
    c1 = _commit(fake_repo, "c1")
    # 在 detached branch 写一个不会被 HEAD 包含的 commit
    subprocess.run(["git", "checkout", "-b", "side", "-q"], cwd=fake_repo, check=True)
    side_commit = _commit(fake_repo, "side commit", file="g.txt")
    subprocess.run(["git", "checkout", "-q", "main"], cwd=fake_repo, check=False)
    # 如果默认分支不是 main，回退到 master
    res = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=fake_repo,
        capture_output=True,
        text=True,
    )
    if res.stdout.strip() != "main":
        subprocess.run(["git", "checkout", "-q", "master"], cwd=fake_repo, check=False)
    assert _gc.is_ancestor(fake_repo, side_commit) is False


def test_check_chain_full(fake_repo):
    impl = _commit(fake_repo, "impl")
    fix1 = _commit(fake_repo, "fix1")
    entry = {
        "implement_commit": impl,
        "phases": {
            "implement": {"status": "done", "commit": impl},
            "fix-r1": {"status": "done", "commit": fix1},
        },
    }
    result = _gc.check_chain(fake_repo, entry)
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["expected"] == [impl, fix1]


def test_check_chain_missing(fake_repo):
    impl = _commit(fake_repo, "impl")
    entry = {
        "implement_commit": impl,
        "phases": {
            "implement": {"status": "done", "commit": impl},
            "fix-r1": {"status": "done", "commit": "deadbeef00000000000000000000000000000000"},
        },
    }
    result = _gc.check_chain(fake_repo, entry)
    assert result["ok"] is False
    assert "deadbeef00000000000000000000000000000000" in result["missing"]


def test_precheck_cli_ok(env_setup, fake_repo, capsys, make_args):
    impl = _commit(fake_repo, "impl")
    _state.write_state(
        env_setup.state_json,
        env_setup.state_md,
        {
            "schema_version": 2,
            "run_ts": env_setup.run_ts,
            "mode": "interactive",
            "status": "in-progress",
            "plan_order": ["add-foo"],
            "progress": [
                {
                    "seq": 1,
                    "change_id": "add-foo",
                    "status": "reviewing",
                    "implement_commit": impl,
                    "phases": {"implement": {"status": "done", "commit": impl}},
                }
            ],
        },
    )
    _gc.precheck(make_args(seq=1))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["missing"] == []


def test_precheck_cli_missing_exits_1(env_setup, fake_repo, capsys, make_args):
    impl = _commit(fake_repo, "impl")
    _state.write_state(
        env_setup.state_json,
        env_setup.state_md,
        {
            "schema_version": 2,
            "run_ts": env_setup.run_ts,
            "mode": "interactive",
            "status": "in-progress",
            "plan_order": ["add-foo"],
            "progress": [
                {
                    "seq": 1,
                    "change_id": "add-foo",
                    "status": "reviewing",
                    "implement_commit": impl,
                    "phases": {
                        "implement": {"status": "done", "commit": impl},
                        "fix-r1": {"status": "done", "commit": "dead" + "0" * 36},
                    },
                }
            ],
        },
    )
    with pytest.raises(SystemExit) as ei:
        _gc.precheck(make_args(seq=1))
    assert ei.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["missing"]
