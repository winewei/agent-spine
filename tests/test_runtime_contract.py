"""2.0 execution contract: EngineeringJob binding, run identity, status, events, result.

Everything runs the real ``npc`` CLI in-process against real git repositories;
only model processes (coder / reviewer / openspec archive) are deterministic fakes.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from npc import cli, isolated as iso, job as _job, locks, paths, pipeline, result as _result, state


# ------------------------------------------------------------------ helpers


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def commit(root: Path, name: str, text: str) -> str:
    (root / name).write_text(text)
    git(root, "add", name)
    git(root, "commit", "-qm", name)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for key in ("NPC_MODE", "NPC_FRESH", "NPC_RUN_TS", "NPC_REPO_ROOT"):
        monkeypatch.delenv(key, raising=False)
    return h


@pytest.fixture
def repo(tmp_path, home):
    r = tmp_path / "project"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@local")
    git(r, "config", "user.name", "T")
    (r / ".npc").mkdir()
    (r / ".npc" / "config.toml").write_text('[verify]\ntest = "true"\n')
    (r / "README.md").write_text("# project\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    return r.resolve()


def write_job(tmp_path: Path, repo: Path, name: str = "job.json", **overrides) -> Path:
    doc = {"schema_version": 1, "job_id": "job-123", "attempt_id": "attempt-1",
           "goal": "Add per-IP rate limiting to authentication endpoints",
           "repository": {"root": str(repo), "target_ref": "main"},
           "mode": "auto", "limits": {"max_parallel": 2}, "metadata": {"worker": "w-1"}}
    doc.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return path


def npc(capsys, *argv, cwd: Path | None = None, monkeypatch=None) -> tuple[int, dict]:
    if cwd is not None:
        monkeypatch.chdir(cwd)
    capsys.readouterr()
    code = 0
    try:
        cli.main(list(argv))
    except SystemExit as e:
        code = e.code or 0
    out = capsys.readouterr().out.strip().splitlines()
    return code, (json.loads(out[-1]) if out else {})


def run_paths(repo: Path, home: Path, run_ts: str) -> paths.Paths:
    return paths.read_run_json(paths.run_json_path_for(paths.task_log_dir_for(repo, home), run_ts))


def events(p: paths.Paths) -> list[dict]:
    return [json.loads(line) for line in p.run_events.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------ job contract


def test_job_contract_validation():
    base = {"schema_version": 1, "job_id": "j", "attempt_id": "a", "goal": "g",
            "repository": {"root": "/abs/repo"}}
    job = _job.parse_job(base)
    assert job.mode == "auto" and job.delivery_mode == "local" and job.target_ref is None
    assert _job.parse_job({**base, "repository": {"root": "/r", "target_ref": "feature/x"}}).target_ref \
        == "refs/heads/feature/x"
    bad = [
        ({**base, "extra": 1}, "invalid-job"),
        ({**base, "schema_version": 2}, "unsupported-schema-version"),
        ({**base, "schema_version": "1"}, "invalid-job"),
        ({**base, "delivery": {"mode": "github"}}, "unsupported-delivery"),
        ({**base, "repository": {"root": "relative"}}, "invalid-job"),
        ({**base, "repository": {"root": "/r", "target_ref": "refs/tags/v1"}}, "invalid-job"),
        ({**base, "repository": {"root": "/r", "target_ref": "a..b"}}, "invalid-job"),
        ({**base, "limits": {"max_parallel": True}}, "invalid-job"),
        ({**base, "limits": {"max_parallel": 0}}, "invalid-job"),
        ({**base, "job_id": " padded"}, "invalid-job"),
        ({**base, "attempt_id": "a\nb"}, "invalid-job"),
        ({**base, "goal": "  "}, "invalid-job"),
        ({**base, "metadata": {"x": "y" * 20000}}, "invalid-job"),
        ({**base, "mode": "headless"}, "invalid-job"),
    ]
    for doc, code in bad:
        with pytest.raises(_job.JobError) as err:
            _job.parse_job(doc)
        assert err.value.code == code, doc
        assert err.value.exit_code == 2
    # The work's identity ignores attempt, limits, mode and metadata.
    assert _job.parse_job(base).fingerprint() == _job.parse_job(
        {**base, "attempt_id": "b", "limits": {"max_parallel": 3}, "mode": "interactive",
         "metadata": {"k": 1}}).fingerprint()
    assert _job.parse_job(base).fingerprint() != _job.parse_job({**base, "goal": "other"}).fingerprint()


def test_job_validate_has_no_side_effects(tmp_path, repo, home, capsys, monkeypatch):
    code, out = npc(capsys, "job", "validate", "--job", str(write_job(tmp_path, repo)))
    assert code == 0 and out["job"]["job_id"] == "job-123"
    assert not (home / "task_log").exists()


# ------------------------------------------------------------------ run identity


def test_run_id_is_minted_once_and_independent_of_path(tmp_path, home):
    repo = tmp_path / "r"
    p = paths.compute_paths(repo, run_ts="2026-01-01-0000", home=home)
    q = paths.compute_paths(repo, run_ts="2026-01-01-0001", home=home)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    paths.write_run_json(p)
    paths.write_run_json(q)
    first = paths.read_run_meta(p.run_dir)["run_id"]
    assert first.startswith("run-") and p.proj_key not in first and str(repo) not in first
    assert first != paths.read_run_meta(q.run_dir)["run_id"]
    paths.write_run_json(p)  # a resume / re-init rewrites run.json …
    assert paths.read_run_meta(p.run_dir)["run_id"] == first  # … but never the identity


def test_legacy_run_json_gains_stable_run_id(tmp_path, home):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    p = paths.compute_paths(repo, run_ts="2026-01-01-0000", home=home)
    p.run_dir.mkdir(parents=True)
    legacy = p.to_run_json_dict()
    (p.run_dir / "run.json").write_text(json.dumps(legacy))  # 1.x: no run_id
    paths.write_run_json(p)
    minted = paths.read_run_meta(p.run_dir)["run_id"]
    paths.write_run_json(p)
    assert paths.read_run_meta(p.run_dir)["run_id"] == minted


def test_unique_run_ts_never_reuses_a_run_directory(tmp_path):
    from datetime import datetime
    now = datetime(2026, 9, 26, 10, 15)
    (tmp_path / "2026-09-26-1015").mkdir()
    (tmp_path / "2026-09-26-1015-2-plan-state.json").write_text("{}")
    assert paths.unique_run_ts(tmp_path, now) == "2026-09-26-1015-3"
    from npc.clean import RUN_TS_RE
    assert RUN_TS_RE.fullmatch("2026-09-26-1015-3")


def test_local_invocation_without_job_is_unchanged(repo, home, capsys, monkeypatch):
    code, init = npc(capsys, "init", cwd=repo, monkeypatch=monkeypatch)
    assert code == 0 and init["job"] is None and init["mode"] == "interactive"
    assert init["run_id"].startswith("run-")
    code, out = npc(capsys, "state", "init-run", "--plan-order", '["alpha"]', "--goal", "local goal")
    assert code == 0 and out["run_id"] == init["run_id"]
    code, st = npc(capsys, "status")
    assert st["run_id"] == init["run_id"] and st["top_status"] == "in-progress"
    code, ext = npc(capsys, "status", "--external")
    assert ext["job_id"] is None and ext["state"] == "running" and ext["run_id"] == init["run_id"]
    # A second plain init resumes the same logical run.
    code, again = npc(capsys, "init")
    assert again["needs_resume"] and again["run_id"] == init["run_id"]


# ------------------------------------------------------------------ binding, status, events


def test_job_identity_is_persisted_in_status_events_and_result(tmp_path, repo, home, capsys, monkeypatch):
    job_file = write_job(tmp_path, repo)
    code, started = npc(capsys, "run", "start", "--job", str(job_file), cwd=tmp_path, monkeypatch=monkeypatch)
    assert code == 0 and started["created"] and started["state"] == "waiting-for-agent"
    # Launcher-side call is idempotent per attempt.
    code, again = npc(capsys, "run", "start", "--job", str(job_file))
    assert again["run_id"] == started["run_id"] and not again["created"] and not again["new_attempt"]

    code, init = npc(capsys, "init", "--job", str(job_file), cwd=repo, monkeypatch=monkeypatch)
    assert code == 0 and init["run_id"] == started["run_id"] and init["mode"] == "auto"
    assert init["job"]["job_id"] == "job-123" and init["job"]["limits"] == {"max_parallel": 2}
    assert init["job"]["target_ref"] == "refs/heads/main"
    code, ext = npc(capsys, "status", "--external", "--job-id", "job-123", "--repo", str(repo),
                    cwd=tmp_path, monkeypatch=monkeypatch)
    assert ext["state"] == "planning"

    monkeypatch.chdir(repo)
    npc(capsys, "state", "init-run", "--plan-order", '["alpha"]')
    p = run_paths(repo, home, started["run_ts"])
    assert state.read_state(p.state_json)["goal"] == "Add per-IP rate limiting to authentication endpoints"
    npc(capsys, "state", "add-change", "1", "alpha")
    npc(capsys, "phase", "enter", "1", "implement")
    ev = [e for e in events(p) if e["event"] == "phase.start"][-1]
    assert (ev["job_id"], ev["attempt_id"], ev["run_id"], ev["change_id"]) == \
        ("job-123", "attempt-1", started["run_id"], "alpha")

    code, ext = npc(capsys, "status", "--external", "--job-id", "job-123", "--repo", str(repo))
    assert ext["state"] == "running" and ext["changes"]["by_phase"] == {"implementing": 1}
    assert (ext["job_id"], ext["attempt_id"], ext["run_id"]) == ("job-123", "attempt-1", started["run_id"])

    code, out = npc(capsys, "run", "abort", "--reason", "operator stop")
    assert code == 0 and out["status"] == "aborted"
    code, shown = npc(capsys, "result", "show", "--job-id", "job-123", "--repo", str(repo))
    doc = shown["result"]
    assert (doc["job_id"], doc["attempt_id"], doc["run_id"]) == ("job-123", "attempt-1", started["run_id"])
    assert doc["metadata"] == {"worker": "w-1"} and doc["status"] == "aborted"
    assert doc["reason"] == "operator stop"
    finished = [e for e in events(p) if e["event"] == "run.finished"]
    assert finished[-1]["job_id"] == "job-123" and finished[-1]["status"] == "aborted"


def test_resume_with_new_attempt_keeps_the_logical_run(tmp_path, repo, home, capsys, monkeypatch):
    job1 = write_job(tmp_path, repo)
    code, init = npc(capsys, "init", "--job", str(job1), cwd=repo, monkeypatch=monkeypatch)
    npc(capsys, "state", "init-run", "--plan-order", '["alpha", "beta"]')
    npc(capsys, "state", "set-progress", "1", "--status", "archived", "--archive-commit", "abc")
    run_id = init["run_id"]

    job2 = write_job(tmp_path, repo, "job2.json", attempt_id="attempt-2", limits={"max_parallel": 3})
    code, resumed = npc(capsys, "init", "--job", str(job2))
    assert code == 0 and resumed["run_id"] == run_id and resumed["needs_resume"]
    assert resumed["run_ts"] == init["run_ts"] and resumed["job"]["attempts"] == 2
    assert resumed["job"]["limits"] == {"max_parallel": 3}
    p = run_paths(repo, home, init["run_ts"])
    assert state.read_state(p.state_json)["run_id"] == run_id  # state reload
    attempt_events = [e for e in events(p) if e["event"] == "run.attempt"]
    assert attempt_events[-1]["attempt_id"] == "attempt-2" and attempt_events[-1]["run_id"] == run_id
    code, ext = npc(capsys, "status", "--external")
    assert ext["attempt_id"] == "attempt-2" and ext["changes"]["by_phase"] == {"delivered": 1, "pending": 1}
    # Plain local resume of the job-bound run keeps its identity and mode.
    code, plain = npc(capsys, "init")
    assert plain["run_id"] == run_id and plain["mode"] == "auto" and plain["job"]["job_id"] == "job-123"


def test_incomplete_run_never_claims_success(tmp_path, repo, home, capsys, monkeypatch):
    job = write_job(tmp_path, repo)
    npc(capsys, "init", "--job", str(job), cwd=repo, monkeypatch=monkeypatch)
    code, out = npc(capsys, "result", "show")
    assert code == 1 and out["error"] == "result-pending" and out["state"] == "planning"
    npc(capsys, "state", "init-run", "--plan-order", '["alpha"]')
    code, out = npc(capsys, "state", "finalize")
    assert code == 1 and out["error"] == "incomplete"
    code, out = npc(capsys, "result", "wait", "--timeout", "0", "--interval", "0.1")
    assert code == 1 and out["error"] == "timeout" and out["state"] == "running"
    code, out = npc(capsys, "result", "render")
    assert code == 1 and out["error"] == "result-pending"


def _terminal_state(p: paths.Paths, progress_updates: list[dict], **top) -> dict:
    def mutate(st):
        for entry, upd in zip(st["progress"], progress_updates):
            entry.update(upd)
        st.update(top)
    return state.update_state(p.state_json, p.state_md, mutate)


@pytest.mark.parametrize("entries,coverage,expected,issue_codes,verified", [
    ([{"status": "archived", "archive_commit": "c1",
       "phases": {"review-r0": {"status": "done", "blocking": 0}}}], ["--goal-complete"], "completed", [], True),
    # Force-archived with open blocking findings is not a verified delivery.
    ([{"status": "archived", "archive_commit": "c1",
       "phases": {"review-r0": {"status": "done", "blocking": 2}}}], [], "completed-with-issues",
     ["review-not-clean"], False),
    # Verified code that does not cover the goal is still not a clean completion.
    ([{"status": "archived", "archive_commit": "c1",
       "phases": {"review-r0": {"status": "done", "blocking": 0}}}], ["--goal-gap", "no admin UI"],
     "completed-with-issues", ["goal-gap"], True),
    ([{"status": "failed", "reason": "archive-failed"}], [], "failed", ["change-failed"], False),
    ([{"status": "archived", "archive_commit": "c1"}], [], "completed-with-issues", ["review-missing"], False),
])
def test_result_status_is_derived_from_authoritative_state(
        tmp_path, repo, home, capsys, monkeypatch, entries, coverage, expected, issue_codes, verified):
    job = write_job(tmp_path, repo)
    code, init = npc(capsys, "init", "--job", str(job), cwd=repo, monkeypatch=monkeypatch)
    npc(capsys, "state", "init-run", "--plan-order", json.dumps([f"c{i}" for i in range(len(entries))]))
    p = run_paths(repo, home, init["run_ts"])
    _terminal_state(p, entries)
    code, out = npc(capsys, "state", "finalize", *coverage)
    assert code == 0 and out["result_status"] == expected
    doc = json.loads(_result.result_path(p).read_text())
    assert doc["status"] == expected
    assert [i["code"] for i in doc["issues"]] == issue_codes
    assert doc["verification"]["ok"] is verified


def test_result_is_atomic_durable_and_idempotent(tmp_path, repo, home, capsys, monkeypatch):
    job = write_job(tmp_path, repo)
    code, init = npc(capsys, "init", "--job", str(job), cwd=repo, monkeypatch=monkeypatch)
    npc(capsys, "state", "init-run", "--plan-order", '["alpha"]')
    p = run_paths(repo, home, init["run_ts"])
    _terminal_state(p, [{"status": "archived", "archive_commit": "c1",
                         "phases": {"review-r0": {"status": "done", "blocking": 0}}}])
    code, out = npc(capsys, "state", "finalize", "--goal-complete")
    path = _result.result_path(p)
    first = path.read_bytes()
    assert not list(p.run_dir.glob("*.tmp"))
    doc = json.loads(first)
    assert doc["schema_version"] == 1 and doc["kind"] == "agent-spine/engineering-result"
    assert doc["target"] == {"ref": "refs/heads/main", "start_sha": git(repo, "rev-parse", "HEAD"),
                             "final_sha": git(repo, "rev-parse", "HEAD")}
    assert doc["finished_at"] and doc["goal_coverage"] == {"assessed": True, "complete": True, "gaps": []}
    # Re-rendering from state is byte-identical and does not re-announce completion.
    assert _result.build_result(p) == doc
    npc(capsys, "result", "render")
    npc(capsys, "result", "render")
    assert path.read_bytes() == first
    assert sum(e["event"] == "run.finished" for e in events(p)) == 1
    code, shown = npc(capsys, "result", "show")
    assert code == 0 and shown["result"] == doc


def test_result_for_run_finalized_by_1x_is_rendered_on_demand(tmp_path, repo, home, capsys, monkeypatch):
    npc(capsys, "init", cwd=repo, monkeypatch=monkeypatch)
    npc(capsys, "state", "init-run", "--plan-order", '["alpha"]')
    p = paths.load_paths(None)
    # 1.x finalize: status only, no result.json, no finished_at.
    _terminal_state(p, [{"status": "skipped-auto"}], status="completed-with-issues")
    assert not _result.result_path(p).exists()
    code, shown = npc(capsys, "result", "show")
    assert code == 0 and shown["result"]["status"] == "failed" and shown["result"]["job_id"] is None


# ------------------------------------------------------------------ fail safely


def test_same_job_id_with_different_work_is_refused(tmp_path, repo, home, capsys, monkeypatch):
    npc(capsys, "run", "start", "--job", str(write_job(tmp_path, repo)), cwd=tmp_path, monkeypatch=monkeypatch)
    other = write_job(tmp_path, repo, "other.json", goal="Something else entirely")
    code, out = npc(capsys, "run", "start", "--job", str(other))
    assert code == 1 and out["error"] == "job-mismatch"


def test_other_unfinished_run_blocks_a_new_job(tmp_path, repo, home, capsys, monkeypatch):
    npc(capsys, "init", cwd=repo, monkeypatch=monkeypatch)
    npc(capsys, "state", "init-run", "--plan-order", '["alpha"]')
    code, out = npc(capsys, "init", "--job", str(write_job(tmp_path, repo)))
    assert code == 1 and out["error"] == "run-conflict" and out["other_job_id"] is None
    # An unplanned stray init holds no work and does not block.
    task_log_dir = paths.task_log_dir_for(repo, home)
    assert _job.find_job_run(task_log_dir, "job-123") is None
    code, out = npc(capsys, "init", "--job", str(write_job(tmp_path, repo)), "--fresh")
    assert code == 0 and out["job"]["job_id"] == "job-123"
    # Now a second job is refused while job-123 is unfinished.
    code, out = npc(capsys, "run", "start", "--job",
                    str(write_job(tmp_path, repo, "b.json", job_id="job-456")))
    assert code == 1 and out["error"] == "run-conflict" and out["other_job_id"] == "job-123"


def test_finished_job_is_not_rerun_and_fresh_supersedes(tmp_path, repo, home, capsys, monkeypatch):
    job = write_job(tmp_path, repo)
    code, first = npc(capsys, "run", "start", "--job", str(job), cwd=tmp_path, monkeypatch=monkeypatch)
    npc(capsys, "run", "abort", "--reason", "cancelled", "--job-id", "job-123", "--repo", str(repo))
    code, out = npc(capsys, "run", "start", "--job", str(job))
    assert code == 1 and out["error"] == "job-already-finished" and out["status"] == "aborted"
    code, fresh = npc(capsys, "run", "start", "--job", str(job), "--fresh")
    assert code == 0 and fresh["created"] and fresh["run_id"] != first["run_id"]
    code, ext = npc(capsys, "status", "--external", "--job-id", "job-123", "--repo", str(repo))
    assert ext["run_id"] == fresh["run_id"] and ext["state"] == "waiting-for-agent"
    old = run_paths(repo, home, first["run_ts"])
    assert paths.read_run_meta(old.run_dir)["superseded_by"] == fresh["run_id"]
    assert json.loads(_result.result_path(old).read_text())["status"] == "aborted"


def test_corrupt_or_stale_metadata_fails_safely(tmp_path, repo, home, capsys, monkeypatch):
    job = write_job(tmp_path, repo)
    code, started = npc(capsys, "run", "start", "--job", str(job), cwd=tmp_path, monkeypatch=monkeypatch)
    task_log_dir = paths.task_log_dir_for(repo, home)
    index = _job.index_path(task_log_dir, "job-123")
    good = index.read_text()

    index.write_text("{not json")
    code, out = npc(capsys, "run", "start", "--job", str(job))
    assert code == 3 and out["error"] == "job-index-corrupt"

    # An index naming another job's run must not be followed.
    rec = json.loads(good)
    index.write_text(json.dumps({**rec, "run_id": "run-someone-else"}))
    code, out = npc(capsys, "status", "--external", "--job-id", "job-123", "--repo", str(repo))
    assert code == 3 and out["error"] == "job-index-mismatch"

    index.write_text(good)
    run_json = task_log_dir / started["run_ts"] / "run.json"
    run_json.write_text("{broken")
    code, out = npc(capsys, "result", "show", "--job-id", "job-123", "--repo", str(repo))
    assert code == 3 and out["error"] == "run-metadata-missing"

    code, out = npc(capsys, "result", "show", "--job-id", "job-unknown", "--repo", str(repo))
    assert code == 1 and out["error"] == "job-not-found"


def test_missing_index_is_recovered_from_run_metadata(tmp_path, repo, home, capsys, monkeypatch):
    job = write_job(tmp_path, repo)
    code, started = npc(capsys, "run", "start", "--job", str(job), cwd=tmp_path, monkeypatch=monkeypatch)
    task_log_dir = paths.task_log_dir_for(repo, home)
    _job.index_path(task_log_dir, "job-123").unlink()  # crash between run.json and index writes
    code, again = npc(capsys, "run", "start", "--job", str(job))
    assert code == 0 and again["run_id"] == started["run_id"] and not again["created"]
    assert _job.index_path(task_log_dir, "job-123").is_file()


def test_target_branch_must_match_the_job(tmp_path, repo, home, capsys, monkeypatch):
    git(repo, "checkout", "-qb", "feature")
    code, out = npc(capsys, "run", "start", "--job", str(write_job(tmp_path, repo)),
                    cwd=tmp_path, monkeypatch=monkeypatch)
    assert code == 3 and out["error"] == "target-mismatch" and out["actual"] == "refs/heads/feature"
    assert not (home / "task_log").exists() or not list((home / "task_log").rglob("run.json"))


def test_job_repository_must_be_this_repository(tmp_path, repo, home, capsys, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    git(other, "-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "--allow-empty", "-m", "i")
    code, out = npc(capsys, "init", "--job", str(write_job(tmp_path, repo)), cwd=other, monkeypatch=monkeypatch)
    assert code == 3 and out["error"] == "repository-mismatch"


def test_blocked_run_reopens_only_for_a_new_attempt(tmp_path, repo, home, capsys, monkeypatch):
    job = write_job(tmp_path, repo)
    code, init = npc(capsys, "init", "--job", str(job), cwd=repo, monkeypatch=monkeypatch)
    npc(capsys, "state", "init-run", "--plan-order", '["alpha"]')
    code, out = npc(capsys, "run", "abort", "--blocked", "--reason", "codex credentials missing")
    assert code == 0 and out["status"] == "blocked"
    code, ext = npc(capsys, "status", "--external")
    assert ext["state"] == "blocked" and ext["final"] and ext["resumable"]
    # The same attempt cannot silently continue past its own blocked outcome.
    code, out = npc(capsys, "init", "--job", str(job))
    assert code == 1 and out["error"] == "attempt-finished"
    code, out = npc(capsys, "run", "start", "--job", str(job))
    assert code == 0 and not out["reopened"] and out["state"] == "blocked"
    # A new attempt reopens the same run; the stale blocked result is retired.
    job2 = write_job(tmp_path, repo, "job2.json", attempt_id="attempt-2")
    code, again = npc(capsys, "init", "--job", str(job2))
    assert code == 0 and again["run_id"] == init["run_id"] and again["needs_resume"]
    p = run_paths(repo, home, init["run_ts"])
    assert not _result.result_path(p).exists() and list(p.run_dir.glob("result.blocked-*.json"))
    code, out = npc(capsys, "result", "show")
    assert code == 1 and out["state"] == "running"


def test_aborted_run_cannot_be_turned_into_blocked_or_completed_run_aborted(tmp_path, repo, home, capsys,
                                                                             monkeypatch):
    npc(capsys, "init", "--job", str(write_job(tmp_path, repo)), cwd=repo, monkeypatch=monkeypatch)
    npc(capsys, "run", "abort", "--reason", "stop")
    code, out = npc(capsys, "run", "abort", "--blocked", "--reason", "late")
    assert code == 1 and out["error"] == "run-already-finished"
    code, out = npc(capsys, "run", "abort", "--reason", "stop again")  # idempotent
    assert code == 0 and out["status"] == "aborted"


def test_checkpoint_describes_done_and_remaining_work(tmp_path, repo, home, capsys, monkeypatch):
    code, init = npc(capsys, "init", "--job", str(write_job(tmp_path, repo)), cwd=repo, monkeypatch=monkeypatch)
    npc(capsys, "state", "init-run", "--plan-order", '["alpha", "beta"]')
    p = run_paths(repo, home, init["run_ts"])
    (p.run_dir / "v3-dag-extract.json").write_text(json.dumps({"nodes": ["alpha", "beta"],
                                                              "edges": [["alpha", "beta"]]}))
    _terminal_state(p, [{"status": "archived", "archive_commit": "c1", "implement_commit": "i1",
                         "phases": {"review-r0": {"status": "done", "blocking": 0}}},
                        {"status": "ready-to-integrate",
                         "prepared": {"head": "h2", "base_commit": "b2", "patch_id": "pid",
                                      "review_round": 1, "tests": {"ok": True, "tests": "pass"}}}])
    code, out = npc(capsys, "run", "checkpoint", "--job-id", "job-123", "--repo", str(repo))
    assert code == 0 and out["completed"] == 1 and out["remaining"] == 1
    doc = json.loads(Path(out["path"]).read_text())
    assert doc["plan"] == {"order": ["alpha", "beta"], "edges": [["alpha", "beta"]]}
    assert doc["completed"] == ["alpha"] and doc["remaining"] == ["beta"]
    beta = doc["changes"][1]
    assert beta["status"] == "ready-to-integrate" and beta["prepared"]["patch_id"] == "pid"
    assert beta["tests"] == {"status": "pass", "source": "prepared", "cmd": None}
    assert doc["as_of"] == state.read_state(p.state_json)["last_updated_at"]


# ------------------------------------------------------------------ fake launcher, end to end


def test_fake_launcher_drives_a_job_to_a_verified_result(tmp_path, repo, home, capsys, monkeypatch):
    """fake Ganglion → npc run start → agent: npc init --job / plan / isolated
    implement → independent review → fix → review → publish → finalize → launcher reads result."""
    launcher_cwd = tmp_path  # the launcher is not inside the repository
    job = write_job(tmp_path, repo, job_id="job-e2e", attempt_id="attempt-7")

    # --- launcher
    code, started = npc(capsys, "run", "start", "--job", str(job), cwd=launcher_cwd, monkeypatch=monkeypatch)
    assert code == 0 and started["state"] == "waiting-for-agent"
    run_id = started["run_id"]

    # --- agent host (what /spine-run --job does, with model calls faked)
    code, init = npc(capsys, "init", "--job", str(job), cwd=repo, monkeypatch=monkeypatch)
    assert init["run_id"] == run_id
    npc(capsys, "state", "init-run", "--plan-order", '["rate-limit"]')
    npc(capsys, "state", "add-change", "1", "rate-limit")

    reviews = []
    monkeypatch.setattr(pipeline, "_portable_timeout_bin", lambda override=None: Path("/fake/timeout"))
    monkeypatch.setattr(pipeline, "_find_codex_bin", lambda override=None: "/fake/codex")

    def reviewer(**kw):  # independent reviewer: one blocking finding, then clean
        reviews.append(kw["repo_root"])
        findings = [] if len(reviews) > 1 else [
            {"id": "R1", "severity": "high", "in_scope": True, "category": "logic",
             "title": "limit is per process", "file": "limiter.txt", "line_range": "1",
             "detail": "counter not keyed by IP", "recommendation": "key by client IP"}]
        kw["review_out"].write_text(json.dumps({"verdict": "ok", "findings": findings}))
        kw["events_out"].write_text("")
        return 0

    monkeypatch.setattr(pipeline, "_codex_exec", reviewer)

    def archive(p, seq, **kw):  # openspec archive stand-in: runs under the target lock
        assert locks.try_acquire(locks.main_lock_path(p.task_log_dir), owner="probe") is None
        tip = commit(p.repo_root, "ARCHIVED.txt", "rate-limit\n")
        iso.update(p, seq, status="archived", archive_commit=tip)
        return {"ok": True, "seq": seq, "archive_commit": tip}

    monkeypatch.setattr(pipeline, "run_archive", archive)

    code, task = npc(capsys, "change", "run", "--seq", "1", "--isolated", "--handoff", "--auto")
    assert task["status"] == "needs-coder" and task["phase"] == "implement"
    wt = Path(task["worktree"])
    code, ext = npc(capsys, "status", "--external", "--job-id", "job-e2e", "--repo", str(repo),
                    cwd=launcher_cwd, monkeypatch=monkeypatch)
    assert ext["state"] == "running" and ext["changes"]["by_phase"] == {"implementing": 1}

    monkeypatch.chdir(repo)
    art = tmp_path / "coder"
    art.mkdir()
    summary = art / "summary.md"
    summary.write_text("done")
    manifest = art / "manifest.json"
    manifest.write_text(json.dumps({"files_written": [{"path": "limiter.txt"}]}))
    receipt = art / "result.txt"
    impl = commit(wt, "limiter.txt", "global counter\n")  # fake coder
    receipt.write_text(f"RESULT: commit={impl} tasks=1 tests=pass summary={summary}")
    code, task = npc(capsys, "change", "run", "--seq", "1", "--isolated", "--handoff", "--auto",
                     "--result-file", str(receipt), "--manifest", str(manifest))
    assert task["status"] == "needs-coder" and task["phase"] == "fix"
    fixed = commit(wt, "limiter.txt", "counter keyed by client IP\n")
    receipt.write_text(f"RESULT: commit={fixed} fixed=1 tests=pass summary={summary} "
                       "categories_scanned=logic regressions_added=-")
    code, out = npc(capsys, "change", "run", "--seq", "1", "--isolated", "--handoff", "--auto",
                    "--result-file", str(receipt), "--manifest", str(manifest))
    assert out["status"] == "ready-to-integrate" and reviews == [wt, wt]
    assert git(repo, "rev-parse", "HEAD") == started_head(repo, home, started)  # nothing published yet

    code, out = npc(capsys, "integrate", "--seq", "1", "--prepared")
    assert code == 0 and out["status"] == "archived"
    code, fin = npc(capsys, "state", "finalize", "--goal-complete")
    assert fin["result_status"] == "completed"

    # --- launcher reads the contract, never Spine internals
    code, shown = npc(capsys, "result", "show", "--job-id", "job-e2e", "--repo", str(repo),
                      cwd=launcher_cwd, monkeypatch=monkeypatch)
    doc = shown["result"]
    assert code == 0 and doc["status"] == "completed"
    assert (doc["job_id"], doc["attempt_id"], doc["run_id"]) == ("job-e2e", "attempt-7", run_id)
    final_sha = git(repo, "rev-parse", "refs/heads/main")
    assert doc["target"]["ref"] == "refs/heads/main" and doc["target"]["final_sha"] == final_sha
    assert doc["delivery"] == {"backend": "local", "ref": "refs/heads/main", "commit": final_sha}
    change = doc["changes"][0]
    assert change["id"] == "rate-limit" and change["status"] == "delivered"
    assert change["commit"] == final_sha and change["commits"]["implement"] == impl
    assert change["commits"]["fixes"] == [fixed] and change["review_rounds"] == 2
    assert change["review"] == {"final_blocking": 0, "clean": True}
    assert change["tests"]["status"] == "pass"
    for sha in (impl, fixed):  # reviewed commit identity preserved on the target
        assert subprocess.run(["git", "merge-base", "--is-ancestor", sha, final_sha], cwd=repo).returncode == 0
    assert doc["verification"] == {"ok": True, "reviews_clean": True, "tests": {"pass": 1}}
    assert doc["goal_coverage"]["complete"] is True and doc["issues"] == []
    code, ext = npc(capsys, "status", "--external", "--job-id", "job-e2e", "--repo", str(repo))
    assert ext["state"] == "completed" and ext["final"] and ext["result"]

    p = run_paths(repo, home, started["run_ts"])
    for event in events(p):
        assert event["run_id"] == run_id and event["job_id"] == "job-e2e"
    review_done = [e for e in events(p) if e["event"] == "review.done"]
    assert review_done and all(e["change_id"] == "rate-limit" for e in review_done)


def started_head(repo: Path, home: Path, started: dict) -> str:
    return paths.read_run_meta(run_paths(repo, home, started["run_ts"]).run_dir)["target_initial_commit"]
