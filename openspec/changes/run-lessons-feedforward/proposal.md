# run-lessons-feedforward

## Why

`spine-run` 一个 run 内的多个 change 目前互相不共享经验：change A 在 fix 循环里反复踩的坑（比如某类 validation 遗漏、某种并发处理不到位），change B 完全无从知晓，只能在自己的 review-fix 循环里重新交一遍学费。Bun 迁移复盘（`docs/optimization-proposals/2026-07-09-bun-migration-lessons.md` 提案 3）指出：Bun 的"3-file trial run"（先拿小样本试跑、验证约定可行后再铺开）是一种廉价但有效的前馈模式；在 `spine-run` 的多 change plan 里，**第一个完成的 change 天然就是那个 trial run**——只要把它暴露出的失败模式喂给后续 change，不需要额外设计"试点阶段"就能免费获得同等收益。

现有基础设施已经具备两个关键前提：① fixer 每轮 fix 在 RESULT 行里自报 `categories_scanned`/`regressions_added`/`notes`（结构化、非自然语言散文），已落盘在该 change 的 `events.jsonl`（`phase.exit` / `fix-rN` / `done` 记录）；② `parallel-dag-scheduling` 已经在 `spine-run.md` Step 3 建立了逐层调度 + 层屏障（一层全部到终态才进下一层）。本 change 只补两条缺失的连线：**archive 成功后确定性提炼这些自报字段 → 追加到 run 级 `lessons.md`**，以及**后续 change 的 implement prompt 指向这份 `lessons.md`**。第三点是可选增强：既然 DAG 层屏障本来就是一个天然的同步点，顺手在那里开一个"试点回写闸口"——允许（人确认或 auto 保守默认关闭）用已积累的 lessons 去修订下游**尚未开始**的 change 的 tasks/design，而不是等它自己在 fix 循环里重新踩坑。

**不做的边界（如实声明）**：这不是让 npc 或某个 agent "学习"或"总结教训"——lessons.md 的每一条都是**已经结构化落盘的 fixer 自报字段的确定性拼接**（categories_scanned / regressions_added / notes 逐字段复制），不经过任何 LLM 摘要或解释步骤；npc 不读、不引用、不转述 review.json 或任何 reviewer 产出的自然语言（不破不变量 1「生成 ⊥ 验证」：验证方的判断绝不可流回生成方视野，哪怕是"下一个"生成方）。

## What Changes

- **`npc lessons record --seq N`**（新增 `src/npc/lessons.py`）：在 `npc archive run` 成功后调用（`spine-run.md` Step 3c，非阻塞，与 `npc spec-report render` 同一时点、同样的 best-effort 语义）。读取该 change 的 `<base>/events.jsonl`，筛出全部 `kind=phase.exit && phase~=^fix-r\d+$ && status=done` 记录，按 round 顺序确定性拼接为一个 lessons 条目（change_id / archived_at / rounds / categories_scanned 去重并集 / regressions_added 去重并集 / 各轮 notes 原文列表），追加到 `<run_dir>/lessons.md`。硬约束：只读这些字段，**MUST NOT** 打开或引用 `round-N.review.json`、`round-N.focus.md`、`*.spec-review.json` 或其中的 category/finding 原文；该 change 没有任何 fix 轮（一次 review 即过）时不追加条目（无失败模式可提炼）。幂等：同一 change_id 的条目已存在时跳过，不重复追加。
- **implement prompt 注入 lessons.md 指针**：`src/npc/templates.py` 的 `render_implementer` 新增可选段落，`npc implement run` 渲染时若 `<run_dir>/lessons.md` 存在且非空，在"必读输入"追加一条指向该文件的 bullet（连同一句"仅供参考、不改变 tasks/spec 验收标准"的免责限定，避免 coder 把 lessons 误当作新的验收依据）；文件不存在或为空（run 内第一个 change，或此前 change 全部一轮通过）时不渲染该段落，行为与现状完全一致。
- **DAG 层屏障处的试点回写闸口**（`npc lessons gate` + 复用既有 `spec write` 三件套）：
  - `npc lessons gate --layer-idx N`：纯只读，在 `spine-run.md` Step 3 的层屏障之后、下一层开始之前调用。确定性算出候选下游集合——`plan_order` 中 `dag_layer > N` 且 `progress.status == "pending"`（尚未 implement）的 change——并报告 `lessons.md` 是否存在新增条目（相对上次已处理的条目游标）。候选集为空或无新增条目时直接返回 `has_candidates:false`，编排者跳过闸口、照常进入下一层。
  - **交互档**：编排者用 `AskUserQuestion` 把候选下游 change 列表 + lessons 摘要摆给用户，用户选择要修订的子集（可以全不选）。
  - **auto 档**：不调用 `AskUserQuestion`；默认动作固定为 `skip-rewrite`（不改 tasks/design，只依赖既有的 lessons 注入路径生效）。这是保守默认，尊重不变量 3（没有人在回路时不额外加判断分支）。
  - 用户/auto 选定要修订的下游 change 后，编排者**先**调用 `npc lessons gate --apply --targets <csv> --decision rewrite|skip-rewrite`：该调用校验 `--decision rewrite` 时的 targets 是否属于本次候选集，通过后把本次闸口决策记入 state（供 resume/telemetry 追溯），并推进已处理条目游标（避免同一批 lessons 在下一层屏障被重复摆出来）；targets 校验失败时返回结构化错误，不落盘、不推进游标。只有 `--decision rewrite` 且 apply 校验通过后，编排者才对每个目标复用既有 `npc spec write run --change <id>`（`docs/cli.md` §8d 三件套），新增一个可选 `--lessons-path <path>` 透传给 `spine-spec-writer` 的 write prompt——作为独立于 `--goal` 的"同 run 前置 change 失败模式（参考，非强制）"段落注入，不改变 write 轮"生成侧不得预知本轮评判标准"的既有边界。`spine-spec-writer` 按既有职责边界只改该 change 目录下的 tasks.md / design.md；`npc spec write record` 装订，边界拦截（`out_of_scope_changes` / `unexpected_commit`）复用既有实现，零新增代码。apply 校验失败时 MUST NOT 触发任何 write 三件套。

