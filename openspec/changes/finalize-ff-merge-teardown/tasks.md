## 1. git 封装

- [x] 1.1 `git_ops`：`merge_ff_only(canonical_root, base_branch, spine_branch)` → 成功/失败
- [x] 1.2 `worktree_remove(canonical_root, worktree_root)`、`branch_delete(canonical_root, branch)`（可注入 runner）

## 2. finalize 合并回 + 拆树

- [x] 2.1 finalize：status==completed 且 worktree 模式（run.json 有 spine_branch）时，从 canonical_repo_root 尝试 ff-only 合回 base_branch
- [x] 2.2 FF 成功 → worktree_remove + branch_delete；返回 `merged_back=true`/`worktree_removed=true`
- [x] 2.3 FF 失败 / 非 completed → 保留，返回 `merged_back=false` + spine_branch + reason
- [x] 2.4 --no-worktree run（无 spine_branch）跳过整段逻辑
- [x] 2.5 幂等容错：worktree/分支已不存在不报错

## 3. 测试

- [x] 3.1 FF 干净：合成功 + 拆树 + 删分支（临时仓库真实 worktree 周期）
- [x] 3.2 base 分叉：保留 + merged_back=false + reason
- [x] 3.3 非 completed：保留全部
- [x] 3.4 --no-worktree：跳过
- [x] 3.5 幂等：worktree 已删时不报错
- [x] 3.6 `pytest` 全绿
