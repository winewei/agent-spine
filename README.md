# agent-spine

**English** | [简体中文](README.zh-CN.md)

A human-steered autonomous engineering harness that runs inside any agent CLI (Claude Code / Kimi CLI / Qwen Code / Codex / OpenCode / …) and drives work from spec to delivered code.

agent-spine splits an autonomous coding run into two layers with a strict contract between them: an **intelligence layer** of host-neutral playbooks that do scheduling and judgment, and a **deterministic execution layer** — the `npc` CLI — that does everything mechanical. The main agent session reads one-line JSON and makes decisions; it never shuttles templates, parses logs, or hand-rolls state.

## Capabilities

- **Spec-to-delivery autonomy** — hand the harness a batch of OpenSpec changes or a one-line goal; it plans, implements, reviews, fixes, and archives. Interactive mode stops at decision forks; `--auto` mode runs unattended end to end, with routine decisions delegated to `npc auto-decide`.
- **Wave-parallel batch execution** — `new-plan-changes-v4` slices active changes into dependency waves (DAG), runs one implementer per change in an isolated git worktree, then integrates serially (`npc integrate` / `npc change run`).
- **Independent review gate** — every change goes through a review→fix loop driven by a premium engine (`codex exec` or `claude -p`, pluggable), with blocking-trend tracking and stale detection. Cheap execution backends are structurally barred from reviewing their own work (`npc verify routing`).
- **Multi-model coder routing** — a provider registry routes implement/fix to any Anthropic-compatible endpoint (Kimi / Qwen / DeepSeek / …) or to `codex exec`. Credentials and models are defined once globally; each project declares only which provider to use, optionally per phase.
- **Deterministic execution layer** — state, events, prompt templates, review parsing, archiving, and git mechanics are each a single `npc` subcommand with a one-line JSON stdout and a documented exit-code contract (`0` ok / `1` business / `2` usage / `3` environment / `4` missing dependency).
- **Context economy** — sub-agent prompts render to disk; the main session passes a ~150-token stub instead of the full template (roughly 93% token saving on spawn).
- **Host neutrality** — `npc` is the only distributed artifact. Playbooks ship inside the package and are materialized into any host via `npc playbook install` (Claude Code, Codex CLI, or any directory). Each playbook carries a host-adaptation table mapping Claude Code mechanisms to generic fallbacks.
- **Externalized, resumable state** — all run state lives under `~/task_log/` with zero intrusion into the target repo. Runs resume across sessions (`npc resume detect`), self-heal on git/state drift (`npc state repair`), and feed cross-run telemetry (`npc telemetry hotspots`).

## How it works

```
┌─ Intelligence layer (playbooks, run in your agent CLI) ─────────────┐
│  spine-run            single goal / single change, full loop        │
│  new-plan-changes-v4  batch: DAG waves + worktrees   ← recommended  │
│  spine-analyze        cross-run metrics, harness self-iteration     │
│  spine-coder          coder sub-agent definition / persona          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  one-line JSON + exit codes
┌─ Deterministic execution layer ─────────────────────────────────────┐
│  npc CLI: init / state / phase / review run / fix record /          │
│  archive run / integrate / change run / playbook / telemetry / ...  │
└─────────────────────────────────────────────────────────────────────┘
```

`npc` is usable on its own (in CI or a plain terminal), but the recommended form is playbook + npc together: batch work through `new-plan-changes-v4`, single-goal autonomous loops through `spine-run`. (`new-plan-changes-v2`/`v3` are earlier iterations kept for reference.)

## Quick start

```bash
# 1) Install the npc command straight from GitHub (no clone needed)
uv tool install --force --from git+https://github.com/winewei/agent-spine.git npc
npc --version          # npc 1.7.0

# 2) Materialize playbooks into your host CLI (pick one)
npc playbook install --host claude    # Claude Code: commands/skills/agents dirs
npc playbook install --host codex     # Codex CLI: ~/.codex/prompts/
npc playbook install --dest <DIR>     # any other host: flat files, mount yourself
```

To upgrade, rerun the same two commands. For local development, install from a checkout instead: `uv tool install --force --from . npc` at the repo root.

Then, inside a git project that has an `openspec/` directory:

```text
/new-plan-changes-v4              # batch: drive all active changes in parallel waves
/spine-run add rate limiting to the auth module --auto   # single goal, fire-and-forget
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
├── <run_ts>-plan-state.json        # authoritative run state (+ .md human view)
└── <run_ts>/                       # per-run artifacts
    ├── run.json / run.events.jsonl / run-summary.md
    ├── tasks/                      # watchable background-task contracts
    └── 001-<change>/               # per-change prompts, reviews, summaries
```

## Configuration

TOML, layered and deep-merged: global `~/.config/npc/config.toml` defines providers and credentials; a project's `.npc/config.toml` only routes. Covers the review engine (`codex`/`claude`), coder provider registry, and host settings — see [docs/configuration.md](docs/configuration.md).

## Design principles

- **Decisions vs. actions** — the LLM judges and interacts; software does deterministic state, string, and subprocess work. Routine decision points sink into `npc auto-decide`.
- **The LLM never shuttles data** — subcommands resolve their own paths; templates live on disk; pipelines bundle multi-step mechanics into one call.
- **JSON + exit codes are the contract** — the main session branches on `jq` fields and `$?`, never on natural-language output.
- **Atomic, self-healing state** — every state write is tmp + `os.replace`; drift between git HEAD and task_log is repaired, not ignored.
- **Execution may be cheap; review must be premium** — third-party backends implement, they don't approve their own work.

The full version, with the architecture invariants and roadmap, is in [docs/principles.md](docs/principles.md) and [docs/design.md](docs/design.md).

## Documentation

| Doc | Contents |
|---|---|
| [docs/usage.md](docs/usage.md) | Recommended setup: CLI + playbooks + project context, end to end |
| [docs/cli.md](docs/cli.md) | Full `npc` contract: every command, stdout schema, exit codes |
| [docs/configuration.md](docs/configuration.md) | Review engine, coder providers, host config, troubleshooting |
| [docs/design.md](docs/design.md) | Architecture and design-decision record |
| [docs/principles.md](docs/principles.md) | Architecture invariants and roadmap |

## Development

```bash
uv run pytest -q            # full suite (40 files, 680+ tests)
uv run pytest --cov=npc     # with coverage
```

Tests are isolated via `tmp_path` + monkeypatch and never touch the real `~/task_log` or `~/.claude`; external binaries (`codex`, `openspec`) are faked.

## License

MIT
