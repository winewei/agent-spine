# Agent Spine 2.0.0

2.0 turns Spine into a host-neutral engineering execution runtime. Spine accepts an engineering job, runs the full spec-to-verified-delivery lifecycle, and returns a durable, structured result that an external worker or control plane can consume. It is **not** a distributed scheduler, and local `/spine-run` use is unchanged.

The contract itself is in [runtime-contract.md](runtime-contract.md). This document records why 2.0 exists, what the architecture audit found, what changed, and how to migrate.

## 1. Architecture audit (Phase 0, against 1.9.1 @ 74dd3fd)

### What already existed and was kept as-is

- **Generation ⊥ verification.** Independent review engine (`npc review run`), `npc verify routing` enforcement, and experience injected into coders only.
- **Isolated inner loop.** One worktree and one writer per change (`isolated.py`), native-agent handoff or headless coders, and a shared concurrency budget for implement, review and fix.
- **Integration correctness.** Immutable prepared receipt bound to the reviewed HEAD. Patch identity (`git patch-id --verbatim`, derived files excluded). Merge of the latest target into the change worktree at publication. Integration-delta review when the patch changes. Combined-tree tests outside the target lock. A write-ahead publication receipt and ff-only publication in a short critical section, with `target-moved` / `target-busy` retries.
- **Recovery.** `run.json` + `active.json`, `npc resume detect`, `state repair`, and recovery of coder receipts and publication.
- **Context economy.** One-line monitor wakeups and on-demand playbook sections (1.9.1).
- **Target identity.** The start branch ref and commit are recorded in `run.json` and never silently rebound.

Most of what an external worker needs was already present as engineering state. It was not exposed as a contract.

### Local assumptions that prevented external orchestration

| Assumption in 1.x | Consequence for a launcher |
|---|---|
| Run identity = `proj_key` (absolute path) + `run_ts` (minute resolution) | no identity that survives a machine boundary; two runs created in the same minute shared a run directory, and the second inherited the first's `run.json` |
| Resume = "newest in-progress state in this repo" | a new external job would silently continue whatever run happened to be in progress |
| Completion = the agent's final chat report + `run-summary.md` (prose) | Ganglion had to ask the agent to write `DELIVERY.md` and watch for it |
| `state.status` had no stop states in practice (`aborted` was declared, never set) | an agent that gives up leaves a run "in-progress" forever; no "blocked, needs input" |
| Goal coverage lived only in the summary's prose | "is the goal done?" could not be answered structurally |
| Events carried `change_id` but no run/job correlation | events could not be forwarded or joined outside the machine |
| `npc pr open` shells out to `gh` | forge-specific; unsuitable as an engine contract |
| `state init-run` read the run mode only from the `NPC_MODE` env var | the mode of a run initialized in another shell was lost |

### Historical material that is not architectural truth

- `docs/design.md` §1–§10 describe the v0.x port of `/new-plan-changes` into `npc` (the v1 vs v2 skill, the P0–P6 roadmap). They are kept as history and are now labelled that way.
- `new-plan-changes-v2/v3` playbooks are legacy entry points. `v4` is an alias of `spine-run`.
- `npc integrate` without `--prepared` (cherry-pick onto the target worktree) is the pre-1.8.1 shared-worktree protocol. It is kept for compatibility and is not used by `spine-run`.
- `docs/principles.md` framed Spine as "not an unattended production system". 2.0 refines that sentence and keeps its reason (see §4).

## 2. What changed

