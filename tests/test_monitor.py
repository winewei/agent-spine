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


# ---------------------------------------------------------------- 1.9: jobs, signals, emission

def obs(evidence=None, done=None, alive=None, error=None):
    return dict(evidence=evidence if evidence is not None else {}, marker=None,
                done=done, alive=alive, error=error)


def job(doc, root, agent_id="bench", **kw):
    kw.setdefault("alive", "true")
    return monitor.register(doc, agent_id=agent_id, role="bench", handle=f"ssh:{agent_id}",
                            artifacts=[], worktree=None, repo_root=root, now=0, kind="job",
                            cwd=str(root), **kw)


def test_probe_first_reading_is_baseline_and_marker_change_is_progress(tmp_path):
    (tmp_path / "log").write_text("1\n")
    doc = document()
    row = job(doc, tmp_path, probe="wc -l < log")
    tick(doc, tmp_path, 100)
    assert row["progress_at"] == 0
    assert row["marker"] == "1"
    (tmp_path / "log").write_text("1\n2\n")
    tick(doc, tmp_path, 200)
    assert row["progress_at"] == 200
    assert row["marker"] == "2"


def test_probe_failure_is_observation_error_not_progress(tmp_path):
    doc = document()
    row = job(doc, tmp_path, probe="echo boom >&2; exit 3")
    tick(doc, tmp_path, 100)
    assert row["progress_at"] == 0
    assert "probe exit 3" in row["observation_error"] and "boom" in row["observation_error"]


def test_job_needs_a_terminal_path(tmp_path):
    doc = document()
    with pytest.raises(ValueError, match="终态检测路径"):
        monitor.register(doc, agent_id="j", role="bench", handle="h", artifacts=[],
                         worktree=None, repo_root=tmp_path, now=0, kind="job", probe="date")
    with pytest.raises(ValueError, match="正整数"):
        job(doc, tmp_path, deadline_seconds=0)
    job(doc, tmp_path, alive=None, deadline_seconds=60)


def test_job_stall_is_reported_instead_of_check_in_and_rearms_after_decision(tmp_path):
    doc = document()
    job(doc, tmp_path, probe="echo constant")
    assert tick(doc, tmp_path, 899)["actions"] == []
    stall = tick(doc, tmp_path, 900)["actions"][0]
    assert (stall["kind"], stall["task_kind"], stall["no_progress"]) == ("STALL", "job", True)
    with pytest.raises(ValueError, match="STALL"):
        ack(doc, stall, 900)
    ack(doc, stall, 900, "progress", "compaction phase writes no log; checked remote load")
    assert tick(doc, tmp_path, 1799)["actions"] == []
    assert tick(doc, tmp_path, 1800)["actions"][0]["kind"] == "STALL"


def test_job_without_progress_evidence_never_stalls(tmp_path):
    doc = document()
    job(doc, tmp_path)
    assert tick(doc, tmp_path, 100000)["actions"] == []


def test_done_signal_stops_probing_and_only_the_host_closes(tmp_path):
    doc = document()
    row = job(doc, tmp_path, probe="echo x >> calls; wc -l < calls", done="test -f done.flag")
    tick(doc, tmp_path, 10)
    (tmp_path / "done.flag").write_text("")
    signal = tick(doc, tmp_path, 20)["actions"][0]
    assert signal["kind"] == "DONE_SIGNAL" and row["status"] == "signaled"
    calls = (tmp_path / "calls").read_text().count("x")
    for now in (30, 40, 50):
        assert tick(doc, tmp_path, now)["actions"][0]["id"] == signal["id"]
    assert (tmp_path / "calls").read_text().count("x") == calls
    with pytest.raises(ValueError, match="finish/cancel"):
        ack(doc, signal, 50, "progress", "looks done")
    ack(doc, signal, 50, "wait", "verifying result table against baseline", 60)
    assert tick(doc, tmp_path, 109)["actions"] == []
    again = tick(doc, tmp_path, 110)["actions"][0]
    assert again["kind"] == "DONE_SIGNAL" and again["id"] != signal["id"]
    assert tick(doc, tmp_path, 110)["active"] == 1
    with pytest.raises(ValueError, match="证据"):
        monitor.close(doc, agent_id="bench", status="finished", note=" ", now=120)
    assert monitor.close(doc, agent_id="bench", status="finished", note="BENCH_DONE and table ok",
                         now=120, result="done")
    assert not monitor.close(doc, agent_id="bench", status="finished", note="retry", now=121)
    assert (row["status"], row["result"], row["pending"]) == ("finished", "done", None)
    assert tick(doc, tmp_path, 130) == dict(ok=True, stopped=False, active=0, actions=[])


