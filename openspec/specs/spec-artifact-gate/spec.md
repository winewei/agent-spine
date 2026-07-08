# spec-artifact-gate Specification

## Purpose
TBD - created by archiving change spec-schema-hardening. Update Purpose after archive.
## Requirements
### Requirement: 项目本地 schema 单一事实源
仓库 MUST 提供项目本地 openspec schema，其名称为 `agent-spine`，位于 `openspec/schemas/agent-spine/schema.yaml`；`openspec/config.yaml` MUST 含 `schema: agent-spine`，使不带 `--schema` 参数的 `openspec status` 自动解析到该 schema。该 schema MUST 通过 `openspec schema validate agent-spine`。

#### Scenario: status 自动解析项目 schema
- **GIVEN** 仓库根存在 `openspec/config.yaml`，内容为 `schema: agent-spine`
- **WHEN** 执行 `openspec status --change <任一存在的 change> --json`（**不传** `--schema`）
- **THEN** stdout 为合法 JSON，且 `.schemaName == "agent-spine"`

#### Scenario: schema 自身合法
- **WHEN** 执行 `openspec schema validate agent-spine`
- **THEN** 进程 exit code 为 `0`
- **AND** stdout 含 `is valid`

### Requirement: implement 前的 artifact 完备性硬门
项目 schema 的 `apply.requires` MUST 恰为 `[proposal, specs, tasks]`（顺序不敏感，集合相等）。因 `npc plan check` 完全委托 `openspec status` 的 `applyRequires`，该配置 MUST 使缺失 `proposal.md` 或缺失 spec delta 的 change 在 implement 前被判定为未就绪。

#### Scenario: 缺 proposal.md 的 change 被拒
- **GIVEN** 一个 change 目录含 `tasks.md` 与 `specs/<cap>/spec.md`，**不含** `proposal.md`
- **WHEN** 执行 `npc plan check --change <id>`
- **THEN** stdout JSON 满足 `.ok == false` 且 `.ready == false`
- **AND** `.missing` 数组包含字符串 `"proposal"`
- **AND** `.apply_requires` 集合等于 `["proposal","specs","tasks"]`

#### Scenario: 缺 spec delta 的 change 被拒
- **GIVEN** 一个 change 目录含 `proposal.md` 与 `tasks.md`，`specs/` 目录为空或不存在
- **WHEN** 执行 `npc plan check --change <id>`
- **THEN** `.ready == false`
- **AND** `.missing` 数组包含字符串 `"specs"`

#### Scenario: 三件齐备的 change 放行
- **GIVEN** 一个 change 目录同时含 `proposal.md`、`specs/<cap>/spec.md`、`tasks.md`（`design.md` 可缺失）
- **WHEN** 执行 `npc plan check --change <id>`
- **THEN** `.ok == true` 且 `.ready == true`
- **AND** `.missing` 为空数组

#### Scenario: design.md 缺失不阻断
- **GIVEN** 一个三件齐备但**无** `design.md` 的 change
- **WHEN** 执行 `npc plan check --change <id>`
- **THEN** `.ready == true`（`design` 不在 `apply.requires` 内，为可选 artifact）

### Requirement: 生成时点的静态写作规范
项目 schema 的 `artifacts[].instruction` MUST 包含 change 无关的静态 spec 写作规范，至少覆盖：延迟决策只允许出现在 `## Open Questions` 段落内、每个 `#### Scenario:` 正文必须含 `WHEN` 与 `THEN` 行、proposal 必须含 Non-Goals 段落、禁止含糊副词与实现泄漏。此规范为**生成时软引导**，MUST NOT 在本 change 中被实现为任何硬门。

#### Scenario: instruction 含延迟决策规则
- **WHEN** 读取 `openspec/schemas/agent-spine/schema.yaml` 中 `id == "design"` 的 artifact 的 `instruction` 字段
- **THEN** 该字段文本含子串 `Open Questions`
- **AND** 该字段文本明确要求：未决决策 MUST 写入 `## Open Questions`，MUST NOT 内联于 `Decisions` 正文

#### Scenario: instruction 含 proposal 的 Non-Goals 要求
- **WHEN** 读取 `id == "proposal"` 的 artifact 的 `instruction` 字段
- **THEN** 该字段文本要求 proposal 含 Non-Goals（或「非目标」）段落

#### Scenario: 软引导不产生硬门
- **GIVEN** 一个 change 的 `design.md` 在 `## Decisions` 正文内联写有「实施时定」，且其余 artifact 三件齐备
- **WHEN** 执行 `npc plan check --change <id>`
- **THEN** `.ready == true`（本 change 不引入语义层硬门；该形态的拦截属后续 `repo-spec-lint` 的职责）

### Requirement: instruction 不得泄漏验证侧判据
项目 schema 的任何 `artifacts[].instruction` 字段 MUST NOT 包含当次 change 的 review focus 渲染文本、上轮 review findings 原文、或 reviewer 的评分 rubric 细则。此约束守核心不变量 1「生成 ⊥ 验证」，与 `src/npc/templates.py` 中 `SELFCHECK_RUBRIC_MD` 的既有边界一致。

#### Scenario: 负向断言——instruction 为 change 无关静态文本
- **WHEN** 解析 `openspec/schemas/agent-spine/schema.yaml` 的全部 `artifacts[].instruction` 字段
- **THEN** 其文本 MUST NOT 含任何 change 的目录名、任何 `review.json` 的 findings 原文、任何 severity/blocking 判据阈值
- **AND** 其文本对任意两个不同的 change 渲染结果完全相同（静态、无插值占位符）

### Requirement: 回归覆盖
本 change MUST 附带自动化测试，断言上述硬门在真实 openspec 二进制下成立，且测试 MUST 在 `openspec` 不可用时明确跳过而非静默通过。

#### Scenario: openspec 缺失时测试跳过
- **GIVEN** `openspec` 不在 `PATH` 中
- **WHEN** 运行本 change 新增的测试
- **THEN** 测试结果为 `skipped`，且跳过原因含 `openspec`
- **AND** 测试 MUST NOT 报告为 `passed`

