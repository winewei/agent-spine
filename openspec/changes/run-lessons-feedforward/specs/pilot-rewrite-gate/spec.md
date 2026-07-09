# pilot-rewrite-gate

## ADDED Requirements

### Requirement: 层屏障后的确定性候选判定
`npc lessons gate --layer-idx N` SHALL 在 DAG 层屏障（第 N 层全部 change 到达终态）之后被调用，只读地计算候选下游集合：`plan_order` 中 `progress[].dag_layer > N && progress[].status == "pending"` 的 change。SHALL 同时判定 `lessons.md` 中是否存在游标（`state.lessons.gate_processed_cursor`）之后的新增条目。`has_candidates` 字段 MUST 为 `候选集非空 AND 存在游标之后的新增条目` 的逻辑与；任一为否，`has_candidates` 为 `false`。

#### Scenario: 有新 lessons 且有未开始下游
- **WHEN** 层 0 收敛后新增了 2 条 lessons 条目，层 1/2 中存在 `status == "pending"` 的 change
- **THEN** `npc lessons gate --layer-idx 0` 返回 `has_candidates:true`，候选集含全部层号 > 0 的 pending change

#### Scenario: 无新增 lessons 时短路
- **WHEN** 自上次 gate 调用后 `lessons.md` 未新增任何条目
- **THEN** `has_candidates:false`，即使候选下游集合非空

#### Scenario: 已开始的下游 change 不入候选集
- **WHEN** 下游 change-d 已执行过 `npc implement run`（`implement_commit` 非空，status 非 pending）
- **THEN** change-d 不出现在候选集中，即使其 `dag_layer` 大于当前层号

### Requirement: 决策落盘与游标推进
`npc lessons gate --layer-idx N --apply --targets <csv> --decision <rewrite|skip-rewrite>` SHALL 把本次决策追加到 `state.lessons.gate_decisions`（含 `layer_idx`/`targets`/`decision`/`ts`），并将 `gate_processed_cursor` 推进到调用时 `lessons.md` 的末尾条目，使同一批已处理条目 MUST NOT 在后续层屏障重复触发 `has_candidates:true`（除非期间又有新条目追加）。

#### Scenario: 处理后游标推进
- **WHEN** 层 0 屏障处对 lessons 条目 1-2 调用 `gate --apply --decision skip-rewrite`
- **THEN** 层 1 屏障处再次调用 `gate` 时，若期间无新增条目，`has_candidates:false`

#### Scenario: 新增条目重新触发
- **WHEN** 层 0 屏障处理后，层 1 内某 change 完成 archive 并追加了新的 lessons 条目
- **THEN** 层 1 屏障处调用 `gate` 时 `has_candidates:true`（仅新条目参与判定）

### Requirement: auto 档默认不回写，交互档需人确认
auto 档（`--auto` 运行模式）下，编排者 SHALL NOT 调用 `AskUserQuestion`；对 `has_candidates:true` 的情形，SHALL 直接以 `--decision skip-rewrite` 调用 `gate --apply`，不修订任何下游 change 的 tasks/design。交互档下，编排者 SHALL 用 `AskUserQuestion` 将候选下游集合与 lessons 摘要呈现给用户，按用户选择的子集**先**调用 `gate --apply --decision rewrite --targets <csv>`；该调用按 Requirement「apply 时 targets 必须是当前候选集子集，否则确定性拒绝」完成只读校验并落盘决策/推进游标，**只有其返回成功后**，编排者才对每个 targets 触发 write 三件套修订流程（未选中的目标或用户全部拒绝时直接以 `decision=skip-rewrite` 调用 `gate --apply`，不触发任何 write 三件套）。若 `gate --apply --decision rewrite` 返回结构化错误（如 `target-not-candidate`），编排者 MUST NOT 对任何 targets 触发 write 三件套。

#### Scenario: auto 档默认跳过
- **WHEN** 运行于 `--auto` 模式且 `has_candidates:true`
- **THEN** 编排者不调用 `AskUserQuestion`，直接 `gate --apply --decision skip-rewrite`，候选下游 change 的 artifact 不被修改

#### Scenario: 交互档人工确认后回写
- **WHEN** 运行于交互模式，用户在 `AskUserQuestion` 中选择对候选下游 change-e 执行修订
- **THEN** 编排者先以 `--decision rewrite --targets change-e` 调用 `gate --apply`；该调用校验 change-e 属于候选集并成功落盘决策、推进游标后，编排者才对 change-e 触发 write 三件套修订流程

