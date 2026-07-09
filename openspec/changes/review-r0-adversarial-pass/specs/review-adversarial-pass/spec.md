# review-adversarial-pass Specification

## Purpose

`npc review run --round 0` 在既有 compliance 评审通道之外新增一个 diff-only、对抗式的第二评审通道，两通道 findings 合并去重进同一份 `round-0.review.json`，使深层 bug（资源释放/边界/求值语义/并发生命周期类）更可能在 round 0 一次性暴露，而不是靠后续轮次逐轮挤出。`round >= 1` 不受影响，保持单通道。

## ADDED Requirements

### Requirement: round-0 执行双 pass 并合并进同一份 review.json

round-0 review 执行在 `round_n == 0` 且配置 `[review].adversarial_round0` 为 `true`（默认）时，MUST 在既有 compliance pass（pass1）成功产出后，顺序追加执行一个对抗式 pass（pass2），并将两者的 findings 合并去重后写入既有的 `round-0.review.json` 路径。`round-0.review.json` 本身的 `REVIEW_SCHEMA` 结构（`verdict` + `findings` 数组）保持不变，因此读取该 JSON 结构的既有下游行为（findings 解析与渲染）MUST NOT 因本需求需要任何改动。已有的 telemetry 事件例外：它需要按下方「telemetry 透出对抗通道运行状态」需求新增两个字段的透传逻辑，本需求不豁免该改动。

#### Scenario: 双 pass 均成功产出合法 JSON

- **WHEN** round-0 review 执行，pass1 与 pass2 均在各自重试次数内产出合法 `REVIEW_SCHEMA` JSON
- **THEN** `round-0.review.json` 的内容是两者 findings 的合并去重结果；`round-0.review.pass1.json` 与 `round-0.review.pass2.adversarial.json` 各自保留原始产物

#### Scenario: `adversarial_round0` 配置为 false 时不触发第二 pass

- **WHEN** `[review].adversarial_round0 = false` 且 `round_n == 0`
- **THEN** round-0 只执行既有单一 compliance pass，行为与本 change 之前一致；不产出 `round-0.adversarial.focus.md` / `round-0.review.pass2.adversarial.json`

### Requirement: round >= 1 保持单通道，不受本变更影响

`round_n >= 1` 时，round review 执行 MUST 只产生一次评审引擎调用，MUST NOT 触发对抗式 pass 或合并逻辑，无论 `[review].adversarial_round0` 取值为何。

#### Scenario: round=1 只调用一次引擎

- **WHEN** round review 以 `round_n=1` 执行
- **THEN** 评审引擎恰好被调用一次；不生成任何 `*.adversarial.*` 命名的产物文件

### Requirement: 对抗式 focus 模板不读 spec、只看 diff

新增的对抗式 round-0 focus 模板（pass2）渲染出的文本 MUST NOT 包含 `proposal.md` / `tasks.md` / `specs/**/spec.md` / `design.md` / `openspec/project.md` / 项目根 `CLAUDE.md` 中任一字符串引用，且 MUST NOT 包含任何项目级上下文注入内容（例如"tasks.md 中明确指定的实现方式视为项目权威决策，不报告与之冲突的建议"一类的免责条款文案）。模板 MUST 指示执行 `git --no-pager diff HEAD~1..HEAD` 查看本次 change 引入的 diff（与 proposal/design/tasks 一致的唯一 diff 指令；MUST NOT 用裸 `git diff`——其读取的是未提交工作区，在 clean worktree 中为空），MUST 包含对抗式框架文案（假设该 diff 必有 bug，唯一任务是找出它），MUST 明确列出四个审查重点：资源释放/double-free、边界与符号处理、急切求值/短路语义、并发与生命周期。

#### Scenario: 渲染文本不含 spec 文件引用

