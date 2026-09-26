# Engineering runtime contract (2.0)

This is the machine-facing contract of Agent Spine: how an external worker (Ganglion, CI, a script, another runtime) hands Spine one engineering job and learns, without reading Spine internals, what happened. It is a CLI + JSON-files contract. Spine does not run a server.

- Human-facing usage: [usage.md](usage.md)
- Full command reference: [cli.md](cli.md)
- Why 2.0 exists, the audit, and migration: [release-2.0.0.md](release-2.0.0.md)

## 1. Responsibility boundary

```
            Launcher / control plane  (e.g. Ganglion)
     job queue · workers · leases · attempts · heartbeats · retries
     environment bootstrap · process lifecycle (tmux, launchd, VM, container)
                              │
                 npc run start --job job.json          (optional pre-binding)
                 <agent host> /spine-run --job job.json
                 npc status --external / npc result show
                              │
                          Agent Spine
     plan · OpenSpec changes · DAG inside the job · isolated worktrees
     implement · independent review · fix loops · tests · integration
     engineering receipts · recovery · goal coverage · EngineeringResult
                              │
               verified commits on the local target branch
                              │
             launcher publishes (push / PR / MR) — outside Spine
```

| Spine owns | The launcher owns |
|---|---|
| Engineering planning, OpenSpec changes, the DAG inside one job | Global scheduling, job claiming, leases, fencing |
| Isolated worktrees, implement / review / fix, tests | Worker registration, heartbeats, machine lifecycle |
| Integration correctness (patch identity, combined-tree tests, short publication section) | Environment bootstrap (clone, checkout, credentials, tools) |
| Engineering state, receipts, recovery, checkpoints | Starting, stopping, restarting and timing out the agent process |
| The structured EngineeringResult | Publishing the delivered ref to a forge; reporting to its control plane |

Spine records `job_id` and `attempt_id` and echoes them everywhere, but never treats them as scheduling authority: it does not lease, fence, heartbeat, or retry. Its binding rules exist only so that engineering state is never silently shared between unrelated jobs.

## 2. Identities

| Id | Minted by | Meaning |
|---|---|---|
| `job_id` | launcher | the requested work (opaque to Spine) |
| `attempt_id` | launcher | one execution attempt of that work (opaque to Spine) |
| `run_id` | Spine | one engineering execution; stable across resume, compaction, re-init and attempts |
| `run_ts` | Spine | human/local timestamp and directory name (not an identity) |
| `proj_key` | Spine | local storage grouping derived from the repository path (not an identity) |

`run_id` looks like `run-20260926T101530Z-3f9a1c2b7e4d` (UTC time plus 48 random bits). It is written once to `run.json` and never re-derived; a 1.x run receives one the first time 2.0 touches it.

## 3. EngineeringJob (schema_version 1)

```json
{
  "schema_version": 1,
  "job_id": "job-123",
  "attempt_id": "attempt-456",
  "goal": "Add per-IP rate limiting to authentication endpoints",
  "repository": { "root": "/workspace/project", "target_ref": "main" },
  "mode": "auto",
  "limits": { "max_parallel": 4 },
  "delivery": { "mode": "local" },
  "metadata": { "worker": "w-17", "lease_epoch": 9 }
}
```

| Field | Required | Rules |
|---|---|---|
| `schema_version` | yes | `1`. Newer versions are refused with `unsupported-schema-version`. |
| `job_id`, `attempt_id` | yes | non-empty strings ≤ 200 chars, no control characters or surrounding whitespace; any other content (including non-ASCII) is fine |
| `goal` | yes | non-empty text; it may point at a requirement file inside the repository |
| `repository.root` | yes | absolute path; must be the git toplevel |
| `repository.target_ref` | no | branch to integrate into (`main` or `refs/heads/main`). Default: the branch checked out at binding. Spine verifies it and never checks it out. Preparing the checkout is the launcher's job. |
| `mode` | no | `auto` (default; no questions, decision points go through `npc auto-decide`) or `interactive` |
| `limits.max_parallel` | no | 1–64; the concurrency budget for implement, review and fix together |
| `delivery.mode` | no | `local` only (see §9). Other values are refused with `unsupported-delivery`. |
| `metadata` | no | an opaque object of at most 16 KiB, echoed in the result. Use it for correlation such as a worker id or lease epoch. Spine never reads it. |

Unknown fields are rejected (`invalid-job`) so typos cannot silently change behavior. `npc job validate --job FILE` checks a file with no side effects.

### Binding rules

