## 1. clean 感知 worktree

- [ ] 1.1 `clean.py`：扫描 `spine/*` worktree（复用 git_ops `list_worktrees`）
- [ ] 1.2 对无 in-progress state 的孤儿 worktree：`worktree_remove` + `branch_delete`
- [ ] 1.3 有 in-progress state 的 worktree 跳过（不删）

## 2. 测试

- [ ] 2.1 孤儿 worktree 被清（临时仓库构造孤儿树）
- [ ] 2.2 in-progress worktree 保留
- [ ] 2.3 `pytest` 全绿