- **WHEN** 渲染对抗式 round-0 focus 模板
- **THEN** 输出文本不包含 `proposal.md` / `tasks.md` / `specs/` / `design.md` / `openspec/project.md` / `CLAUDE.md` 中任一字符串

#### Scenario: 渲染文本含四个审查重点

- **WHEN** 渲染对抗式 round-0 focus 模板
- **THEN** 输出文本包含「资源释放」或「double-free」、「边界」、「急切求值」或「短路」、「并发」或「生命周期」相关表述

### Requirement: findings 合并去重规则确定性

系统 MUST 按以下规则将两个 pass 各自产出的 `REVIEW_SCHEMA` 兼容 findings 数组合并为一份同样满足 `REVIEW_SCHEMA` 的最终结果：

1. 去重键为 `(file, line_range, category)` 三元组精确字符串匹配；pass2 中与 pass1 去重键相同的 finding MUST 被丢弃，保留 pass1 版本。
2. 合并顺序为 pass1 全量（原相对顺序）后接 pass2 去重后剩余（原相对顺序）；`id` 字段 MUST 按此顺序重新分配为 `F1, F2, ..., Fn`（丢弃引擎自报的原始 `id`）。
3. 合并结果的 `verdict` MUST 按合并后的 findings 全集重新计算：存在至少一条 `severity ∈ {critical, high}` 且 `in_scope == true` 的 finding → `changes-requested`；否则若 findings 非空 → `passed-with-advisory`；否则 → `approve`。MUST NOT 直接采信 pass1 或 pass2 自报的 `verdict` 字段。

#### Scenario: 同一问题被两个 pass 各自报告

- **WHEN** pass1 与 pass2 的 findings 中各有一条 `file`/`line_range`/`category` 三者完全相同的记录
- **THEN** 合并结果只保留 pass1 的那一条，总 findings 数比两份原始之和少 1

#### Scenario: pass2 独有 blocking finding 提升 verdict

- **WHEN** pass1 的 verdict 为 `approve`（无 findings），pass2 有一条 `severity=high` 且 `in_scope=true` 的独有 finding
- **THEN** 合并结果的 `verdict` 为 `changes-requested`，且该 finding 出现在合并结果中并被赋予新 `id`

#### Scenario: pass2 无 findings 时合并结果等价于 pass1-only

- **WHEN** pass2 输入为空 findings 替身 `{"findings": []}`（合并规则只消费 findings 数组，verdict 恒在合并后重算、从不读取任一 pass 的自报值，故替身无需含 verdict 字段，也不违反 `REVIEW_SCHEMA` 对完整 review 产物的 verdict 必填约束——替身不是落盘产物）
- **THEN** 合并结果的 findings 与 `id` 分配和仅有 pass1 时完全一致，`verdict` 由 pass1 的 findings 集合按同一规则重新计算得出

### Requirement: 对抗式 pass 失败时降级而非拖垮整轮

pass2（对抗式）在其重试预算耗尽后仍未产出合法 `REVIEW_SCHEMA` JSON 时，MUST NOT 使 round-0 视为失败。系统 MUST 将 round-0 结果降级为等价于仅 pass1 的结果（即以空 findings 集合充当 pass2 参与同一套合并归一化规则），并在返回值与 telemetry 中标记 `adversarial_pass_ran = false`。pass1 自身失败（重试耗尽）MUST 仍按既有行为使 round-0 整体失败，不受本需求影响。

#### Scenario: pass2 引擎报错、重试耗尽

- **WHEN** pass2 每次尝试均以非零退出码结束，重试预算耗尽
- **THEN** 该轮 review 结果为 `ok=true`（假设 pass1 成功），`round-0.review.json` 内容等价于 pass1-only，`adversarial_pass_ran=false`

#### Scenario: pass1 失败仍视为整轮失败

- **WHEN** pass1 引擎调用重试耗尽后仍未产出合法 JSON（无论 pass2 是否会被执行）
- **THEN** 该轮 review 结果为 `ok=false`，行为与本 change 引入前一致；pass2 MUST NOT 被执行