A job's *work identity* is a fingerprint of `job_id`, `goal`, `repository` and `delivery`. Attempts may change `attempt_id`, `mode`, `limits` and `metadata`.

| Situation | Outcome |
|---|---|
| first binding of the job | a new run is created (`run.created` event) |
| same job, same attempt | idempotent; same run |
| same job, new attempt | same run; the attempt is appended to `run.json` `job.attempts` (`run.attempt` event) |
| same job, run `blocked`, new attempt | the run is reopened (`run.reopened`) and the stale blocked result is renamed `result.blocked-<ts>.json` |
| same job, run `blocked`, same attempt | `run start` reports `blocked` unchanged; the agent-side `init` refuses with `attempt-finished` |
| same `job_id`, different work | `job-mismatch`; use a new job_id |
| job already `completed` / `completed-with-issues` / `aborted` | `job-already-finished` (with `result_path`) |
| another job's unfinished run, or an unbound in-progress local run, is current in this repository | `run-conflict` |
| `--fresh` | explicit supersede: a new run for the job; an unfinished previous run is aborted (`superseded by …`) and marked `superseded_by` |
| checked-out branch ≠ job/recorded target, or detached HEAD | `target-mismatch` / `target-unresolved` |
| job index or run metadata corrupt or inconsistent | `job-index-corrupt` / `job-index-mismatch` / `run-metadata-missing`. Spine never guesses. |

A stray unplanned local `npc init` holds no engineering work and does not block a job.

## 4. Invocation

Recommended launcher flow:

```bash
# 1. prepare: clone/checkout target_ref, install tools, write job.json (launcher)
npc run start --job /jobs/job-123.json          # binds; state = waiting-for-agent
# 2. launch the agent host in the repository (launcher owns the process)
claude '/spine-run --job /jobs/job-123.json'    # any host: run the spine-run playbook with --job
# 3. observe (any time, from any cwd)
npc status --external --job-id job-123 --repo /workspace/project
# 4. collect
npc result show --job-id job-123 --repo /workspace/project   # exit 0 ⇔ result published
```

`npc run start` is optional because the agent-side `npc init --job` performs the same binding. Calling it first gives the launcher the `run_id` and a `waiting-for-agent` status before the agent host starts. Both calls are idempotent per attempt.

Inside the host, the playbook runs `npc init --job FILE`. That call binds or rebinds the run, takes goal, mode and `max_parallel` from the job, and resumes an in-progress run (`needs_resume: true`) instead of starting over. The intelligence layer is unchanged. The host agent still plans, dispatches, diagnoses and judges, and Spine does not become a headless Python orchestrator.

Unattended stop: when the agent cannot continue (missing dependency or credentials, operator stop), it records a structured terminal state before exiting:

```bash
npc run abort --reason "codex credentials missing" --blocked   # resumable by a new attempt
npc run abort --reason "requirement withdrawn"                 # final
```

`npc run abort --job FILE` works before planning, and even before binding (it binds, then stops).

Without `--job`, nothing changes: `/spine-run "goal"` works locally with no launcher, server or remote state. Local runs have `run_id` and a result too, with `job_id: null`.

## 5. External status

`npc status --external [--job-id ID | --run-id ID] [--repo PATH] [--detail]`

```json
{"ok": true, "schema_version": 1, "job_id": "job-123", "attempt_id": "attempt-456",
 "run_id": "run-…", "run_ts": "2026-09-26-1015", "state": "running", "final": false,
 "resumable": false, "reason": null, "goal": "…", "mode": "auto",
 "changes": {"total": 3, "by_phase": {"delivered": 1, "reviewing": 1, "fixing": 1}},
 "pending_decisions": 0, "updated_at": "2026-09-26T10:41:07Z", "result": null}
```

`state` is one of:

| state | meaning (evidence) |
|---|---|
| `waiting-for-agent` | bound, and no agent host has initialized this attempt yet |
| `planning` | an agent initialized the attempt; there is no plan (state file) yet |
| `running` | plan exists, not terminal |
| `needs-decision` | a change waits for a decision (interactive mode only) |
| `completed` · `completed-with-issues` · `failed` · `aborted` | final (see §6) |
| `blocked` | final for this attempt, `resumable: true` |

Parallel changes are in different phases at the same time, so per-change phases are counted in `changes.by_phase` rather than collapsed into one false top-level phase: `pending`, `implementing`, `reviewing`, `fixing`, `ready-to-integrate`, `integrating`, `delivered`, `failed`, `skipped`, `needs-decision`. `--detail` adds each change's internal status, commits, worktree, prepared candidate and publication receipt.