def test_exit_needs_consecutive_failed_liveness_checks_and_done_wins(tmp_path):
    doc = document()
    flaky = job(doc, tmp_path, "flaky", alive="test -f alive.flag")
    job(doc, tmp_path, "finished", alive="false", done="true")
    result = tick(doc, tmp_path, 10)
    assert [a["kind"] for a in result["actions"]] == ["DONE_SIGNAL"]
    (tmp_path / "alive.flag").write_text("")
    tick(doc, tmp_path, 20)
    (tmp_path / "alive.flag").unlink()
    tick(doc, tmp_path, 30)
    assert flaky["status"] == "active"
    kinds = {a["agent_id"]: a["kind"] for a in tick(doc, tmp_path, 40)["actions"]}
    assert kinds == {"flaky": "EXITED_SIGNAL", "finished": "DONE_SIGNAL"}


def test_deadline_extension_and_clearing_require_explicit_decisions(tmp_path):
    doc = document()
    row = job(doc, tmp_path, deadline_seconds=100)
    assert tick(doc, tmp_path, 99)["actions"] == []
    deadline = tick(doc, tmp_path, 100)["actions"][0]
    assert deadline["kind"] == "DEADLINE"
    monitor.acknowledge(doc, action_id=deadline["id"], decision="intervene",
                        note="warmup slower than planned; extend", now=100, deadline_seconds=50)
    assert (row["status"], row["deadline_at"]) == ("active", 150)
    assert tick(doc, tmp_path, 149)["actions"] == []
    deadline = tick(doc, tmp_path, 150)["actions"][0]
    ack(doc, deadline, 150, "intervene", "deadline no longer applies; liveness still checked")
    assert row["deadline_at"] is None
    assert tick(doc, tmp_path, 100000)["actions"] == []


def test_terminal_signal_supersedes_pending_inquiry_and_bounded_wait(tmp_path):
    doc = document()
    row = monitor.register(doc, agent_id="coder", role="implement", handle="agent:c",
                           artifacts=[], worktree=None, repo_root=tmp_path, now=0,
                           done="test -f RESULT", cwd=str(tmp_path))
    inquiry = tick(doc, tmp_path, 600)["actions"][0]
    ack(doc, inquiry, 600)
    ack(doc, inquiry, 610, "wait", "long test suite; pid verified", 600)
    (tmp_path / "RESULT").write_text("RESULT: commit=abc")
    signal = tick(doc, tmp_path, 620)["actions"][0]
    assert signal["kind"] == "DONE_SIGNAL" and row["wait_until"] == 0
    ack(doc, signal, 630, "intervene", "RESULT malformed; asked coder to rewrite it")
    assert row["status"] == "active"
    assert [e["outcome"] for e in doc["_journal"]] == ["wait", "intervene"]


def test_superseded_pending_is_journaled(tmp_path):
    doc = document()
    monitor.register(doc, agent_id="coder", role="implement", handle="agent:c", artifacts=[],
                     worktree=None, repo_root=tmp_path, now=0, done="test -f RESULT",
                     cwd=str(tmp_path))
    inquiry = tick(doc, tmp_path, 600)["actions"][0]
    (tmp_path / "RESULT").write_text("done")
    assert tick(doc, tmp_path, 610)["actions"][0]["kind"] == "DONE_SIGNAL"
    assert doc["_journal"][-1]["id"] == inquiry["id"]
    assert doc["_journal"][-1]["outcome"] == "superseded"


def test_recent_evidence_stretches_inquiry_cadence_without_disabling_it(tmp_path):
    doc = document()
    add(doc, tmp_path)
    def at(now, value):
        return monitor.tick(doc, repo_root=tmp_path, now=now,
                            observations={"coder": obs({"x": value})})["actions"]
    for now, value in ((550, "a"), (600, "a"), (1100, "b"), (1650, "c"), (1799, "c")):
        assert at(now, value) == [], now
    assert at(1800, "c")[0]["kind"] == "CHECK_IN"
    doc = document()
    add(doc, tmp_path)
    assert at(550, "a") == [] and at(1149, "a") == []
    assert at(1150, "a")[0]["kind"] == "CHECK_IN"  # evidence went quiet: back to 600 s


def test_missing_observation_skips_evidence_for_that_round(tmp_path):
    doc = document()
    row = job(doc, tmp_path, probe="echo x")
    monitor.tick(doc, repo_root=tmp_path, now=10, observations={})
    assert "marker" not in row and row["evidence"] == {}


