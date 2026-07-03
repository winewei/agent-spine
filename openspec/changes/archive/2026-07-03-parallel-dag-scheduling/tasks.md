# parallel-dag-scheduling — Tasks

## 1. 前置验证（硬闸门）

- [x] 1.1 验证前置提案已落地：orchestrator-check-record-result、init-crash-worktree-recovery、in-session-coder-timeout 均已 archive；任一缺失则本 change 停止实施并报告

## 2. plan-dag-analysis（npc plan dag）

- [x] 2.1 `src/npc/plan.py` 增加 touched-paths 静态提取（tasks.md/specs 正则 + glob 归一）与依赖声明解析
- [x] 2.2 实现分层算法：依赖拓扑排序 + 层内路径无交集约束 + 无路径 change 单独成层 + max_parallel 切片；环/未知依赖 → 完全串行 + `degraded_reason`
- [x] 2.3 `src/npc/cli.py` 注册 `npc plan dag` 子命令，单行 JSON 输出 `{ok, layers, serialization_reason, parallelizable_fraction, degraded_reason?}`；`[scheduler]` 节进 config.py（max_parallel 默认 3、max_evictions 默认 2，均校验整数 ≥1，telemetry 记录生效值）
- [x] 2.4 测试：同层/依赖后置/路径重叠/无路径退化/max_parallel=1 等价串行/依赖环/未知依赖/serialization_reason 点名热点（用 13 个加固提案目录作真实语料验证召回与热点诊断）

## 3. per-change worktree 与 state 扩展

- [x] 3.1 `src/npc/git_ops.py`：per-change 分支 `spine/<run_ts>/<cid>` + worktree 创建/拆除（复用孤儿回收的前缀扫描）
- [x] 3.2 `src/npc/state.py`：progress[] 增加 `dag_layer` / `merge_status` / `eviction_count` / `change_branch` / `exec_worktree`（不改 status 枚举，按 design D5 映射表）；旧 state 缺字段按串行解释
- [x] 3.3 `src/npc/state.py`：run 级互斥锁（state.lock，flock + 超时重试，超时上报不静默）覆盖全部 state 变更；并发写测试
- [x] 3.4 `src/npc/paths.py`：per-change worktree 显式绑定父 run（`--run-ts`/`--task-log-dir` 或 pointer 文件），record 落父 run 的测试
- [x] 3.5 `src/npc/resume.py`：按层重建断点，输出结构化 payload（layer/changes/blocked，含 exec_worktree）
- [x] 3.6 测试：并行层中断续跑、旧 state 向后兼容、并发 record 不丢更新

## 4. merge-queue-eviction

- [x] 4.1 新建 `src/npc/merge_queue.py`：串行队列 rebase→`npc verify tests` 复测→ff 合入 run 分支→**run 分支上 archive**（merged→archived 状态链）→拆 worktree
- [x] 4.2 驱逐路径：队列侧 abort → **npc 在 per-change worktree 重放 rebase 停在冲突处** → 结构化 eviction 文件（冲突文件清单 + diff / 测试输出），接入 fix prompt 渲染的 findings 注入通道；fix prompt 含"解冲突 + rebase --continue、禁自行 rebase/reset"指令
- [x] 4.3 `src/npc/auto_decide.py` 新增 trigger `merge-evicted`（默认 skip → skipped-auto + skipped_reason=merge-evicted）
- [x] 4.4 `src/npc/telemetry.py` + `telemetry_schema_v1.json`：事件 `merge_enqueued`/`merge_done`/`merge_evicted`/`merge_evict_limit`（payload：change_id/dag_layer/eviction_count/reason），change 级 `dag_layer`、`merge_evictions`、serialization 诊断
- [x] 4.5 测试：干净合回+archive、archive 不进并行段、冲突驱逐重放回喂闭环、复测失败驱逐、二次驱逐转 auto-decide、依赖失败下游 dep-failed skip、主 checkout 零写入

## 5. spine-run 编排契约改写

- [x] 5.1 `plugins/agent-spine/commands/spine-run.md`：Step 2 后插 `npc plan dag`；Step 3 改为按层调度（层内并行 spawn、层屏障、merge queue 触发、headless 后端降级串行）；单元素层路径与现版本等价（archive 时点：单层沿用 3c，并行层归 queue）
- [x] 5.2 Guardrails 补：并发上限、层屏障、merge queue 串行、驱逐超限转 3d、依赖失败 dep-failed skip、并行下仍逐个检查 record `.ok`、per-change worktree 内 npc 调用必须带 run 绑定
- [x] 5.3 Output 模板补 dag 层数、驱逐统计与并行节省摘要

## 6. 灰度验证

- [ ] 6.1 `max_parallel=1` 灰度 run：DAG 分析与新 state 字段全跑但不真并行，核对 telemetry 与 resume 正确性
- [ ] 6.2 真实并行 run（≥2 个不重叠 change）：验证并行 spawn、merge queue、至少一次人为制造的冲突驱逐回喂闭环
- [ ] 6.3 `/spine-analyze` 确认新指标可读，记录并行收益基线