Spine cannot observe whether the agent process is alive, because the launcher owns the process. `updated_at` is the latest write to state, events or run metadata. Apply your own staleness policy on top of it.

## 6. EngineeringResult (schema_version 1)

Written atomically (tmp + fsync + rename) to `<run_dir>/result.json` when the run reaches a terminal status. `npc state finalize` and `npc run abort` write it, and it is also rendered on demand for a terminal 1.x run. It is a pure function of `run.json` and the state file: re-rendering (`npc result render`) is byte-identical and does not re-announce completion. Nothing in it is parsed from LLM prose.

```json
{
  "schema_version": 1,
  "kind": "agent-spine/engineering-result",
  "job_id": "job-123", "attempt_id": "attempt-456",
  "run_id": "run-20260926T101530Z-3f9a1c2b7e4d", "run_ts": "2026-09-26-1015",
  "status": "completed",
  "reason": null,
  "goal": "Add per-IP rate limiting to authentication endpoints",
  "mode": "auto",
  "started_at": "…", "finished_at": "…",
  "target": {"ref": "refs/heads/main", "start_sha": "…", "final_sha": "…"},
  "delivery": {"backend": "local", "ref": "refs/heads/main", "commit": "…"},
  "changes": [{
    "id": "add-ip-limiter", "seq": 1, "status": "delivered", "internal_status": "archived",
    "commit": "…", "review_rounds": 2, "review": {"final_blocking": 0, "clean": true},
    "tests": {"status": "pass", "source": "combined-tree", "cmd": "uv run pytest -q"},
    "commits": {"implement": "…", "fixes": ["…"], "integrated": "…", "archive": "…"},
    "reason": null
  }],
  "counts": {"total": 1, "delivered": 1, "failed": 0, "skipped": 0, "incomplete": 0},
  "verification": {"ok": true, "reviews_clean": true, "tests": {"pass": 1}},
  "goal_coverage": {"assessed": true, "complete": true, "gaps": []},
  "issues": [],
  "attempts": [{"attempt_id": "attempt-456", "bound_at": "…"}],
  "metadata": {"worker": "w-17", "lease_epoch": 9},
  "producer": {"name": "npc", "version": "2.0.0"},
  "artifacts": {"run_dir": "…", "state": "…", "events": "…", "summary": "…/run-summary.md", "result": "…"}
}
```

### Status

| status | when |
|---|---|
| `completed` | every change delivered with a clean final independent review, no failed tests, no goal-coverage gap |
| `completed-with-issues` | terminal, something was delivered, and at least one issue exists |
| `failed` | terminal, changes were planned, and none was delivered |
| `aborted` | stopped for good (`npc run abort`, or superseded by `--fresh`) |
| `blocked` | stopped awaiting external input; a new attempt of the same job reopens the run |

`issues[].code`: `change-failed`, `change-skipped`, `change-incomplete`, `review-not-clean` (for example a force-archived change with open blocking findings), `review-missing`, `tests-failed`, `goal-gap`, `no-changes-planned`.

Evidence rules. Review and test evidence come only from npc-recorded receipts: review phases, `last_review`, and prepared or combined-tree test receipts. A coder's self-reported `tests=pass` is never used. When Spine has no test evidence the status is `unknown`, and when no test command exists it is `skipped`; neither is reported as `pass`. `verification.ok` is true only when at least one change was delivered, every delivered change has a clean final review, and no delivered change has failing tests. Goal coverage is the orchestrating agent's structured judgment, recorded with `npc state finalize --goal-complete` or `--goal-gap TEXT` (repeatable). If it is not recorded, `goal_coverage.assessed` is `false`.

`npc result show` exits 0 with `{"ok": true, "path", "result"}` only when the result exists. Otherwise it exits 1 with `result-pending` and the current `state`. `npc result wait --timeout S --interval S` is a thin poll over the same check.

## 7. Checkpoint (schema_version 1)

