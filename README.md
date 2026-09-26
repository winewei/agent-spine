# agent-spine

**English** | [简体中文](README.zh-CN.md)

A host-neutral engineering execution runtime that runs inside any agent CLI (Claude Code / Kimi CLI / Qwen Code / Codex / OpenCode / …) and drives work from spec to verified, delivered code. A person can steer it directly. An external worker such as [Ganglion](https://github.com/winewei/ganglion) or CI can also invoke it unattended with an engineering job and get back a structured result.

agent-spine splits an autonomous coding run into two layers with a strict contract between them: an **intelligence layer** of host-neutral playbooks that do scheduling and judgment, and a **deterministic execution layer** — the `npc` CLI — that does everything mechanical. Structured receipts keep transitions reliable. The main agent can inspect source, reviews and logs on demand, diagnose failures and adapt the plan without hand-rolling state.

## Capabilities

- **Spec-to-delivery autonomy** — hand the harness a batch of OpenSpec changes or a one-line goal; it plans, implements, reviews, fixes, and archives. Interactive mode stops at decision forks; `--auto` mode runs unattended end to end, with engineering decisions retained by the orchestrating agent.
- **Engineering runtime contract (2.0)**: a versioned `EngineeringJob` in (`/spine-run --job job.json`, `npc run start --job`) and a durable, atomically written `EngineeringResult` out (`result.json`, `npc result show`). The result is derived from authoritative state, never from LLM prose. It distinguishes completed, completed-with-issues, failed, aborted and blocked, and it cannot report success for unreviewed, failed or incomplete work. Each run has a stable `run_id`. External `job_id`/`attempt_id` appear in status, events and results as opaque correlation ids. `npc status --external` and `npc run checkpoint` expose progress without Spine internals. Spine is not a scheduler: leases, workers, retries and process lifecycle stay with the launcher. See [docs/runtime-contract.md](docs/runtime-contract.md).
- **Parallel development loops (1.8.1)** — each change implements, reviews and fixes in its own git worktree, using native Codex/Claude Code agents or headless coders. `npc integrate --prepared` publishes verified work to the session’s starting branch; only publication and archive serialize. Shared files are integration risks, not automatic scheduling dependencies. It accepts a one-line goal (decomposed into changes first), explicit change names, or nothing (= all active changes).
- **Background task monitoring (1.9)** — `npc monitor` tracks sub-agents, remote jobs and background commands by work evidence (artifacts, worktree diffs, probe markers). Completion, exit and deadline observations raise signals for the main session, which alone verifies and closes each task; follow output wakes the session only for new decisions, and `--format line` (1.9.1) reduces each wake-up to one plain-text line with the full detail kept on disk.
- **Independent review gate** — every change goes through a review→fix loop driven by a premium engine (`codex exec` or `claude -p`, pluggable), with blocking-trend tracking and stale detection. Cheap execution backends are structurally barred from reviewing their own work (`npc verify routing`).
- **Multi-model coder routing** — a provider registry routes implement/fix to any Anthropic-compatible endpoint (Kimi / Qwen / DeepSeek / …) or to `codex exec`. Credentials and models are defined once globally; each project declares only which provider to use, optionally per phase.
- **Deterministic execution layer** — state, events, prompt templates, review parsing, archiving, and git mechanics are each a single `npc` subcommand with a one-line JSON stdout and a documented exit-code contract (`0` ok / `1` business / `2` usage / `3` environment / `4` missing dependency).
- **Context economy** — sub-agent prompts render to disk; the main session passes a ~150-token stub instead of the full template (roughly 93% token saving on spawn). Results stay on disk and only a one-line pointer enters context; the `spine-run` playbook keeps a small resident scheduling loop and loads low-frequency branches (recovery, publication receipts, monitor events) on demand with `npc playbook show spine-run --section …` (1.9.1).
- **Host neutrality** — `npc` is the only distributed artifact. Playbooks ship inside the package and are materialized into any host via `npc playbook install` (Claude Code, Codex CLI, or any directory). Each playbook carries a host-adaptation table mapping Claude Code mechanisms to generic fallbacks.
- **Externalized, resumable state** — all run state lives under `~/task_log/` with zero intrusion into the target repo. Each run has a stable `run_id` (2.0) that survives resume, compaction and new external attempts. Runs resume across sessions (`npc resume detect`), self-heal on git/state drift (`npc state repair`), and feed cross-run telemetry (`npc telemetry hotspots`).
- **Experience layer (optional, 1.8)** — every change's coder starts from scratch, so batches keep re-discovering the same environment facts and re-committing the same review finding classes. With [OpenViking](https://github.com/volcengine/OpenViking) running locally, npc distills each archived, review-passed change's trajectory into reusable rules and injects the 2–3 most relevant ones into the next change's coder prompt. Off by default; never touches the review gate; degrades to a no-op when the server is absent. See [docs/experience.md](docs/experience.md).

## How it works

```
┌─ Intelligence layer (playbooks, run in your agent CLI) ─────────────┐
│  spine-run            goal / changes → DAG waves + worktrees  ← the entry │
│  new-plan-changes-v4  merged into spine-run (alias kept for compat)     │
│  spine-analyze        cross-run metrics, harness self-iteration     │
│  spine-coder          coder sub-agent definition / persona          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  one-line JSON + exit codes
┌─ Deterministic execution layer ─────────────────────────────────────┐
│  npc CLI: init / state / phase / review run / fix record /          │
│  archive run / integrate / change run / playbook / telemetry / ...  │
└─────────────────────────────────────────────────────────────────────┘
```

`npc` is usable on its own (in CI or a plain terminal), but the recommended form is playbook + npc together: everything goes through `spine-run`.

Unattended use adds one optional outer layer without changing the two above. The launcher owns jobs, workers, leases, the environment and the agent process. It hands Spine a job file and reads back status and result:

```
launcher (Ganglion / CI)  ──job.json──▶  agent host: /spine-run --job job.json  ──▶  npc
          ▲                                                                          │
          └──────── npc status --external · npc result show (result.json) ◀──────────┘
``` (`new-plan-changes-v2`/`v3`/`v4` are earlier iterations; `v4` is now an alias that redirects to `spine-run`.)

## Quick start

```bash
# 1) Install the npc command straight from GitHub (no clone needed)
uv tool install --force --from git+https://github.com/winewei/agent-spine.git npc
npc --version          # npc 2.0.0

# 2) Materialize playbooks into your host CLI (pick one)
npc playbook install --host claude    # Claude Code: commands/skills/agents dirs
npc playbook install --host codex     # Codex CLI: ~/.codex/skills/spine-run/ + legacy prompts
npc playbook install --dest <DIR>     # any other host: flat files, mount yourself
```

To upgrade, rerun the same two commands, pinning a release tag: `uv tool install --reinstall --from git+https://github.com/winewei/agent-spine@v<version> npc`. Do **not** install the global `npc` from a local checkout (`--from .`): work-in-progress code would leak into the CLI you rely on day to day. For development, run the checkout in place with `uv run npc ...`; `npc doctor` flags a local-directory install as `install-source: warn`.

Then, inside a git project that has an `openspec/` directory:

```text
/spine-run --auto --max-parallel 4                       # all active changes, parallel waves, fire-and-forget
/spine-run add rate limiting to the auth module --auto   # one goal: decompose into changes, then same pipeline
```

The full three-layer setup (CLI + playbooks + project context snippet) is documented in [docs/usage.md](docs/usage.md).

### Requirements

- Python ≥ 3.11, `git` (required)
- `codex` CLI — default review engine; switchable to `claude` via config
- `openspec` CLI — needed by `npc archive run` only
- `jq` recommended; `portable-timeout` is bootstrapped automatically on first `npc init`

## Supported hosts

Claude Code, Kimi CLI, Qwen Code, Codex CLI, OpenCode — and any other agent CLI via `npc playbook install --dest`. Host detection (`[host]` config or the `CLAUDECODE` env var) selects full capabilities on Claude Code and degrades gracefully elsewhere (by-cwd session lookup, no auto-authorization). Project context is read from `CLAUDE.md` with an `AGENTS.md` fallback, so non-Claude hosts only need `AGENTS.md`.

## Command surface

The LLM-facing surface is intentionally small — high-level pipeline commands that bundle a whole step into one call:

| Command | One call does |
|---|---|
| `npc init --auto` / `npc resume detect` | bootstrap or resume a run (`run.json` + `active.json` under `~/task_log/`) |
| `npc run start --job F` / `npc init --job F` | bind an external EngineeringJob to its run (launcher side / agent side) |
| `npc status --external` / `npc result show` | normalized execution state / the structured EngineeringResult |
| `npc state finalize --goal-complete\|--goal-gap T` / `npc run abort` | finish (writes `result.json`) / stop as aborted or `--blocked` |
| `npc implement record` / `npc fix record` | validate a coder's RESULT line, stamp phase timing and state |
| `npc review run --seq N --round M` | render focus → run review engine (with retry) → parse → trend → stale verdict |
| `npc archive run --seq N` | precheck → `openspec validate` → `openspec archive` → git commit |
| `npc integrate` / `npc change run` | merge worktree output into main / drive one change's inner loop |
| `npc agent prompt render` / `npc agent spawn-prompt` | write full sub-agent prompt to disk, return a thin spawn stub |
| `npc telemetry hotspots` / `npc watch` | cross-run cost hotspots / live task observation |

Low-level commands (`state`, `phase`, `review parse`, …) remain available for debugging and customization. The complete contract — every argument, stdout schema, and exit code — is in [docs/cli.md](docs/cli.md).

## Run artifacts

Everything lands under `~/task_log/<PROJ_KEY>/`, keyed by project path — nothing is written into the target repo:

```
~/task_log/<PROJ_KEY>/
├── active.json                     # points at the current run
├── index.jsonl                     # cross-run index (one JSON line per run)
├── jobs/                           # 2.0: external job_id → run index
├── <run_ts>-plan-state.json        # authoritative run state (+ .md human view)
└── <run_ts>/                       # per-run artifacts
    ├── run.json / run.events.jsonl / run-summary.md
    ├── result.json                 # 2.0: EngineeringResult (terminal runs)
    ├── tasks/                      # watchable background-task contracts
    └── 001-<change>/               # per-change prompts, reviews, summaries
```

## Configuration

TOML, layered and deep-merged: global `~/.config/npc/config.toml` defines providers and credentials; a project's `.npc/config.toml` only routes. Covers the review engine (`codex`/`claude`), coder provider registry, host settings, and the optional `[experience]` layer — see [docs/configuration.md](docs/configuration.md).

## Design principles

- **Decisions vs. actions** — the LLM judges and interacts; software does deterministic state, string, and subprocess work. Routine decision points sink into `npc auto-decide`.
- **The LLM never shuttles data** — subcommands resolve their own paths; templates live on disk; pipelines bundle multi-step mechanics into one call.
- **JSON + exit codes are the contract** — the main session branches on `jq` fields and `$?`, never on natural-language output.
- **Atomic, self-healing state** — every state write is tmp + `os.replace`; drift between git HEAD and task_log is repaired, not ignored.
- **Execution may be cheap; review must be premium** — third-party backends implement, they don't approve their own work.
- **Engineering runtime, not scheduler** — Spine owns the engineering lifecycle; distributed ownership, scheduling and process lifecycle belong to whoever launches it.

The full version, with the architecture invariants and roadmap, is in [docs/principles.md](docs/principles.md) and [docs/design.md](docs/design.md).

## Documentation

| Doc | Contents |
|---|---|
| [docs/usage.md](docs/usage.md) | Recommended setup: CLI + playbooks + project context, end to end |
| [docs/cli.md](docs/cli.md) | Full `npc` contract: every command, stdout schema, exit codes |
| [docs/configuration.md](docs/configuration.md) | Review engine, coder providers, host config, troubleshooting |
| [docs/experience.md](docs/experience.md) | Optional experience layer: the problem it solves, installing OpenViking, enabling, measuring, troubleshooting |
| [docs/runtime-contract.md](docs/runtime-contract.md) | 2.0 machine-facing contract: EngineeringJob, run identity, external status, EngineeringResult, checkpoints, events, delivery boundary |
| [docs/release-2.0.0.md](docs/release-2.0.0.md) | Why 2.0, architecture audit, compatibility and migration |
| [docs/design.md](docs/design.md) | Architecture and design-decision record (§0 is current; §1–§10 are the historical v0.x plan) |
| [docs/principles.md](docs/principles.md) | Architecture invariants and roadmap |

## Development

```bash
uv run pytest -q            # full suite (46 files, 1000+ tests)
uv run pytest --cov=npc     # with coverage
```

Tests are isolated via `tmp_path` + monkeypatch and never touch the real `~/task_log` or `~/.claude`; external binaries (`codex`, `openspec`) are faked.

## License

MIT