## Capabilities

### New Capabilities

- `run-lessons-extraction`：archive 成功后从该 change 的结构化自报字段（`categories_scanned` / `regressions_added` / `notes`，全部来自 fixer 的 RESULT 行）确定性提炼一条 lessons 条目并追加到 run 级 `lessons.md`；不读、不含任何 reviewer 产出。
- `run-lessons-injection`：`npc implement run` 渲染 implement prompt 时，若 run 级 `lessons.md` 存在且非空，注入指向该文件的必读输入指针（不注入内容本身，coder 自行 Read）。
- `pilot-rewrite-gate`：DAG 层屏障后的确定性闸口，报告可修订的下游未开始 change 与 lessons 增量；交互档经人确认、auto 档默认不改，复用既有 `spec write` 三件套执行实际修订，边界严格限定在目标 change 自己的 `openspec/changes/<id>/` 目录。

### Modified Capabilities

<!-- 本 change 不修改 archive-error-contract / parallel-layer-scheduling / spec-writer 现有 Requirement 的行为契约：
     lessons record 挂在 archive 成功之后、非阻塞（与 spec-report 同构，不改 archive 判定）；
     pilot-rewrite-gate 挂在既有层屏障之后、不改变层屏障本身"全部到终态才进下一层"的判据；
     spec write 三件套的 --lessons-path 是新增可选参数，省略时行为与现状逐字等价。 -->

## Impact

- `src/npc/lessons.py`（新增）：`extract_and_append(p, seq)`（record）、`gate_candidates(p, layer_idx)`（gate 只读部分）、`apply_gate_decision(p, layer_idx, targets, decision)`（gate 落状态与游标）
- `src/npc/cli.py`：注册 `npc lessons record` / `npc lessons gate` 子命令
- `src/npc/templates.py`：`render_implementer` 新增可选 `lessons_path` 形参与对应段落
- `src/npc/pipeline.py`：`npc implement run` 渲染路径接入 `lessons_path`（存在且非空才传）
- `src/npc/templates.py` + `src/npc/spec_pipeline.py`（或对应 write-run 渲染入口）：`npc spec write run` 新增可选 `--lessons-path`，注入独立段落
- `src/npc/state.py`：新增 `lessons` 顶层节（`entries_appended: [change_id...]`、`gate_processed_cursor`、`gate_decisions: [{layer_idx, targets, decision, ts}]`），供幂等与 resume/telemetry 追溯
- `plugins/agent-spine/commands/spine-run.md`：Step 3c 补 `npc lessons record`（非阻塞，紧邻 `spec-report render`）；层屏障之后补 `npc lessons gate` 判断分支（交互档 AskUserQuestion / auto 档默认 skip-rewrite）
- `tests/`：lessons 提炼幂等性、无 fix 轮不追加、注入条件（存在且非空）、gate 候选集确定性、auto 默认不改、边界拦截复用既有测试路径
