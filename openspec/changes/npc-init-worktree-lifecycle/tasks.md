## 1. git worktree 封装

- [ ] 1.1 `git_ops`（或新模块）：`add_worktree(repo_root, path, branch, base_ref)` = `git worktree add -b <branch> <path> <base_ref>`，可注入 runner
- [ ] 1.2 `list_worktrees(repo_root)`：解析 `git worktree list --porcelain` → [{path, branch}]

## 2. run.json 回指字段

- [ ] 2.1 `paths.py`：`Paths` 增 canonical 回指字段（或在 init 层单独写入 run.json 扩展段）
- [ ] 2.2 `to_run_json_dict` 写出 `canonical_repo_root`/`canonical_proj_key`/`base_branch`/`spine_branch`
- [ ] 2.3 `read_run_json` 容错还原（旧 run.json 缺字段时不报错）

## 3. init 创建 worktree + 重键

- [ ] 3.1 `init_cmd.run`：探测 canonical repo_root/proj_key/base_branch → 建 `spine/<run_ts>` 分支 + worktree（`~/.spine/worktrees/<canonical_proj_key>/<run_ts>/`）
- [ ] 3.2 用 `repo_root=worktree_path` 计算 Paths；其余自举不变
- [ ] 3.3 emit 增 `worktree_root`/`spine_branch`/`canonical_proj_key`
- [ ] 3.4 `--no-worktree`（cli.py + init_cmd）：跳过 worktree，就地行为
- [ ] 3.5 worktree/分支创建失败 → exit 3，不写半残 run.json/active.json

## 4. 续跑扫描悬空 worktree

- [ ] 4.1 建新树前 `list_worktrees` 过滤 `spine/*`，按各 worktree 路径查 in-progress state
- [ ] 4.2 命中 → `needs_resume=true` + `worktree_root` 指向悬空树，不新建（多命中取 state mtime 最新）

## 5. 测试

- [ ] 5.1 默认 init 建 worktree + 分支，Paths.repo_root=worktree，emit 字段齐
- [ ] 5.2 run.json 回指字段往返一致；旧 run.json 缺字段可读
- [ ] 5.3 --no-worktree 就地行为
- [ ] 5.4 worktree 创建失败 → exit 3 无半残
- [ ] 5.5 续跑扫描：有悬空 in-progress spine worktree → needs_resume 指向它；无 → 正常新建
- [ ] 5.6 `pytest` 全绿（git 操作用临时仓库或可注入 runner）
