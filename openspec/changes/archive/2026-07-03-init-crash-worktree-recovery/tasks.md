## 1. 意向落盘

- [x] 1.1 `npc init`：创建 worktree 前写 plan-state 骨架（status=initializing、run_ts、预定 worktree_root/spine_branch）
- [x] 1.2 Step 2 `state init-run` 把骨架升级为正式 plan state（幂等，不破坏既有语义）

## 2. 续跑扫描回收

- [x] 2.1 扫描识别 initializing 态：worktree 完好 → 返回 `needs_resume` 变体或直接复用该 worktree_root，不新建
- [x] 2.2 worktree 缺失/残破的 initializing 记录 → 标记孤儿（记录在案，init 继续新建）

## 3. clean 感知孤儿

- [x] 3.1 `npc clean` 列出孤儿 worktree（有骨架无进度且非 active）
- [x] 3.2 回收：`git worktree remove` + 删 spine 分支，task_log 账目保留；worktree/分支已不存在时幂等不报错

## 4. 测试

- [x] 4.1 崩溃窗口用例：建树后中断（无 init-run）→ 下次 init 复用旧 worktree，不新建第二棵
- [x] 4.2 正常流回归：init→init-run→finalize 全链路语义不变
- [x] 4.3 clean 列出并回收孤儿；幂等重复回收不报错
- [x] 4.4 `pytest` 全绿