| Capability | Implementation |
|---|---|
| Engineering Job contract | `npc/job.py`: EngineeringJob v1 (strict validation, work fingerprint, opaque `metadata`), a job→run index, and binding rules that refuse instead of guessing (`job-mismatch`, `job-already-finished`, `run-conflict`, `attempt-finished`, `target-mismatch`, corrupt-index errors) |
| First-class execution entry | `npc run start --job` (launcher side, no host needed) and `npc init --job` (agent side). The playbook accepts `/spine-run --job FILE`. The host agent still runs the playbook, so there is no headless rewrite. |
| Stable run identity | `run_id` minted once in `run.json`, preserved on every rewrite, mirrored into state, status, index and events. `run_ts` is collision-safe (`-2` suffix). `run.json` writes are atomic. |
| EngineeringResult | `npc/result.py`: a pure projection of state + `run.json`, written atomically by `state finalize` / `run abort`. It distinguishes `completed` / `completed-with-issues` / `failed` / `aborted` / `blocked`, and a forced, unreviewed or incomplete change never reports success. `npc result show / wait / render`. |
| Stop states | `npc run abort --reason [--blocked]`. `blocked` is reopened by the next attempt of the same job; `aborted` is final. |
| Goal coverage | `npc state finalize --goal-complete` or `--goal-gap TEXT` records the orchestrator's judgment as data |
| External status | `npc status --external [--detail]`: normalized top-level state plus per-change phase counts and correlation ids, derived from existing state. No new state machine. |
| Checkpoints | `npc run checkpoint` → `checkpoint.json`, derived from existing receipts |
| Correlated events | `events.append_event` stamps `run_id` / `job_id` / `attempt_id`. New lifecycle events: `run.created`, `run.attempt`, `run.reopened`, `run.finished`. |
| Delivery boundary | `delivery.mode` in the job (`local` only), `result.delivery` in the result. Remote publication is the launcher's. |

Not changed: review/fix loop, integration protocol, monitor, experience layer (OpenViking remains optional context, never authoritative), telemetry, config format, and every existing command's JSON contract. Commands only gained fields.

## 3. Explicit non-goals

No worker registry, global queue, distributed claim, lease or fencing epoch, worker heartbeat service, control plane, network API server, Kubernetes, Temporal, Redis, Durable Objects or DynamoDB scheduler, and no process supervision (tmux, launchd, systemd, VMs). These belong to the launcher. Spine does not replicate live state across machines; the checkpoint describes it and resuming happens on the machine that holds the worktrees. There is no GitHub or GitLab backend in this release (see runtime-contract §9).

## 4. Positioning (principles)

1.x said Spine is "a human-steered skill, not an unattended production system". The reason behind that sentence is invariant 3: hard rails scale with the absence of a human, and Spine should not grow rails it does not need. 2.0 keeps the reason and refines the claim. Spine is an engineering runtime with explicit verification boundaries. An external worker may now invoke it unattended, and it then reports a structured result instead of relying on a human reading the transcript. Distributed worker ownership and production control-plane concerns remain outside Spine. The verification boundary itself did not move: independent review, the clean-review publication gate and npc-recorded test evidence are what make an unattended result trustworthy. So nothing about review was relaxed to make unattended use possible.

## 5. Compatibility and migration

**Why this is a major version.** 2.0 introduces a new public, versioned machine-facing contract (job, result, status, run identity) and changes the product positioning. No existing command was removed or had its output changed incompatibly. No breaking CLI change was necessary.

- **Local use**: `/spine-run …` is unchanged. Local runs also get a `run_id`, a `result.json` at finalize, and `status --external`, with `job_id: null`.
- **Historical runs**: 1.x `run.json` files receive a `run_id` the first time a 2.0 command rewrites them, stable from then on. A terminal 1.x run without `result.json` gets one rendered on demand by `npc result show`. 1.x in-progress runs resume as before with plain `npc init`. They are never adopted by a job: `npc init --job` reports `run-conflict` until the run is finished, aborted, or explicitly superseded with `--fresh`.
- **State**: new optional top-level fields `run_id`, `finished_at`, `target_final_commit`, `status_reason`, `goal_coverage`, `stops`. There is a new top-level status `blocked`, which `npc clean` never removes. `state init-run` falls back to the mode recorded by `npc init` when `NPC_MODE` is unset.
- **Events**: lines gained correlation fields; existing fields are untouched.
- **Playbooks**: reinstall with `npc playbook install …`. `spine-run` requires `npc` ≥ 2.0.0.
- **Launchers**: see runtime-contract §12 to replace `DELIVERY.md`.

## 6. Tests

`tests/test_runtime_contract.py` exercises the real CLI against real git repositories. It covers the job contract, run identity across invocations, resume and state reload, 1.x `run.json` migration, persistence of job and attempt ids in status, events and results, result durability and byte-identical re-rendering, incomplete, aborted, forced and failed runs never reporting success, resuming by a new attempt, blocked reopen rules, corrupt or stale index and metadata, target and repository mismatch, supersede with `--fresh`, and the checkpoint. It also includes a fake-launcher end-to-end test: `run start` → `init --job` → isolated implement → independent review with a finding → fix → clean review → `integrate --prepared` → `finalize` → the launcher reads a `completed` result with the reviewed commits on the target. The whole pre-existing suite passes unchanged.