`npc run checkpoint [--job-id …]` writes `<run_dir>/checkpoint.json`, a deterministic projection of current state. It is not a second state machine. It contains identity and correlation ids, `as_of` (the state's last update), target ref and start SHA, the plan (order plus DAG edges), and for every change: status, commits, review rounds and final blocking count, test evidence, worktree, prepared candidate (`head`, `base_commit`, `patch_id`, `review_round`), combined-tree candidate and publication receipt. It also lists `completed` and `remaining`. It is enough for another system to understand where engineering stands. Resuming execution still goes through `npc init --job` on the machine that holds the worktrees, because Spine does not replicate live state.

## 8. Events

`<run_dir>/run.events.jsonl` stays a local append-only log (envelope version 1, recorded as `events_schema_version` in run.json). Every line carries `ts` and `event`, plus `run_id`, `job_id` and `attempt_id` when known, and `change_id` / `change_seq` for change-scoped events. `attempt_id` is the attempt current when the event was written. A launcher can forward lines as-is without understanding them. Lifecycle events for launchers: `run.created`, `run.attempt`, `run.reopened`, `run.finished` (`status`, `result`). Everything else (`phase.*`, `review.*`, `isolated.transition`, `integrate.*`, …) is diagnostic detail.

## 9. Delivery boundary and target synchronization

The delivery backend in this release is `local`. Verified work is fast-forwarded onto the local target branch inside the shortest possible critical section, and `result.delivery` names the ref and commit. Publishing that ref (push, pull request, merge request) is the launcher's job, because it owns credentials and the forge relationship. `npc deliver` / `npc pr open` remain optional human-gated helpers for interactive use and are not part of this contract. `pr open` is GitHub-specific (`gh`), which is one reason it stays outside.

Sync-to-latest-target happens at the integration boundary, not continuously. Workers develop against the base they started from. At publication Spine merges the latest target into the change's worktree and checks that the reviewed patch identity is preserved (`git patch-id --verbatim`, excluding declared derived files). It then tests the combined tree outside the target lock, and publishes only if the target has not moved (`target-moved` otherwise, retried without redoing work). If synchronization changed the reviewed patch, the change goes back to independent review of only the integration delta. Reviewed commits keep their identity on the target, so nothing is rebased or rewritten.

A future remote backend would map onto the same boundary: the target tip becomes the remote ref, and ff-only publication becomes a non-forced push that fails when the remote moved. It is deliberately not built until it can be built cleanly.

## 10. Error codes (binding and lookup)

| exit | codes |
|---|---|
| 1 | `job-mismatch`, `job-already-finished`, `run-conflict`, `attempt-finished`, `job-busy`, `job-not-found`, `run-not-found`, `run-already-finished`, `result-pending`, `timeout` |
| 2 | `invalid-job`, `unsupported-schema-version`, `unsupported-delivery`, `job-unreadable`, `invalid_args` |
| 3 | `repository-missing`, `repository-invalid`, `repository-mismatch`, `target-mismatch`, `target-unresolved`, `job-index-corrupt`, `job-index-mismatch`, `job-ambiguous`, `run-metadata-missing`, `run-metadata-corrupt`, `state-corrupt`, `env_missing` |

Every error is a one-line JSON object `{"ok": false, "error": "<code>", "message": "…", …}` on stdout, like every other npc command.

## 11. Files

```
~/task_log/<proj_key>/
├── active.json
├── jobs/<sha256(job_id)[:32]>.json   # job index: {job_id, run_id, run_ts}
├── <run_ts>-plan-state.json          # authoritative engineering state
└── <run_ts>/
    ├── run.json                       # run_id, job binding + attempts, target ref/start commit
    ├── run.events.jsonl               # correlated event log
    ├── result.json                    # EngineeringResult (terminal runs)
    ├── checkpoint.json                # on demand
    └── run-summary.md                 # human summary (not part of the contract)
```

## 12. Migrating a launcher off DELIVERY.md (Ganglion)

Before 2.0, Ganglion's launch prompt asked the agent to write `DELIVERY.md` because Spine had no completion signal, and its Stop hook watched for that file. With 2.0:

1. Write a job file per requirement: `job_id` = requirement id, `attempt_id` = a new value per launch, `goal` = the requirement text or a pointer to the committed requirement file, `repository.root` = the prepared clone, and correlation data in `metadata`.
2. Optionally call `npc run start --job FILE` right after preparing the repository, and record the returned `run_id`.
3. Launch with `/spine-run --job FILE` and drop the DELIVERY.md sentence from the prompt.
4. In the Stop hook (or on a timer), run `npc result show --job-id ID --repo ROOT`. Exit 0 means a result exists; use `result.status`, `result.target` and `result.issues` for the notification. Exit 1 with `result-pending` means the agent stopped without finishing. That is an attempt failure the launcher may retry with a new `attempt_id`, and the retry resumes the same run.
5. Publish `result.delivery.ref` / `commit` to the forge if desired.

Ganglion does not need to understand review rounds, blocking trends, patch ids, OpenSpec archive internals, `scheduler.json` or `monitor.json`.
