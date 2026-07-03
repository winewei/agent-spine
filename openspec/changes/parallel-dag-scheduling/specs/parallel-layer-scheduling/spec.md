# parallel-layer-scheduling

## ADDED Requirements

### Requirement: 编排者按 DAG 层调度，层内并行、层间屏障；并行仅限 deferred 后端
编排者（spine-run 主 session）SHALL 按 `npc plan dag` 输出的层序调度：层内全部 change 的 implement 可并行 spawn（单条消息多个 Task 调用），review-fix 循环按 change 独立推进；一层内所有 change 到达终态（progress.status ∈ {archived, failed, skipped-auto}）之前 MUST NOT 开始下一层的任何 phase。并行 implement 仅当 coder 后端为 deferred（in-session Task）时生效；headless 后端（`npc implement run` 阻塞调用）SHALL 降级为层内串行执行，dag 分层与 telemetry 字段照常记录。

#### Scenario: 同层 change 并行 implement
- **WHEN** 当前层含 change-a、change-b 且二者 `npc implement run` 均返回 deferred=true
- **THEN** 编排者在同一条消息中并行 spawn 两个 spine-coder Task，随后逐个 record

#### Scenario: headless 后端降级串行
- **WHEN** coder 后端为 headless（deferred=false）且当前层含 2 个 change
- **THEN** 编排者逐个同步执行，不并行；telemetry 仍记录 dag_layer

#### Scenario: 层屏障阻止跨层提前执行
- **WHEN** 层 1 的 change-a 仍在 review-fix 循环中
- **THEN** 层 2 的任何 change MUST NOT 触发 implement

### Requirement: 显式依赖失败向下游传播
当 change-b 显式依赖的 change-a 终态非 archived（failed 或 skipped-auto）时，b SHALL 被自动标记 skipped-auto（`skipped_reason=dep-failed`），MUST NOT 触发 implement、不占并发额度；仅路径重叠（无显式依赖声明）的 change MUST NOT 受前置失败影响。

#### Scenario: 前置失败下游自动 skip
- **WHEN** change-a 驱逐超限被标记 skipped-auto，change-b 显式声明依赖 a
- **THEN** b 不 implement，直接标记 skipped-auto（skipped_reason=dep-failed），telemetry 记录

#### Scenario: 路径重叠不传播失败
- **WHEN** change-c 与失败的 change-a 仅路径重叠、无依赖声明
- **THEN** c 照常执行

### Requirement: 并行下的 state 变更互斥
npc 的一切 state 变更（implement/review/fix record、merge queue 状态、telemetry 汇总字段）SHALL 在 run 级互斥锁（state 同目录 lock 文件，flock + 超时重试）保护下完成 read-mutate-write；锁获取超时 MUST 上报错误，MUST NOT 静默跳过写入。

#### Scenario: 并发 record 不丢更新
- **WHEN** 两个 npc 进程同时对不同 change 执行 record
- **THEN** 两次更新都反映在最终 state 中（互不覆盖）

### Requirement: per-change worktree 内的 npc 调用显式绑定父 run
per-change worktree 中执行的一切 npc 命令 SHALL 显式绑定父 run 的 state（`--run-ts`/`--task-log-dir` 或等价 pointer），MUST NOT 依赖 cwd 推断 task_log 归属；state 中该 change SHALL 记录 `exec_worktree`（执行路径），与 run worktree 的 repo_root 分离。

#### Scenario: per-change worktree 中 record 落到父 run
- **WHEN** coder 在 per-change worktree 完成实现、npc 在该 cwd 下执行 record
- **THEN** 更新写入父 run 的 state.json，而非按 cwd 解析出的其他 task_log

### Requirement: 并行层的 change 各自使用独立 per-change worktree
当层大小 > 1 时，每个 change SHALL 在从 run 分支 HEAD 新建的分支 `spine/<run_ts>/<change_id>` 及专属 worktree 中执行其全部 implement/fix；当层大小 == 1 时 SHALL 直接在 run worktree 中执行（与串行版行为一致，不建额外 worktree）。per-change worktree 在该 change 到达任一终态（progress.status ∈ {archived, failed, skipped-auto}）后 MUST 被拆除；驱逐超限（skipped-auto + merge-evicted）的 worktree 可能仍带冲突标记或未完成的 rebase，拆除前 npc SHALL 先 `git rebase --abort` 还原再拆，MUST NOT 留下处于 rebase 中间态的孤儿 worktree。

#### Scenario: 驱逐超限的 worktree 被安全拆除
- **WHEN** change-b 驱逐超限被标记 skipped-auto，其 worktree 仍停在 rebase 冲突处
- **THEN** npc 先 abort 该 rebase 再拆除 worktree 与分支，孤儿扫描无残留

#### Scenario: 并行层建独立 worktree
- **WHEN** 层含 2 个 change
- **THEN** 每个 change 的 coder 在各自的 `spine/<run_ts>/<change_id>` worktree 中工作，run worktree 不被写入

#### Scenario: 单 change 层零额外开销
- **WHEN** 层仅含 1 个 change
- **THEN** 不创建 per-change worktree，coder 直接在 run worktree 执行

### Requirement: 并行状态可续跑
state 的 `progress[]` 每项 SHALL 增加 `dag_layer`、`merge_status`（pending/queued/evicted/merged）、`eviction_count`、`change_branch`、`exec_worktree`（现有 status 枚举与 finalize 判定不变）。`npc resume detect` SHALL 定位第一个未收敛层并输出结构化 payload：`{layer, changes: [{change_id, seq, status, merge_status, phase, round, eviction_count, change_branch, exec_worktree}], blocked: [change_id...]}`（含悬空 per-change worktree 路径）；不含新字段的旧 state MUST 按串行语义解释。

#### Scenario: 并行层中断后续跑
- **WHEN** run 在层 2（含 a、b）中断，a 已 archived、b 处于 fix round 3
- **THEN** `npc resume detect` 的 payload 报告层 2 的 b（phase=fix, round=3, exec_worktree=悬空路径），a 不在待续跑清单中

#### Scenario: 旧 state 向后兼容
- **WHEN** resume 读取无 `dag_layer` 字段的旧 run state
- **THEN** 按原线性 seq 游标语义恢复，不报错
