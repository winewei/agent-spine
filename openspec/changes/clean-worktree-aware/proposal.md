## Why

worktree-per-run 会留下孤儿 worktree（失败/中止的 run 不被 finalize 拆除）。`npc clean` 目前只清 task_log 目录，不感知 git worktree，会留下悬空 `spine/*` worktree 与分支。需要让 clean 一并清理。

## What Changes

- `npc clean` 扩展：在清 task_log 陈旧 run 的同时，对悬空 `spine/*` worktree 执行 `git worktree remove` + 删对应分支（仅清理无 in-progress state 的、确认已陈旧的）。
- 不动有 in-progress state 的 worktree（那是可续跑的 run）。

## Capabilities

### New Capabilities
- `clean-worktree`: npc clean 感知并清理孤儿 spine worktree 的契约。

### Modified Capabilities

## Impact

- `src/npc/clean.py`：worktree 扫描 + 移除。
- `src/npc/git_ops.py`：复用 `list_worktrees` / `worktree_remove` / `branch_delete`。
- `tests/`：孤儿 worktree 被清；in-progress worktree 保留。
