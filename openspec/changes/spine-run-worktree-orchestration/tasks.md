## 1. spine-run.md worktree 编排

- [x] 1.1 Step 1：读 `worktree_root`；后续所有 npc/coder 调用以 `cwd=worktree_root` 运行
- [x] 1.2 续跑：cd 进 init 指出的悬空 `worktree_root`
- [x] 1.3 Step 4：读 finalize 的 `merged_back`/`worktree_removed`/`spine_branch`，报告合回结果（合成功拆树 / 分叉保留待人 merge）
- [x] 1.4 Guardrails：worktree 隔离 + ff-only 不推远端说明

## 2. 一致性校验

- [x] 2.1 字段名与 `npc-init-worktree-lifecycle`（worktree_root/spine_branch）、`finalize-ff-merge-teardown`（merged_back/worktree_removed）一致
- [x] 2.2 无残留"在主 checkout 跑"的旧措辞
