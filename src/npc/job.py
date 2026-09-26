"""EngineeringJob contract and run binding (2.0).

An external launcher (Ganglion, CI, a shell script, a person) describes one
engineering job as a small versioned JSON document; Spine binds it to exactly
one engineering run. The identities stay distinct:

- ``job_id``      external requested work (opaque to Spine)
- ``attempt_id``  external execution attempt (opaque to Spine)
- ``run_id``      the Spine engineering execution (minted by Spine)
- ``run_ts``      human/local timestamp; ``proj_key`` local storage grouping

Spine records the ids and echoes them in status, events and the result. It
never interprets them as scheduling authority: leases, worker ownership and
retries belong to the launcher. The binding rules only protect engineering
state from being silently shared:

- same job, same or new attempt  → the same run (a new attempt is recorded)
- same job_id, different content → refused (``job-mismatch``)
- job already finished            → refused (``job-already-finished``)
- another job's (or an unbound) unfinished run is current → refused
  (``run-conflict``) unless ``--fresh``
- a ``blocked`` run is reopened by the next attempt of the same job
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import _io, locks as _locks, paths as _paths, target as _target


JOB_SCHEMA_VERSION = 1
MODES = ("auto", "interactive")
DELIVERY_MODES = ("local",)
MAX_ID_LEN = 200
MAX_PARALLEL_LIMIT = 64
MAX_METADATA_BYTES = 16384
JOBS_DIRNAME = "jobs"
JOBS_LOCK_FILENAME = ".jobs.lock"
JOBS_LOCK_WAIT_SEC = 30.0

_TOP_KEYS = {"schema_version", "job_id", "attempt_id", "goal", "repository", "mode",
             "limits", "delivery", "metadata"}
_REPOSITORY_KEYS = {"root", "target_ref"}
_LIMIT_KEYS = {"max_parallel"}
_DELIVERY_KEYS = {"mode"}

# Top-level state.status values. ``blocked`` stops an attempt but the next
# attempt of the same job may reopen it; the others end the run for good.
FINISHED_STATUSES = frozenset({"completed", "completed-with-issues", "aborted"})
RESUMABLE_STATUSES = frozenset({"in-progress", "blocked"})


class JobError(Exception):
    """Contract or binding violation; ``code`` is the JSON ``error`` field."""

    def __init__(self, code: str, message: str, *, exit_code: int = 1, **fields):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
        self.fields = fields

    def emit(self) -> None:
        import sys
        payload = {"ok": False, "error": self.code, "message": str(self), **self.fields}
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        sys.exit(self.exit_code)


@dataclass(frozen=True)
class Job:
    job_id: str
    attempt_id: str
    goal: str
    root: str
    target_ref: str | None = None
    mode: str = "auto"
    max_parallel: int | None = None
    delivery_mode: str = "local"
    metadata: dict = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Identity of the requested work. Attempt, limits, mode and metadata may
        differ between attempts of the same job; the work itself may not."""
        body = {"job_id": self.job_id, "goal": self.goal,
                "repository": {"root": self.root, "target_ref": self.target_ref},
                "delivery": {"mode": self.delivery_mode}}
        text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

    def limits(self) -> dict:
        return {"max_parallel": self.max_parallel}

    def summary(self) -> dict:
        return {"job_id": self.job_id, "attempt_id": self.attempt_id, "goal": self.goal,
                "mode": self.mode, "limits": self.limits(), "target_ref": self.target_ref,
                "delivery": {"mode": self.delivery_mode}}


# ============================================================
# Parsing / validation
# ============================================================


def _invalid(message: str) -> JobError:
    return JobError("invalid-job", message, exit_code=2)


