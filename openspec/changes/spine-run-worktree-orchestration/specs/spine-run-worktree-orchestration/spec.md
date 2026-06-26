## ADDED Requirements

### Requirement: 编排者在 worktree 内操作并报告合回

`spine-run` 编排 skill MUST 在 init 后读取 `worktree_root`，并令后续所有 npc/coder 调用以 `cwd=worktree_root` 运行；MUST 在收尾读取 finalize 的 `merged_back`/`worktree_removed`/`spine_branch` 并在最终报告中体现。

#### Scenario: init 后 cd 进 worktree

- **WHEN** `npc init` 返回 `worktree_root`
- **THEN** skill 文档指示后续 npc 子命令与 coder spawn 一律在 `worktree_root` 内执行

#### Scenario: 续跑进入悬空 worktree

- **WHEN** init 报 `needs_resume=true` 且给出悬空 `worktree_root`
- **THEN** skill 文档指示 cd 进该 worktree 续跑，不新建

#### Scenario: 收尾报告合回结果

- **WHEN** finalize 返回 `merged_back`/`worktree_removed`/`spine_branch`
- **THEN** 最终输出报告合回结果：合成功并拆树，或分叉/未干净保留 `spine_branch` 待人 merge
