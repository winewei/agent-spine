## Why

npc 侧落地 worktree 后，编排 skill `spine-run.md` 还不知道："init 现在返回 `worktree_root`，后续所有 npc/coder 必须 cd 进 worktree 跑"，以及 finalize 现在会返回 `merged_back`/`worktree_removed` 需要在收尾报告里体现。必须同步编排文档，否则编排者仍在主 checkout 跑、隔离落空。

## What Changes

- `spine-run.md`：
  - Step 1：读 init 的 `worktree_root`，之后所有 `npc …` 与 coder 调用以 `cwd=worktree_root` 运行（cd 进 worktree）。续跑时 cd 进 init 指出的悬空 `worktree_root`。
  - Step 4：finalize 后读 `merged_back`/`worktree_removed`/`spine_branch`，在收尾输出里报告合回结果（合成功拆树 / 分叉保留待人 merge）。
  - Guardrails 增补：worktree 隔离说明 + ff-only 不自作主张推远端。
- 不改 npc 行为（纯编排文档）。

## Capabilities

### New Capabilities
- `spine-run-worktree-orchestration`: 编排者在 worktree 内操作并报告合回结果的契约。

### Modified Capabilities

## Impact

- `plugins/agent-spine/commands/spine-run.md`。
- 纯文档/skill 变更；依赖 `npc-init-worktree-lifecycle`（worktree_root 字段）与 `finalize-ff-merge-teardown`（merged_back 字段）。