### Requirement: telemetry 透出对抗通道运行状态

`review.round` telemetry 事件 MUST 新增两个字段：`adversarial_pass_ran`（`bool`，任何情况下都 MUST 是 `true` 或 `false`，MUST NOT 为 `None`/缺省）与 `adversarial_blocking_count`（`int | None`）。两字段的取值 MUST 严格按下表五种互斥情形之一确定，不存在表外取值：

| # | 情形 | round_n | adversarial_round0 | pass1 结果 | pass2 结果 | `adversarial_pass_ran` | `adversarial_blocking_count` |
|---|---|---|---|---|---|---|---|
| 1 | 双 pass 成功 | `0` | `true` | 成功 | 成功 | `true` | `int`（`>= 0`，取自合并函数在合并期间、重编号之前统计并随返回值透出的 side-channel 计数：来源于 pass2 且未被去重丢弃的 blocking finding 数；MUST NOT 从合并后的 `round-0.review.json` 反推——重编号后来源信息已不可辨） |
| 2 | pass2 失败降级 | `0` | `true` | 成功 | 失败（重试耗尽） | `false` | `None` |
| 3 | pass1 失败（整轮失败） | `0` | `true` | 失败（重试耗尽） | 不执行 | `false` | `None` |
| 4 | 对抗通道禁用 | `0` | `false` | 成功或失败 | 不执行 | `false` | `None` |
| 5 | round>=1 | `>= 1` | 任意 | 既有单 pass | 不适用 | `false` | `None` |

即：`adversarial_blocking_count` 当且仅当 `adversarial_pass_ran == true` 时为非 `None` 的 `int`；其余四种情形（含 pass1 失败、对抗通道禁用、round>=1）均是 `adversarial_pass_ran == false` 且 `adversarial_blocking_count is None`——这三者虽然都取值 `false`/`None`，但触发原因不同，调用方可结合 `round_n` 与配置的 `adversarial_round0` 另行区分，telemetry 字段本身不做区分。`review.round` 事件的字段白名单契约（telemetry 结构不变量测试所强制的那份登记表）MUST 同步登记这两个字段，缺失任一都视为契约破坏。

#### Scenario: round-0 双 pass 成功时事件含两个新字段

- **WHEN** round-0 双 pass 均成功且合并出至少 1 条来源于 pass2 的 blocking finding
- **THEN** 对应 `review.round` 事件的 `adversarial_pass_ran == true`，`adversarial_blocking_count >= 1`

#### Scenario: pass2 失败降级时事件新字段为 false/None

- **WHEN** round-0 的 pass1 成功、pass2 重试耗尽仍未产出合法 JSON
- **THEN** 对应 `review.round` 事件（`ok == true`）的 `adversarial_pass_ran == false` 且 `adversarial_blocking_count is None`

#### Scenario: pass1 失败（整轮失败）时事件新字段为 false/None

- **WHEN** round-0 的 pass1 重试耗尽仍未产出合法 JSON，round-0 整轮失败
- **THEN** 对应 `review.round` 事件（`ok == false`）仍 MUST 含 `adversarial_pass_ran == false` 且 `adversarial_blocking_count is None`（不是缺省字段，不是 `None`/未定义值）

#### Scenario: `adversarial_round0=false` 时事件新字段为 false/None

- **WHEN** `[review].adversarial_round0 = false` 且 `round_n == 0`，round-0 只执行单一 compliance pass
- **THEN** 对应 `review.round` 事件的 `adversarial_pass_ran == false` 且 `adversarial_blocking_count is None`

#### Scenario: round>=1 事件的新字段为固定 false/None

- **WHEN** `round_n >= 1` 的 review.round 事件被 emit
- **THEN** 事件含 `adversarial_pass_ran == false`（`bool`，不是 `None`）且 `adversarial_blocking_count is None`