def test_emitter_wakes_only_for_new_decisions_and_reminds_unresolved():
    emit = monitor.Emitter(remind_seconds=1800)
    def snap(*actions, stopped=False):
        return dict(ok=True, stopped=stopped, active=len(actions), actions=list(actions))
    a1 = dict(id="1", kind="CHECK_IN", no_progress=False, sent_at=None)
    assert not emit(snap(), 0)
    assert emit(snap(a1), 60)
    assert not emit(snap(dict(a1, sent_at=61)), 120)  # own ack
    assert not emit(snap(), 180)  # own decision / registration churn
    a2 = dict(id="2", kind="STALL", no_progress=True, sent_at=None)
    assert emit(snap(a2), 240)
    assert not emit(snap(a2), 2039)
    assert emit(snap(a2), 2040)  # unresolved reminder
    assert emit(snap(dict(a1, id="3", no_progress=False), a2), 2100)
    assert emit(snap(dict(a1, id="3", no_progress=True), a2), 2160)
    assert emit(snap(stopped=True), 2220)


def test_checkpoint_moves_decisions_to_history_journal(tmp_path):
    with monitor.checkpoint(tmp_path) as doc:
        add(doc, tmp_path)
        action = tick(doc, tmp_path, 600)["actions"][0]
        ack(doc, action, 600)
        ack(doc, action, 610, "progress", "commit abc adds parser tests")
    history = [json.loads(l) for l in (tmp_path / "monitor.history.jsonl").read_text().splitlines()]
    assert [(h["agent_id"], h["outcome"]) for h in history] == [("coder", "progress")]
    assert "_journal" not in json.loads((tmp_path / "monitor.json").read_text())


def test_pre_19_rows_resume_unchanged(tmp_path):
    doc = document()
    doc["agents"]["old"] = dict(role="review", handle="agent:r", worktree=None, artifacts=[],
                                status="active", registered_at=0, progress_at=0, inquiry_at=0,
                                evidence={}, pending=None, wait_until=0, history=[])
    assert monitor.register(doc, agent_id="old", role="review", handle="agent:r",
                            worktree=None, artifacts=[], repo_root=tmp_path, now=5) is doc["agents"]["old"]
    action = tick(doc, tmp_path, 600)["actions"][0]
    assert (action["kind"], action["task_kind"]) == ("CHECK_IN", "agent")


def test_owner_scope_list_cancel_stop_and_revive(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    def run(*args):
        ns = _build_parser().parse_args(["monitor", *args])
        ns.handler(ns)
        return json.loads(capsys.readouterr().out)
    registered = run("register", "--owner", "bench", "--id", "heap", "--role", "bench",
                     "--handle", "ssh:ctl", "--kind", "job", "--alive", "true",
                     "--probe", "echo 1")
    scope_dir = tmp_path / "home" / "task_log" / "_monitor" / "bench"
    assert registered["scope"] == "owner"
    assert registered["checkpoint"] == str(scope_dir / "monitor.json")
    assert run("tick", "--owner", "bench")["active"] == 1
    listed = run("list", "--owner", "bench", "--open")
    assert [(t["id"], t["kind"], t["status"], t["marker"]) for t in listed["tasks"]] == [
        ("heap", "job", "active", "1")]
    with pytest.raises(SystemExit):
        run("stop", "--owner", "bench")
    capsys.readouterr()
    run("cancel", "--owner", "bench", "--id", "heap", "--note", "superseded by new heap size")
    assert run("finish", "--owner", "bench", "--id", "heap", "--note", "retry")["already_closed"]
    assert run("list", "--owner", "bench", "--open")["tasks"] == []
    run("stop", "--owner", "bench")
    run("register", "--owner", "bench", "--id", "heap2", "--role", "bench",
        "--handle", "ssh:ctl", "--kind", "job", "--deadline-seconds", "3600")
    assert run("list", "--owner", "bench")["open"] == 1
    history = (scope_dir / "monitor.history.jsonl").read_text()
    assert '"outcome": "cancelled"' in history
    with pytest.raises(SystemExit):
        run("list", "--owner", "../escape")
    assert json.loads(capsys.readouterr().out)["error"] == "monitor_failed"


def test_run_scope_rejects_register_after_stop(computed_paths, capsys):
    p = computed_paths
    paths.write_run_json(p)
    paths.set_active(p.task_log_dir, p.run_ts)
    def run(*args):
        ns = _build_parser().parse_args(["--run-ts", p.run_ts, "--task-log-dir", str(p.task_log_dir), "monitor", *args])
        ns.handler(ns)
        return json.loads(capsys.readouterr().out)
    run("stop")
    with pytest.raises(SystemExit):
        run("register", "--id", "late", "--role", "review", "--handle", "native")
    assert "新 run" in json.loads(capsys.readouterr().out)["message"]