def _identifier(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{key} must be a non-empty string")
    if len(value) > MAX_ID_LEN:
        raise _invalid(f"{key} longer than {MAX_ID_LEN} characters")
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _invalid(f"{key} must not contain control characters or surrounding whitespace")
    return value


def _unknown(obj: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise _invalid(f"unknown field(s) in {where}: {extra}")


def normalize_ref(ref: str) -> str:
    """``main`` → ``refs/heads/main``. Only branches are integration targets."""
    if ref.startswith("refs/"):
        if not ref.startswith("refs/heads/") or ref == "refs/heads/":
            raise _invalid(f"repository.target_ref must name a branch: {ref}")
        full = ref
    else:
        full = f"refs/heads/{ref}"
    name = full[len("refs/heads/"):]
    if (not name or name.startswith("-") or ".." in name or name.endswith((".", "/", ".lock"))
            or any(ch.isspace() or ch in "~^:?*[\\" for ch in name)):
        raise _invalid(f"repository.target_ref is not a valid branch name: {ref}")
    return full


def parse_job(data) -> Job:
    if not isinstance(data, dict):
        raise _invalid("job must be a JSON object")
    version = data.get("schema_version")
    if version != JOB_SCHEMA_VERSION:
        if isinstance(version, int) and not isinstance(version, bool) and version > JOB_SCHEMA_VERSION:
            raise JobError("unsupported-schema-version",
                           f"job schema_version {version} is newer than supported {JOB_SCHEMA_VERSION}",
                           exit_code=2)
        raise _invalid(f"schema_version must be {JOB_SCHEMA_VERSION}")
    _unknown(data, _TOP_KEYS, "job")
    job_id = _identifier(data, "job_id")
    attempt_id = _identifier(data, "attempt_id")
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise _invalid("goal must be a non-empty string")

    repo = data.get("repository")
    if not isinstance(repo, dict):
        raise _invalid("repository must be an object")
    _unknown(repo, _REPOSITORY_KEYS, "repository")
    root = repo.get("root")
    if not isinstance(root, str) or not root or not Path(root).is_absolute():
        raise _invalid("repository.root must be an absolute path")
    ref = repo.get("target_ref")
    if ref is not None and (not isinstance(ref, str) or not ref):
        raise _invalid("repository.target_ref must be a non-empty string when given")
    target_ref = normalize_ref(ref) if ref else None

    mode = data.get("mode", "auto")
    if mode not in MODES:
        raise _invalid(f"mode must be one of {list(MODES)}")

    limits = data.get("limits") or {}
    if not isinstance(limits, dict):
        raise _invalid("limits must be an object")
    _unknown(limits, _LIMIT_KEYS, "limits")
    max_parallel = limits.get("max_parallel")
    if max_parallel is not None and (
            isinstance(max_parallel, bool) or not isinstance(max_parallel, int)
            or not 1 <= max_parallel <= MAX_PARALLEL_LIMIT):
        raise _invalid(f"limits.max_parallel must be an integer in 1..{MAX_PARALLEL_LIMIT}")

    delivery = data.get("delivery") or {}
    if not isinstance(delivery, dict):
        raise _invalid("delivery must be an object")
    _unknown(delivery, _DELIVERY_KEYS, "delivery")
    delivery_mode = delivery.get("mode", "local")
    if delivery_mode not in DELIVERY_MODES:
        raise JobError("unsupported-delivery",
                       f"delivery.mode {delivery_mode!r} is not supported; this release delivers to the "
                       "local target branch only (the launcher publishes it)", exit_code=2)

    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise _invalid("metadata must be an object")
    if len(json.dumps(metadata, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
        raise _invalid(f"metadata larger than {MAX_METADATA_BYTES} bytes")

    return Job(job_id=job_id, attempt_id=attempt_id, goal=goal.strip(), root=root,
               target_ref=target_ref, mode=mode, max_parallel=max_parallel,
               delivery_mode=delivery_mode, metadata=metadata)


def load_job(path: str | Path) -> Job:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise JobError("job-unreadable", f"cannot read job file {path}: {e}", exit_code=2) from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise _invalid(f"job file is not valid JSON: {e}") from e
    return parse_job(data)


def repository_root(job: Job) -> Path:
    """The git toplevel named by the job; the job root must be that toplevel."""
    root = Path(job.root)
    if not root.is_dir():
        raise JobError("repository-missing", f"repository.root does not exist: {root}", exit_code=3)
    try:
        top = _paths.detect_repo_root(root)
    except _paths.PathsError as e:
        raise JobError("repository-invalid", str(e), exit_code=3) from e
    if top.resolve() != root.resolve():
        raise JobError("repository-mismatch",
                       f"repository.root {root} is not the git toplevel ({top})", exit_code=3)
    return top


# ============================================================
# Job index: job_id → run
# ============================================================


def index_path(task_log_dir: Path, job_id: str) -> Path:
    # Hashed so any opaque id maps to a safe filename; the record repeats the id.
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:32]
    return task_log_dir / JOBS_DIRNAME / f"{digest}.json"


def _write_index(task_log_dir: Path, job_id: str, run_id: str, run_ts: str) -> None:
    target = index_path(task_log_dir, job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    _paths._atomic_write_json(target, {"schema_version": 1, "job_id": job_id,
                                       "run_id": run_id, "run_ts": run_ts})


def _bound_job_id(meta: dict | None) -> str | None:
    job = (meta or {}).get("job")
    return job.get("job_id") if isinstance(job, dict) else None


def find_job_run(task_log_dir: Path, job_id: str, *, repair: bool = False) -> str | None:
    """run_ts of the run bound to ``job_id``; None when the job was never bound.

    The index is authoritative; an index that disagrees with the run's own
    metadata is an error, never a guess. Without an index (crash between the
    run.json write and the index write) the run directories are scanned.
    """
    ip = index_path(task_log_dir, job_id)
    if ip.is_file():
        try:
            rec = json.loads(ip.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise JobError("job-index-corrupt", f"unreadable job index {ip}: {e}", exit_code=3) from e
        if not isinstance(rec, dict) or rec.get("job_id") != job_id or not rec.get("run_ts"):
            raise JobError("job-index-corrupt", f"job index {ip} does not describe job {job_id!r}",
                           exit_code=3)
        run_dir = task_log_dir / rec["run_ts"]
        meta = _paths.read_run_meta(run_dir)
        if meta is None:
            raise JobError("run-metadata-missing",
                           f"job {job_id!r} points at {run_dir} whose run.json is missing or corrupt",
                           exit_code=3, run_ts=rec["run_ts"])
        if _bound_job_id(meta) != job_id or meta.get("run_id") != rec.get("run_id"):
            raise JobError("job-index-mismatch",
                           f"job index for {job_id!r} disagrees with {run_dir / 'run.json'}",
                           exit_code=3, run_ts=rec["run_ts"])
        return rec["run_ts"]
    if not task_log_dir.is_dir():
        return None
    matches = []
    for child in sorted(task_log_dir.iterdir()):
        if not child.is_dir():
            continue
        meta = _paths.read_run_meta(child)
        if _bound_job_id(meta) == job_id and not meta.get("superseded_by"):
            matches.append((child.name, meta))
    if not matches:
        return None
    if len(matches) > 1:
        raise JobError("job-ambiguous", f"several runs claim job {job_id!r}: {[m[0] for m in matches]}",
                       exit_code=3)
    run_ts, meta = matches[0]
    if repair:
        _write_index(task_log_dir, job_id, meta["run_id"], run_ts)
    return run_ts


def find_run_by_id(task_log_dir: Path, run_id: str) -> str | None:
    if not task_log_dir.is_dir():
        return None
    for child in sorted(task_log_dir.iterdir()):
        if child.is_dir() and (_paths.read_run_meta(child) or {}).get("run_id") == run_id:
            return child.name
    return None


# ============================================================
# Run status helpers
# ============================================================


def state_status(p: _paths.Paths) -> str | None:
    """Top-level state.status; None when the run has no plan (no state file yet)."""
    if not p.state_json.is_file():
        return None
    try:
        data = json.loads(p.state_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise JobError("state-corrupt", f"unreadable state {p.state_json}: {e}", exit_code=3) from e
    return data.get("status") if isinstance(data, dict) else None


def _unfinished_runs(task_log_dir: Path, repo_root: Path, home: Path | None) -> list[tuple[str, dict | None, str | None]]:
    """Runs that could still resume here: the active one and the newest in-progress one."""
    from . import resume as _resume
    seen: list[str] = []
    active = _paths.read_active(task_log_dir)
    if active:
        seen.append(active)
    latest = _resume.find_latest_in_progress(task_log_dir)
    if latest is not None:
        ts = latest.name[: -len("-plan-state.json")]
        if ts not in seen:
            seen.append(ts)
    out = []
    for ts in seen:
        p = _paths.compute_paths(repo_root, run_ts=ts, home=home)
        meta = _paths.read_run_meta(p.run_dir)
        status = state_status(p)
        if status in FINISHED_STATUSES or (meta or {}).get("superseded_by"):
            continue
        if status is None and _bound_job_id(meta) is None:
            continue  # an unplanned local init holds no engineering work
        out.append((ts, meta, status))
    return out


def current_attempt(meta: dict | None) -> dict:
    job = (meta or {}).get("job") or {}
    for attempt in reversed(job.get("attempts") or []):
        if attempt.get("attempt_id") == job.get("attempt_id"):
            return attempt
    return {}


def mark_agent_attached(p: _paths.Paths) -> None:
    """Record that an agent host initialized the current attempt (evidence for
    ``waiting-for-agent`` vs ``planning``)."""
    meta = _paths.read_run_meta(p.run_dir)
    if not meta:
        return
    now = _io.now_utc_iso()
    updates: dict = {"agent_attached_at": meta.get("agent_attached_at") or now}
    job = meta.get("job")
    if isinstance(job, dict):
        attempts = [dict(a) for a in job.get("attempts") or []]
        for attempt in attempts:
            if attempt.get("attempt_id") == job.get("attempt_id") and not attempt.get("agent_attached_at"):
                attempt["agent_attached_at"] = now
        updates["job"] = {**job, "attempts": attempts}
    _paths.write_run_json(p, **updates)


# ============================================================
# Binding
# ============================================================


def _check_target(job: Job, repo_root: Path, recorded: str | None = None) -> str:
    actual = _target.capture(repo_root)["target_ref"]
    if not actual:
        raise JobError("target-unresolved",
                       "HEAD is detached; the launcher must check out the target branch", exit_code=3)
    expected = recorded or job.target_ref
    if expected and actual != expected:
        raise JobError("target-mismatch",
                       f"checked-out branch {actual} is not the job target {expected}", exit_code=3,
                       expected=expected, actual=actual)
    return actual


def _run_event(p: _paths.Paths, event: dict) -> None:
    from . import events as _events
    _events.append_run_event(p.run_events, {"ts": _io.now_iso(), **event})


def bind(repo_root: Path, job: Job, *, home: Path | None = None, fresh: bool = False,
         agent: bool = False) -> dict:
    """Bind ``job`` to its run (creating one when needed). Returns the binding summary."""
    task_log_dir = _paths.task_log_dir_for(repo_root, home)
    task_log_dir.mkdir(parents=True, exist_ok=True)
    now = _io.now_utc_iso()
    try:
        with _locks.held(task_log_dir / JOBS_LOCK_FILENAME, owner=f"job bind {job.job_id}",
                         wait_sec=JOBS_LOCK_WAIT_SEC):
            return _bind_locked(task_log_dir, repo_root, job, home=home, fresh=fresh,
                                agent=agent, now=now)
    except _locks.LockBusy as e:
        raise JobError("job-busy", f"another process is binding a job here: {e}", exit_code=1) from e


def _bind_locked(task_log_dir: Path, repo_root: Path, job: Job, *, home, fresh, agent, now) -> dict:
    existing = find_job_run(task_log_dir, job.job_id, repair=True)
    if existing is not None:
        p = _paths.compute_paths(repo_root, run_ts=existing, home=home)
        meta = _paths.read_run_meta(p.run_dir) or {}
        bound = meta.get("job") or {}
        if bound.get("fingerprint") != job.fingerprint():
            raise JobError("job-mismatch",
                           f"job {job.job_id!r} is already bound to run {meta.get('run_id')} with "
                           "different goal/repository/delivery; use a new job_id", run_id=meta.get("run_id"))
        status = state_status(p)
        if not fresh:
            if status in FINISHED_STATUSES:
                raise JobError("job-already-finished",
                               f"job {job.job_id!r} already finished as {status} in run {meta.get('run_id')}",
                               run_id=meta.get("run_id"), status=status,
                               result_path=str(p.run_dir / "result.json"))
            return _rebind(p, meta, job, status=status, agent=agent, now=now)
        # --fresh: the previous run of this job is superseded; stop it if it was still open.
        superseded = meta.get("run_id")
        if status in RESUMABLE_STATUSES:
            from . import result as _result
            _result.terminate(p, "aborted", reason=f"superseded by a fresh run of job {job.job_id}")
    else:
        superseded = None
        if not fresh:
            for ts, meta, status in _unfinished_runs(task_log_dir, repo_root, home):
                raise JobError("run-conflict",
                               f"run {ts} ({(meta or {}).get('run_id')}) is unfinished here and belongs to "
                               f"{'job ' + repr(_bound_job_id(meta)) if _bound_job_id(meta) else 'no job'}; "
                               "finish or abort it, or pass --fresh to start a new run",
                               run_ts=ts, run_id=(meta or {}).get("run_id"),
                               other_job_id=_bound_job_id(meta), status=status)

    actual_ref = _check_target(job, repo_root)
    run_ts = _paths.unique_run_ts(task_log_dir)
    p = _paths.compute_paths(repo_root, run_ts=run_ts, home=home)
    _paths.ensure_dirs(p)
    attempt = {"attempt_id": job.attempt_id, "bound_at": now}
    if agent:
        attempt["agent_attached_at"] = now
    binding = {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job.job_id,
        "attempt_id": job.attempt_id,
        "goal": job.goal,
        "repository": {"root": job.root, "target_ref": job.target_ref or actual_ref},
        "mode": job.mode,
        "limits": job.limits(),
        "delivery": {"mode": job.delivery_mode},
        "metadata": job.metadata,
        "fingerprint": job.fingerprint(),
        "bound_at": now,
        "attempts": [attempt],
    }
    updates = {"run_id": _paths.new_run_id(), "job": binding, "mode": job.mode}
    if agent:
        updates["agent_attached_at"] = now
    if superseded:
        updates["supersedes"] = superseded
    _paths.write_run_json(p, **updates)
    run_id = updates["run_id"]
    _write_index(task_log_dir, job.job_id, run_id, run_ts)
    if superseded:
        old = _paths.compute_paths(repo_root, run_ts=existing, home=home)
        _paths.write_run_json(old, superseded_by=run_id)
    _paths.set_active(task_log_dir, run_ts)
    _run_event(p, {"event": "run.created", "goal": job.goal, "target_ref": binding["repository"]["target_ref"],
                   **({"supersedes": superseded} if superseded else {})})
    return {"run_id": run_id, "run_ts": run_ts, "paths": p, "created": True,
            "new_attempt": True, "reopened": False}


def _rebind(p: _paths.Paths, meta: dict, job: Job, *, status: str | None, agent: bool, now: str) -> dict:
    bound = dict(meta["job"])
    _check_target(job, p.repo_root, recorded=meta.get("target_ref") or bound["repository"].get("target_ref"))
    attempts = [dict(a) for a in bound.get("attempts") or []]
    known = {a.get("attempt_id") for a in attempts}
    new_attempt = job.attempt_id not in known
    if status == "blocked" and not new_attempt:
        # The attempt already ended blocked; only a new attempt may reopen the run.
        if agent:
            raise JobError("attempt-finished",
                           f"attempt {job.attempt_id!r} already stopped as blocked "
                           f"({meta.get('run_id')}); start a new attempt to resume",
                           run_id=meta.get("run_id"), status=status,
                           result_path=str(p.run_dir / "result.json"))
        return {"run_id": meta["run_id"], "run_ts": p.run_ts, "paths": p, "created": False,
                "new_attempt": False, "reopened": False}
    if new_attempt:
        attempts.append({"attempt_id": job.attempt_id, "bound_at": now})
    if agent:
        for attempt in attempts:
            if attempt.get("attempt_id") == job.attempt_id and not attempt.get("agent_attached_at"):
                attempt["agent_attached_at"] = now
    reopened = False
    if status == "blocked":
        from . import result as _result
        _result.reopen(p, attempt_id=job.attempt_id)
        reopened = True
    bound.update(attempt_id=job.attempt_id, mode=job.mode, limits=job.limits(),
                 metadata=job.metadata, attempts=attempts)
    updates = {"job": bound, "mode": job.mode}
    if agent:
        updates["agent_attached_at"] = meta.get("agent_attached_at") or now
    _paths.write_run_json(p, **updates)
    _write_index(p.task_log_dir, job.job_id, meta["run_id"], p.run_ts)
    _paths.set_active(p.task_log_dir, p.run_ts)
    if new_attempt:
        _run_event(p, {"event": "run.attempt", "previous_status": status})
    if reopened:
        _run_event(p, {"event": "run.reopened"})
    return {"run_id": meta["run_id"], "run_ts": p.run_ts, "paths": p, "created": False,
            "new_attempt": new_attempt, "reopened": reopened}


# ============================================================
# CLI: npc run start / npc job validate
# ============================================================


def cli_validate(args: argparse.Namespace) -> None:
    try:
        job = load_job(args.job)
    except JobError as e:
        e.emit()
        return
    _io.emit({"ok": True, "job": job.summary(), "fingerprint": job.fingerprint(),
              "repository": {"root": job.root}})


def cli_start(args: argparse.Namespace) -> None:
    """Launcher-side binding: no agent host involved. Idempotent per attempt."""
    from . import result as _result
    try:
        job = load_job(args.job)
        repo_root = repository_root(job)
        bound = bind(repo_root, job, fresh=bool(getattr(args, "fresh", False)))
    except JobError as e:
        e.emit()
        return
    except _paths.PathsError as e:
        _io.emit_error("run-metadata-corrupt", str(e), exit_code=3)
        return
    p = bound["paths"]
    status = _result.external_status(p)
    _io.emit({"ok": True, "run_id": bound["run_id"], "run_ts": bound["run_ts"],
              "job_id": job.job_id, "attempt_id": job.attempt_id,
              "created": bound["created"], "new_attempt": bound["new_attempt"],
              "reopened": bound["reopened"], "state": status["state"],
              "task_log_dir": str(p.task_log_dir), "run_dir": str(p.run_dir),
              "state_json": str(p.state_json), "result_path": str(p.run_dir / _result.RESULT_FILENAME)})
