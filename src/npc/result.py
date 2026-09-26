"""Machine-facing execution contract (2.0): EngineeringResult, checkpoint, status.

Everything here is a deterministic projection of authoritative on-disk state
(``run.json`` + ``STATE_JSON``); nothing is parsed from LLM prose and nothing
here drives the engineering state machine. Three views:

- **EngineeringResult** (``<run_dir>/result.json``): written atomically when the
  run reaches a terminal status (``state finalize`` / ``run abort``). Rendering
  depends only on state + run.json, so re-rendering is byte-identical.
- **Checkpoint** (``<run_dir>/checkpoint.json``, on demand): what exists, what
  is done, what remains, which commits/reviews/tests/candidates exist.
- **External status** (``npc status --external``): a small normalized state for
  launchers, with detail only on request.

Liveness of the agent process is not observable here (the launcher owns the
process); status exposes ``updated_at`` so the launcher can apply its policy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, _io, job as _job, paths as _paths, state as _state


RESULT_FILENAME = "result.json"
CHECKPOINT_FILENAME = "checkpoint.json"
RESULT_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = 1
STATUS_SCHEMA_VERSION = 1

# state.status values after which the attempt is over (blocked may be reopened).
TERMINAL_STATE_STATUSES = frozenset({"completed", "completed-with-issues", "aborted", "blocked"})
RESULT_STATUSES = ("completed", "completed-with-issues", "failed", "aborted", "blocked")

_REVIEW_RE = re.compile(r"^review-r(\d+)$")
_FIX_RE = re.compile(r"^fix-r(\d+)$")


def result_path(p: _paths.Paths) -> Path:
    return p.run_dir / RESULT_FILENAME


def checkpoint_path(p: _paths.Paths) -> Path:
    return p.run_dir / CHECKPOINT_FILENAME


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _dump(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def target_tip(p: _paths.Paths, state: dict) -> str | None:
    """Full SHA of the integration target now (recorded once at finish)."""
    ref = state.get("target_ref") or (_paths.read_run_meta(p.run_dir) or {}).get("target_ref") or "HEAD"
    proc = subprocess.run(["git", "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                          cwd=p.repo_root, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


# ============================================================
# Per-change projection
# ============================================================


def _phase_rounds(phases: dict, regex: re.Pattern) -> list[tuple[int, dict]]:
    out = []
    for name, info in phases.items():
        m = regex.match(name)
        if m:
            out.append((int(m.group(1)), info or {}))
    return sorted(out, key=lambda x: x[0])


def change_phase(entry: dict) -> str:
    """Normalized change phase for external consumers."""
    status = entry.get("status") or "pending"
    fixed = {"archived": "delivered", "failed": "failed", "skipped-auto": "skipped",
             "needs-user-decision": "needs-decision", "ready-to-integrate": "ready-to-integrate",
             "integrated": "integrating", "needs-review": "reviewing"}
    if status in fixed:
        return fixed[status]
    for name, info in (entry.get("phases") or {}).items():
        if (info or {}).get("status") != "in-progress":
            continue
        if name == "implement":
            return "implementing"
        if _REVIEW_RE.match(name):
            return "reviewing"
        if _FIX_RE.match(name):
            return "fixing"
        if name == "archive":
            return "integrating"
    return {"implementing": "implementing", "reviewing": "reviewing",
            "in-fix-loop": "fixing"}.get(status, "pending")


def _review(entry: dict) -> dict:
    phases = entry.get("phases") or {}
    done = [(n, info) for n, info in _phase_rounds(phases, _REVIEW_RE)
            if info.get("status") == "done" and isinstance(info.get("blocking"), int)]
    last = entry.get("last_review") or {}
    if done:
        final_blocking = done[-1][1]["blocking"]
    elif isinstance(last.get("blocking"), int):
        final_blocking = last["blocking"]
    else:
        final_blocking = None
    return {"rounds": len(done), "final_blocking": final_blocking,
            "clean": final_blocking == 0, "reviewed_head": last.get("head")}


def _tests(entry: dict) -> dict:
    """Test evidence recorded by npc (never the coder's self-report)."""
    for source, receipt in (("combined-tree", (entry.get("candidate") or {}).get("tests")),
                            ("prepared", (entry.get("prepared") or {}).get("tests"))):
        if isinstance(receipt, dict) and receipt.get("tests") in ("pass", "fail", "skipped"):
            return {"status": receipt["tests"], "source": source, "cmd": receipt.get("cmd")}
    if entry.get("verify_tests_detail") or entry.get("reason") == "verify-tests-failed":
        return {"status": "fail", "source": "integrate", "cmd": None}
    return {"status": "unknown", "source": None, "cmd": None}


def _commits(entry: dict) -> dict:
    phases = entry.get("phases") or {}
    fixes = [info.get("commit") for _, info in _phase_rounds(phases, _FIX_RE) if info.get("commit")]
    return {"implement": entry.get("implement_commit"), "fixes": fixes,
            "integrated": entry.get("integrated_commit"), "archive": entry.get("archive_commit")}


def change_view(entry: dict, *, detail: bool = False) -> dict:
    commits = _commits(entry)
    review = _review(entry)
    view = {
        "id": entry.get("change_id"),
        "seq": entry.get("seq"),
        "status": change_phase(entry),
        "internal_status": entry.get("status"),
        "commit": (commits["archive"] or commits["integrated"]) if entry.get("status") == "archived" else None,
        "review_rounds": review["rounds"],
        "review": {"final_blocking": review["final_blocking"], "clean": review["clean"]},
        "tests": _tests(entry),
        "commits": commits,
        "reason": entry.get("reason"),
    }
    if detail:
        isolation = entry.get("isolation") or {}
        prepared = entry.get("prepared") or {}
        candidate = entry.get("candidate") or {}
        pending = entry.get("pending_coder") or {}
        view.update({
            "worktree": isolation.get("worktree"),
            "base_commit": isolation.get("base_commit"),
            "reviewed_head": review["reviewed_head"],
            "prepared": ({"head": prepared.get("head"), "base_commit": prepared.get("base_commit"),
                          "patch_id": prepared.get("patch_id"), "review_round": prepared.get("review_round"),
                          "tests_ok": (prepared.get("tests") or {}).get("ok")} if prepared else None),
            "candidate": ({"base": candidate.get("base"), "head": candidate.get("head"),
                           "tests_ok": (candidate.get("tests") or {}).get("ok")} if candidate else None),
            "publication": entry.get("publication") or None,
            "awaiting_coder": ({"phase": pending.get("phase"), "round": pending.get("round")}
                               if pending else None),
            "pending_decision": ((entry.get("pending_decision") or {}).get("trigger")
                                 if entry.get("pending_decision") else None),
        })
    return view


# ============================================================
# Run-level projections
# ============================================================


def _load(p: _paths.Paths) -> tuple[dict, dict | None]:
    meta = _paths.read_run_meta(p.run_dir) or {}
    state = _state.read_state(p.state_json) if p.state_json.is_file() else None
    return meta, state


def _correlation(meta: dict, state: dict | None) -> dict:
    job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
    return {"job_id": job.get("job_id"), "attempt_id": job.get("attempt_id"),
            "run_id": meta.get("run_id") or (state or {}).get("run_id"),
            "run_ts": meta.get("run_ts") or (state or {}).get("run_ts")}


def _artifacts(p: _paths.Paths) -> dict:
    return {"run_dir": str(p.run_dir), "state": str(p.state_json), "events": str(p.run_events),
            "summary": str(p.run_dir / "run-summary.md"), "result": str(result_path(p))}


def _issues(state: dict, changes: list[dict]) -> list[dict]:
    issues: list[dict] = []
    for c in changes:
        internal = c["internal_status"]
        if internal == "failed":
            issues.append({"code": "change-failed", "change_id": c["id"], "detail": c["reason"]})
        elif internal == "skipped-auto":
            issues.append({"code": "change-skipped", "change_id": c["id"], "detail": c["reason"]})
        elif internal != "archived":
            issues.append({"code": "change-incomplete", "change_id": c["id"], "detail": internal})
        else:
            if c["review"]["final_blocking"] is None:
                issues.append({"code": "review-missing", "change_id": c["id"], "detail": None})
            elif not c["review"]["clean"]:
                issues.append({"code": "review-not-clean", "change_id": c["id"],
                               "detail": f"archived with {c['review']['final_blocking']} blocking finding(s)"})
            if c["tests"]["status"] == "fail":
                issues.append({"code": "tests-failed", "change_id": c["id"], "detail": c["tests"]["cmd"]})
    coverage = state.get("goal_coverage") or {}
    for gap in coverage.get("gaps") or []:
        issues.append({"code": "goal-gap", "change_id": None, "detail": gap})
    if not changes and coverage.get("complete") is not True:
        issues.append({"code": "no-changes-planned", "change_id": None, "detail": None})
    return issues


def _result_status(state: dict, changes: list[dict], issues: list[dict]) -> str:
    top = state.get("status")
    if top in ("aborted", "blocked"):
        return top
    delivered = sum(1 for c in changes if c["internal_status"] == "archived")
    if changes and delivered == 0:
        return "failed"
    return "completed-with-issues" if issues else "completed"


def build_result(p: _paths.Paths, meta: dict | None = None, state: dict | None = None) -> dict:
    """EngineeringResult for a terminal run. Pure function of run.json + state."""
    if meta is None or state is None:
        meta, state = _load(p)
    if not state or state.get("status") not in TERMINAL_STATE_STATUSES:
        raise ValueError("run is not terminal; no EngineeringResult yet")
    changes = [change_view(e) for e in state.get("progress") or []]
    issues = _issues(state, changes)
    status = _result_status(state, changes, issues)
    delivered = [c for c in changes if c["internal_status"] == "archived"]
    tests: dict[str, int] = {}
    for c in delivered:
        tests[c["tests"]["status"]] = tests.get(c["tests"]["status"], 0) + 1
    reviews_clean = all(c["review"]["clean"] for c in delivered)
    job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
    target_ref = state.get("target_ref") or meta.get("target_ref")
    final_sha = state.get("target_final_commit")
    coverage = state.get("goal_coverage") or {}
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "kind": "agent-spine/engineering-result",
        **_correlation(meta, state),
        "status": status,
        "reason": state.get("status_reason"),
        "goal": state.get("goal") or job.get("goal"),
        "mode": state.get("mode"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "target": {"ref": target_ref,
                   "start_sha": state.get("target_initial_commit") or meta.get("target_initial_commit"),
                   "final_sha": final_sha},
        "delivery": {"backend": (job.get("delivery") or {}).get("mode", "local"),
                     "ref": target_ref, "commit": final_sha if delivered else None},
        "changes": changes,
        "counts": {"total": len(changes), "delivered": len(delivered),
                   "failed": sum(1 for c in changes if c["internal_status"] == "failed"),
                   "skipped": sum(1 for c in changes if c["internal_status"] == "skipped-auto"),
                   "incomplete": sum(1 for c in changes
                                     if c["internal_status"] not in ("archived", "failed", "skipped-auto"))},
        "verification": {"ok": bool(delivered) and reviews_clean and "fail" not in tests,
                         "reviews_clean": reviews_clean, "tests": tests},
        "goal_coverage": {"assessed": bool(coverage.get("assessed")),
                          "complete": coverage.get("complete"), "gaps": list(coverage.get("gaps") or [])},
        "issues": issues,
        "attempts": [{"attempt_id": a.get("attempt_id"), "bound_at": a.get("bound_at")}
                     for a in job.get("attempts") or []],
        "metadata": job.get("metadata") or {},
        "producer": {"name": "npc", "version": __version__},
        "artifacts": _artifacts(p),
    }


def read_result(p: _paths.Paths) -> dict | None:
    path = result_path(p)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema_version") else None


def publish_result(p: _paths.Paths) -> dict:
    """Render the result and write it atomically; unchanged content is not rewritten."""
    from . import events as _events
    doc = build_result(p)
    text = _dump(doc)
    path = result_path(p)
    try:
        unchanged = path.read_text(encoding="utf-8") == text
    except OSError:
        unchanged = False
    if not unchanged:
        _atomic_write(path, text)
        _events.append_run_event(p.run_events, {"ts": _io.now_iso(), "event": "run.finished",
                                                "status": doc["status"], "result": str(path)})
    return doc


def terminate(p: _paths.Paths, status: str, *, reason: str) -> dict:
    """Stop a run as ``aborted`` (final) or ``blocked`` (resumable) and publish its result."""
    if status not in ("aborted", "blocked"):
        raise ValueError(f"invalid terminal status {status}")
    if not p.state_json.is_file():
        # Stopped before planning: record the stop in a minimal (empty) plan.
        _state.write_state(p.state_json, p.state_md, _state.build_initial_state(p, [], None))
    now = _io.now_iso()
    current = _state.read_state(p.state_json).get("status")
    if current in ("completed", "completed-with-issues") or (current == "aborted" and status == "blocked"):
        raise _job.JobError("run-already-finished", f"run already finished as {current}",
                            status=current, result_path=str(result_path(p)))

    def mutate(st: dict) -> None:
        if st.get("status") == status and st.get("finished_at"):
            return  # idempotent: keep the first stop record
        st["status"] = status
        st["status_reason"] = reason
        st["finished_at"] = now
        st["target_final_commit"] = target_tip(p, st)

    _state.update_state(p.state_json, p.state_md, mutate)
    return publish_result(p)


def reopen(p: _paths.Paths, *, attempt_id: str) -> None:
    """Reopen a ``blocked`` run for a new attempt of the same job."""
    def mutate(st: dict) -> None:
        if st.get("status") != "blocked":
            return
        st.setdefault("stops", []).append({"status": "blocked", "reason": st.get("status_reason"),
                                           "finished_at": st.get("finished_at")})
        st["status"] = "in-progress"
        for key in ("status_reason", "finished_at", "target_final_commit"):
            st.pop(key, None)

    _state.update_state(p.state_json, p.state_md, mutate)
    # The blocked result described the previous attempt; it must not outlive the reopen.
    path = result_path(p)
    if path.is_file():
        os.replace(path, p.run_dir / f"result.blocked-{int(time.time())}.json")


def _dag(p: _paths.Paths) -> dict:
    try:
        dag = json.loads((p.run_dir / "v3-dag-extract.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"edges": []}
    return {"edges": dag.get("edges") or []} if isinstance(dag, dict) else {"edges": []}


def _top_state(meta: dict, state: dict | None, result: dict | None) -> str:
    if state is None:
        return "planning" if _job.current_attempt(meta).get("agent_attached_at") or (
            not meta.get("job") and meta.get("agent_attached_at")) else "waiting-for-agent"
    status = state.get("status")
    if status in TERMINAL_STATE_STATUSES:
        if result and result.get("status") in RESULT_STATUSES:
            return result["status"]
        changes = [change_view(e) for e in state.get("progress") or []]
        return _result_status(state, changes, _issues(state, changes))
    if any(e.get("status") == "needs-user-decision" for e in state.get("progress") or []):
        return "needs-decision"
    return "running"


def _updated_at(p: _paths.Paths) -> str | None:
    mtimes = []
    for path in (p.state_json, p.run_events, p.run_dir / _paths.RUN_JSON_FILENAME):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            pass
    if not mtimes:
        return None
    return datetime.fromtimestamp(max(mtimes), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def external_status(p: _paths.Paths, *, detail: bool = False) -> dict:
    meta, state = _load(p)
    result = read_result(p)
    top = _top_state(meta, state, result)
    progress = (state or {}).get("progress") or []
    by_phase: dict[str, int] = {}
    for entry in progress:
        phase = change_phase(entry)
        by_phase[phase] = by_phase.get(phase, 0) + 1
    final = bool(state) and state.get("status") in TERMINAL_STATE_STATUSES
    job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
    out = {
        "schema_version": STATUS_SCHEMA_VERSION,
        **_correlation(meta, state),
        "state": top,
        "final": final,
        "resumable": top == "blocked",
        "reason": (state or {}).get("status_reason"),
        "goal": (state or {}).get("goal") or job.get("goal"),
        "mode": (state or {}).get("mode") or meta.get("mode"),
        "changes": {"total": len(progress), "by_phase": by_phase},
        "pending_decisions": sum(1 for e in progress if e.get("pending_decision")),
        "updated_at": _updated_at(p),
        "result": str(result_path(p)) if result else None,
    }
    if detail:
        out["detail"] = {"internal_status": (state or {}).get("status"),
                         "changes": [change_view(e, detail=True) for e in progress]}
    return out


def build_checkpoint(p: _paths.Paths) -> dict:
    """Engineering checkpoint: identity, target, plan, per-change evidence, remaining work."""
    meta, state = _load(p)
    state = state or {}
    progress = state.get("progress") or []
    changes = [change_view(e, detail=True) for e in progress]
    job = meta.get("job") if isinstance(meta.get("job"), dict) else {}
    return {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "kind": "agent-spine/checkpoint",
        **_correlation(meta, state or None),
        "as_of": state.get("last_updated_at") or meta.get("created_at"),
        "state": _top_state(meta, state or None, read_result(p)),
        "final": state.get("status") in TERMINAL_STATE_STATUSES,
        "goal": state.get("goal") or job.get("goal"),
        "mode": state.get("mode") or meta.get("mode"),
        "target": {"ref": state.get("target_ref") or meta.get("target_ref"),
                   "start_sha": state.get("target_initial_commit") or meta.get("target_initial_commit")},
        "plan": {"order": state.get("plan_order") or [], **_dag(p)},
        "changes": changes,
        "completed": [c["id"] for c in changes if c["internal_status"] == "archived"],
        "remaining": [c["id"] for c in changes
                      if c["internal_status"] not in ("archived", "failed", "skipped-auto")],
        "artifacts": _artifacts(p),
    }


# ============================================================
# Run lookup for external callers
# ============================================================


def resolve(args: argparse.Namespace) -> _paths.Paths:
    """Locate a run by ``--job-id`` / ``--run-id`` (under ``--repo`` or cwd), else the active run."""
    job_id = getattr(args, "job_id", None)
    run_id = getattr(args, "run_id", None)
    repo = getattr(args, "repo", None)
    explicit_tld = getattr(args, "task_log_dir", None)
    if not (job_id or run_id or repo):
        return _paths.load_paths(args)
    if explicit_tld:
        task_log_dir = Path(explicit_tld)
    else:
        task_log_dir = _paths.task_log_dir_for(_paths.detect_repo_root(Path(repo) if repo else None))
    if job_id:
        run_ts = _job.find_job_run(task_log_dir, job_id)
        if run_ts is None:
            raise _job.JobError("job-not-found", f"no run is bound to job {job_id!r} in {task_log_dir}",
                                job_id=job_id)
    elif run_id:
        run_ts = _job.find_run_by_id(task_log_dir, run_id)
        if run_ts is None:
            raise _job.JobError("run-not-found", f"no run {run_id!r} in {task_log_dir}", run_id=run_id)
    else:
        run_ts = _paths.read_active(task_log_dir)
        if run_ts is None:
            raise _paths.PathsError(f"no active run in {task_log_dir}")
    p = _paths.read_run_json(_paths.run_json_path_for(task_log_dir, run_ts))
    if run_id and (_paths.read_run_meta(p.run_dir) or {}).get("run_id") != run_id:
        raise _job.JobError("run-mismatch", f"job {job_id!r} is bound to another run than {run_id!r}")
    return p


def _resolve_or_emit(args: argparse.Namespace) -> _paths.Paths | None:
    try:
        return resolve(args)
    except _job.JobError as e:
        e.emit()
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
    return None


# ============================================================
# CLI handlers
# ============================================================


def _final_result(p: _paths.Paths) -> dict | None:
    """The published result; a terminal run without one (e.g. finalized by 1.x) is rendered now."""
    doc = read_result(p)
    if doc is not None:
        return doc
    try:
        state = _state.read_state(p.state_json)
    except (OSError, json.JSONDecodeError):
        return None
    if state.get("status") in TERMINAL_STATE_STATUSES:
        return publish_result(p)
    return None


def cli_show(args: argparse.Namespace) -> None:
    import sys
    p = _resolve_or_emit(args)
    if p is None:
        return
    doc = _final_result(p)
    if doc is None:
        status = external_status(p)
        _io.emit({"ok": False, "error": "result-pending", "message": "run has not reached a terminal status",
                  "state": status["state"], "run_id": status["run_id"], "job_id": status["job_id"],
                  "attempt_id": status["attempt_id"], "result_path": str(result_path(p))})
        sys.exit(1)
    _io.emit({"ok": True, "path": str(result_path(p)), "result": doc})


def cli_wait(args: argparse.Namespace) -> None:
    """Thin observer: poll for the published result until ``--timeout``."""
    import sys
    p = _resolve_or_emit(args)
    if p is None:
        return
    deadline = time.monotonic() + max(0.0, float(args.timeout))
    interval = max(0.1, float(args.interval))
    while True:
        doc = _final_result(p)
        if doc is not None:
            _io.emit({"ok": True, "path": str(result_path(p)), "result": doc})
            return
        if time.monotonic() >= deadline:
            status = external_status(p)
            _io.emit({"ok": False, "error": "timeout", "state": status["state"], "run_id": status["run_id"],
                      "job_id": status["job_id"], "updated_at": status["updated_at"]})
            sys.exit(1)
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def cli_render(args: argparse.Namespace) -> None:
    p = _resolve_or_emit(args)
    if p is None:
        return
    try:
        doc = publish_result(p)
    except (ValueError, FileNotFoundError) as e:
        _io.emit_error("result-pending", str(e), exit_code=1)
        return
    _io.emit({"ok": True, "path": str(result_path(p)), "status": doc["status"]})


def cli_status(args: argparse.Namespace) -> None:
    p = _resolve_or_emit(args)
    if p is None:
        return
    try:
        out = external_status(p, detail=bool(getattr(args, "detail", False)))
    except (OSError, json.JSONDecodeError) as e:
        _io.emit_error("state-corrupt", str(e), exit_code=3)
        return
    _io.emit({"ok": True, **out})


def cli_checkpoint(args: argparse.Namespace) -> None:
    p = _resolve_or_emit(args)
    if p is None:
        return
    try:
        doc = build_checkpoint(p)
    except (OSError, json.JSONDecodeError) as e:
        _io.emit_error("state-corrupt", str(e), exit_code=3)
        return
    path = checkpoint_path(p)
    _atomic_write(path, _dump(doc))
    _io.emit({"ok": True, "path": str(path), "run_id": doc["run_id"], "job_id": doc["job_id"],
              "state": doc["state"], "completed": len(doc["completed"]), "remaining": len(doc["remaining"])})


def cli_abort(args: argparse.Namespace) -> None:
    """``npc run abort``: stop the run as aborted (final) or blocked (resumable)."""
    reason = (args.reason or "").strip()
    if not reason:
        _io.emit_error("invalid_args", "--reason is required", exit_code=2)
        return
    status = "blocked" if getattr(args, "blocked", False) else "aborted"
    try:
        if getattr(args, "job", None):
            job = _job.load_job(args.job)
            bound = _job.bind(_job.repository_root(job), job)
            p = bound["paths"]
        else:
            p = resolve(args)
        doc = terminate(p, status, reason=reason)
    except _job.JobError as e:
        e.emit()
        return
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    _io.emit({"ok": True, "status": doc["status"], "run_id": doc["run_id"], "job_id": doc["job_id"],
              "attempt_id": doc["attempt_id"], "result": str(result_path(p))})
