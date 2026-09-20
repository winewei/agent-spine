"""Progress and delivery semantics with simulated time, no wall-clock waits."""
import argparse
import json
import subprocess

import pytest

from npc import monitor, paths
from npc.cli import _build_parser


def document():
    return dict(agents={}, serial=0, stopped=False)


def add(doc, root, agent_id="coder", role="implement", artifacts=None, worktree=None):
    return monitor.register(doc, agent_id=agent_id, role=role, handle=f"host:{agent_id}",
                            artifacts=artifacts or [], worktree=worktree, repo_root=root, now=0)


def tick(doc, root, now):
    return monitor.tick(doc, repo_root=root, now=now)


def ack(doc, action, now, decision="sent", note="", wait_seconds=0):
    monitor.acknowledge(doc, action_id=action["id"], decision=decision, note=note,
                        now=now, wait_seconds=wait_seconds)


def test_every_role_gets_periodic_inquiry_and_missing_agent_stays_active(tmp_path):
    doc = document()
    for role in ("analyst", "architect", "implement", "review", "fix"):
        add(doc, tmp_path, role, role)
    assert tick(doc, tmp_path, 599)["actions"] == []
    result = tick(doc, tmp_path, 600)
    assert result["active"] == 5
    assert {a["role"] for a in result["actions"]} == set(doc["agents"])
    assert all(a["kind"] == "CHECK_IN" for a in result["actions"])
    # No transcript discovery or heartbeat can silently finish a registered job.
    assert tick(doc, tmp_path, 20000)["active"] == 5


def test_inquiry_must_be_sent_before_grace_and_dedup_survives_restart(tmp_path):
    with monitor.checkpoint(tmp_path) as doc:
        add(doc, tmp_path)
        action = tick(doc, tmp_path, 600)["actions"][0]
    with monitor.checkpoint(tmp_path) as doc:
        assert tick(doc, tmp_path, 10000)["actions"][0]["id"] == action["id"]
        assert tick(doc, tmp_path, 10000)["actions"][0]["kind"] == "CHECK_IN"
        with pytest.raises(ValueError, match="先实际询问"):
            ack(doc, action, 10000, "progress", "working")
        ack(doc, action, 10000)
        ack(doc, action, 10100)  # retries don't extend reply deadline
        assert tick(doc, tmp_path, 10299)["actions"][0]["kind"] == "CHECK_IN"
        control = tick(doc, tmp_path, 10300)["actions"][0]
        assert control["kind"] == "CONTROL_REQUIRED"
        assert control["no_progress"]
        assert control["id"] != action["id"]
        with pytest.raises(ValueError):
            ack(doc, control, 10300)
        ack(doc, control, 10300, "intervene", "saved worktree; redirected original coder")
        assert doc["agents"]["coder"]["progress_at"] == 0


def test_analysis_artifact_counts_but_same_content_and_liveness_do_not(tmp_path):
    artifact = tmp_path / "analysis.json"
    artifact.write_text("baseline")
    doc = document()
    row = add(doc, tmp_path, "analyst", "analyst", [str(artifact)])
    artifact.write_text("new dependency evidence")
    assert tick(doc, tmp_path, 500)["actions"] == []
    assert row["progress_at"] == 500
    # Recent output/heartbeat and touching a result are not an advance.
    (tmp_path / "transcript.jsonl").write_text("still working")
    row["heartbeat"] = 1400
    artifact.touch()
    action = tick(doc, tmp_path, 1400)["actions"][0]
    assert action["no_progress"]
    assert row["progress_at"] == 500
    assert action["kind"] == "CHECK_IN"  # progress never disables periodic inquiry


def test_bounded_wait_expires_and_reregister_preserves_pending(tmp_path):
    doc = document()
    row = add(doc, tmp_path)
    action = tick(doc, tmp_path, 600)["actions"][0]
    add(doc, tmp_path)
    assert row["pending"]["id"] == action["id"]
    ack(doc, action, 600)
    with pytest.raises(ValueError, match="1–900"):
        ack(doc, action, 620, "wait", "test running", 99999)
    ack(doc, action, 620, "wait", "test pid verified; expect result within 60 seconds", 60)
    assert tick(doc, tmp_path, 679)["actions"] == []
    assert tick(doc, tmp_path, 680)["actions"][0]["kind"] == "CHECK_IN"
    assert row["progress_at"] == 0
    with pytest.raises(ValueError, match="另一任务"):
        add(doc, tmp_path, role="different")


def test_only_isolated_git_changes_count_as_evidence(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    git("init")
    git("-c", "user.name=Test", "-c", "user.email=t@e.test", "commit", "--allow-empty", "-m", "init")
    doc = document()
    shared = add(doc, repo, "shared", worktree=str(repo))
    wt = tmp_path / "external-worktree"
    git("worktree", "add", "-b", "worker", str(wt))
    isolated = add(doc, repo, "isolated", worktree=str(wt))
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=Test", "-c", "user.email=t@e.test",
                    "commit", "--allow-empty", "-m", "worker result"], check=True, capture_output=True)
    git("-c", "user.name=Test", "-c", "user.email=t@e.test", "commit", "--allow-empty", "-m", "someone else")
    tick(doc, repo, 100)
    assert isolated["progress_at"] == 100
    assert shared["progress_at"] == 0


def test_startup_finish_stop_and_no_business_state_writes(computed_paths, capsys):
    p = computed_paths
    paths.write_run_json(p)
    paths.set_active(p.task_log_dir, p.run_ts)
    def run(*args):
        ns = _build_parser().parse_args(["--run-ts", p.run_ts, "--task-log-dir", str(p.task_log_dir), "monitor", *args])
        ns.handler(ns)
        return json.loads(capsys.readouterr().out)
    assert run("tick")["active"] == 0
    run("register", "--id", "a", "--role", "review", "--handle", "native-a")
    with pytest.raises(SystemExit):
        run("stop")
    capsys.readouterr()
    run("finish", "--id", "a", "--note", "host confirms exit; review artifact checked")
    run("stop")
    assert run("tick")["stopped"]
    assert not p.state_json.exists()
    assert not (p.run_dir / "scheduler.json").exists()


def test_checkpoint_exception_does_not_overwrite(tmp_path):
    with monitor.checkpoint(tmp_path) as doc:
        add(doc, tmp_path)
    before = (tmp_path / "monitor.json").read_bytes()
    with pytest.raises(ValueError):
        with monitor.checkpoint(tmp_path) as doc:
            doc["agents"].clear()
            raise ValueError("failed")
    assert (tmp_path / "monitor.json").read_bytes() == before


def test_deleted_and_restored_identical_artifact_is_not_progress(tmp_path):
    artifact = tmp_path / "result"
    artifact.write_text("same")
    doc = document()
    row = add(doc, tmp_path, artifacts=[str(artifact)])
    artifact.unlink()
    tick(doc, tmp_path, 100)
    artifact.write_text("same")
    tick(doc, tmp_path, 200)
    assert row["progress_at"] == 0


def test_bad_intervals_rejected():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["monitor", "follow", "--interval", "0"])
