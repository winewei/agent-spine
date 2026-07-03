# merge-queue-eviction

## ADDED Requirements

### Requirement: 收敛的 change 经 merge queue 串行合回 run 分支并在 run 分支上 archive
并行层内 review 收敛（blocking==0）的 change SHALL 进入 merge queue，由 npc 逐个执行：rebase `spine/<run_ts>/<change_id>` 到 run 分支 HEAD → 复跑测试（复用 `npc verify tests` 笼子）→ fast-forward 合入 run 分支（merge_status=merged）→ **在 run 分支上执行 archive**（progress.status=archived，记录 archive_commit）→ 拆除 per-change worktree。archive MUST 发生在合回之后的队列串行段（openspec/ 树是共享写，MUST NOT 在 per-change worktree 中 archive）。队列 MUST 串行处理（同一时刻至多一个 change 在合回/archive），全程不触碰主 checkout。finalize 对终态的判定（archived/failed/skipped-auto）不变。

#### Scenario: 干净合回并 archive
- **WHEN** change-a rebase 无冲突且复测通过
- **THEN** run 分支 ff 收下 a 的 commits，随后 archive 在 run 分支上执行，a 的 merge_status=merged 且 progress.status=archived（含 archive_commit），worktree 被拆除

#### Scenario: archive 不进并行段
- **WHEN** 同层 change-a、change-b 均收敛待合回
- **THEN** 两者的 archive commit 都由队列在 run 分支上串行产生，任何 per-change worktree 中不出现 archive 变更

#### Scenario: 合回不触碰主 checkout
- **WHEN** merge queue 处理任意 change
- **THEN** base_branch 与主 checkout 无任何写操作（finalize 的 ff-only 语义不变）

### Requirement: 合回失败即驱逐，npc 重放 rebase 进 worktree，coder 只解冲突
rebase 冲突或复测失败时，npc SHALL：① 还原队列侧状态（`git rebase --abort`）；② 将该 change 标记 evicted（merge_status=evicted，`eviction_count` +1）；③ **在该 change 的 per-change worktree 中重放同一 rebase 并停在冲突处**（working tree 保留冲突标记；复测失败场景则完成 rebase 仅注入失败输出）；④ 把冲突文件清单与 conflict diff（或测试失败输出）写入结构化 eviction 文件。随后的 `npc fix run` 渲染 fix prompt 时 MUST 注入该上下文，并 MUST 指令 coder：解决 working tree 中已存在的冲突标记 → `git rebase --continue` → 修复失败测试；coder MUST NOT 自行发起新的 rebase/reset。修复后该 change 重新进入 merge queue。npc MUST NOT 自动解决冲突内容。

#### Scenario: rebase 冲突驱逐回喂
- **WHEN** change-b rebase 到 run 分支时在 src/x.py 冲突
- **THEN** 队列侧 abort，b 的 worktree 中 rebase 被重放并停在 src/x.py 冲突处（含冲突标记），eviction 文件含冲突 diff，下一轮 fix prompt 注入该上下文与"解冲突 + rebase --continue"指令

#### Scenario: 复测失败同样驱逐
- **WHEN** change-b rebase 干净但 `npc verify tests` 失败
- **THEN** b 被驱逐，eviction 文件含测试失败输出，run 分支不含 b 的 commits

### Requirement: 驱逐超限转 auto-decide
同一 change 的 `eviction_count` 达到上限（`[scheduler].max_evictions`，默认 2，整数 ≥1）时，npc SHALL 停止重新排队并暴露决策点：auto 档调用 `npc auto-decide --trigger merge-evicted`（默认 action=skip，change 标记 `skipped-auto` 且 `skipped_reason=merge-evicted`，不阻塞层屏障判定）；交互档由编排者 AskUserQuestion。状态命名 MUST 沿用现有枚举 `skipped-auto`，MUST NOT 新增 `skipped` 状态。

#### Scenario: 二次驱逐触发 auto-decide
- **WHEN** change-b 第 2 次被驱逐且处于 auto 档
- **THEN** `npc auto-decide --trigger merge-evicted` 被调用，默认返回 skip，b 标记 skipped-auto（skipped_reason=merge-evicted），层内其余 change 不受影响

### Requirement: merge queue 事件进 telemetry（事件名与 payload 定稿）
npc SHALL emit 以下 telemetry 事件（进 telemetry_schema_v1.json）：`merge_enqueued`、`merge_done`、`merge_evicted`、`merge_evict_limit`，payload MUST 含 `change_id`、`dag_layer`、`eviction_count`、`reason`（conflict/test-failure/…）；change 级记录 `dag_layer` 与 `merge_evictions`，使 /spine-analyze 可量化并行收益（wall-clock 节省 vs token 增量 vs 驱逐率）。

#### Scenario: 驱逐事件被记录
- **WHEN** change-b 被驱逐一次（rebase 冲突）后成功合回
- **THEN** telemetry 含 b 的 `merge_evicted`（reason=conflict, eviction_count=1）与 `merge_done` 事件，change 级 `merge_evictions=1`
