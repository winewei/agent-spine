## Why

`npc init` 把一切按 `repo_root = cwd 的 git toplevel` 来键。两个 `/spine-run` 跑在同一 checkout 时硬冲突两次：(1) 多个 coder 在同一工作树并发 commit；(2) 单例 `active.json` 指针被后来的 init 劫持。设计（`docs/superpowers/specs/2026-06-23-spine-worktree-per-run-design.md`）定：每个 run 拿独立 git worktree，按 worktree 路径重新键 state——因为 `git rev-parse --show-toplevel` 在 linked worktree 内返回 worktree 路径，`detect_repo_root`/`compute_paths` 零改动即自动隔离。

## What Changes

- `npc init`（默认）：探测 canonical repo_root（主 checkout）后，在当前 HEAD 上建分支 `spine/<run_ts>` 与 worktree（落 `~/.spine/worktrees/<canonical_proj_key>/<run_ts>/`），随后用 `repo_root = worktree 路径` 计算 `Paths`。
- `run.json` 增加回指字段：`canonical_repo_root`、`canonical_proj_key`、`base_branch`、`spine_branch`。
- init JSON 输出新增 `worktree_root`、`spine_branch`、`canonical_proj_key`。
- `--no-worktree` 逃生口：保留旧的就地行为（调试 / 受信单 run）。
- **续跑探测扩展**：建新 worktree 前先 `git worktree list --porcelain` 扫 `spine/*`，命中有 in-progress state 的 worktree → `needs_resume=true` 并指向该 `worktree_root`，不建新树。

## Capabilities

### New Capabilities
- `init-worktree`: npc init 的 worktree 创建、state 重新键、run.json 回指、续跑扫描契约。

### Modified Capabilities

## Impact

- `src/npc/init_cmd.py`：worktree 创建 + Paths 重键 + run.json 回指 + emit + 续跑扫描 + `--no-worktree`。
- `src/npc/paths.py`：`run.json` 新字段序列化/反序列化（`to_run_json_dict`/`read_run_json`）。
- `src/npc/git_ops.py`（或新模块）：`git worktree add/list` 封装。
- `src/npc/cli.py`：`--no-worktree` flag。
- `tests/`：worktree 创建、重键、回指字段、续跑扫描、--no-worktree。