#### Scenario: apply 校验失败时不触发 write
- **WHEN** 运行于交互模式，用户选择的目标在编排者调用 `gate --apply --decision rewrite --targets <csv>` 时被判定不属于候选集（返回 `{ok:false, error:"target-not-candidate"}`）
- **THEN** 编排者不对任何 targets 触发 write 三件套，直接将错误呈现并按既有失败路径处理（不修改任何下游 change 目录）

### Requirement: 回写执行复用既有 spec write 三件套，边界不扩大
被批准回写的下游 change SHALL 通过 `npc spec write run --change <id> --lessons-path <lessons.md 绝对路径>` 渲染修订 prompt（不传 `--goal`，视为既有"补全/修复"分支），由 `spine-spec-writer` 执行，`npc spec write record` 装订。装订前的越界拦截（`out_of_scope_changes` / `unexpected_commit`）与既有 `spec write record` 契约完全一致，MUST NOT 为 lessons 回写场景放宽。

#### Scenario: 回写越界被拦截
- **WHEN** `spine-spec-writer` 在 lessons 回写任务中修改了目标 change 目录之外的文件
- **THEN** `npc spec write record` 返回 `out_of_scope_changes`，不装订

#### Scenario: 省略 --lessons-path 时行为不变
- **WHEN** `npc spec write run --change <id>` 未传 `--lessons-path`
- **THEN** 渲染出的 prompt 与本 change 之前的行为逐字等价，不含任何 lessons 段落

### Requirement: 候选集为空或无新增条目时零开销跳过
`has_candidates:false` 时，编排者 SHALL 直接跳过整个闸口分支（不调用 `AskUserQuestion`、不触发任何 write 三件套调用），继续按既有层屏障语义推进下一层。

#### Scenario: 无候选时不产生任何交互或调用
- **WHEN** `npc lessons gate --layer-idx N` 返回 `has_candidates:false`
- **THEN** 编排者不发起 `AskUserQuestion`，不调用 `npc spec write run`，直接进入下一层调度

### Requirement: apply 时 targets 必须是当前候选集子集，否则确定性拒绝
`npc lessons gate --layer-idx N --apply --targets <csv> --decision rewrite` SHALL 在写入 `gate_decisions` / 推进游标之前，校验 `--targets` 中每个 change_id 都属于本次 `--layer-idx N` 计算出的候选集（即 `dag_layer > N && status == "pending"`）。若存在任一 targets 不满足此条件（已执行过 `implement run`、已终态、`dag_layer <= N`、或 `plan_order` 中不存在的未知 change_id），命令 SHALL 返回结构化错误（不写入 `gate_decisions`、不推进 `gate_processed_cursor`），且 MUST NOT 对任何 targets 触发 spec write 三件套。`--decision skip-rewrite` 不受此约束（其 `--targets` 语义上可以为空或被忽略）。为满足此约束，编排者 MUST 在触发任何 targets 的 write 三件套之前先调用本命令并等待其成功返回；`gate --apply --decision rewrite` 因此是「决策落盘与 targets 校验」的单一入口，其成功返回是触发 write 三件套的前置条件（见 Requirement「auto 档默认不回写，交互档需人确认」的交互档流程顺序）。

#### Scenario: targets 含已开始的 change 被拒绝
- **WHEN** 调用 `gate --apply --decision rewrite --targets change-d`，但 change-d 已执行过 `npc implement run`（非 pending）
- **THEN** 命令返回 `{ok:false, error:"target-not-candidate"}`，`state.lessons.gate_decisions` 不新增记录，`gate_processed_cursor` 不推进

#### Scenario: targets 含未知 change_id 被拒绝
- **WHEN** 调用 `gate --apply --decision rewrite --targets no-such-change`，`no-such-change` 不存在于 `plan_order`
- **THEN** 命令返回 `{ok:false, error:"target-not-candidate"}`，不装订任何决策，不触发 spec write

#### Scenario: targets 全部是候选集子集时正常放行
- **WHEN** 调用 `gate --apply --decision rewrite --targets change-e`，change-e 属于 `--layer-idx N` 计算出的候选集
- **THEN** 命令按既有流程写入 `gate_decisions` 并推进游标
