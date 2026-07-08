# spec-attribution Specification

## Purpose
TBD - created by archiving change spec-attribution-telemetry. Update Purpose after archive.
## Requirements
### Requirement: review output schema 必须随代码演进
`ensure_schema(schema_path)` MUST 在磁盘上的 schema 内容与 `REVIEW_SCHEMA` 语义不一致时重写该文件；内容一致时 MUST NOT 重写。判定 MUST 基于解析后的 JSON 对象相等，而非字节相等（避免缩进/键序差异导致无谓重写）。

#### Scenario: 陈旧 schema 被重写
- **GIVEN** `schema_path` 指向一个已存在的文件，其 JSON 内容缺少 `spec_attribution` 属性
- **WHEN** 调用 `ensure_schema(schema_path)`
- **THEN** 该文件被重写为 `REVIEW_SCHEMA` 的序列化结果
- **AND** 重写后 `json.loads(schema_path.read_text())` 等于 `REVIEW_SCHEMA`

#### Scenario: 内容一致时不重写（幂等）
- **GIVEN** `schema_path` 的 JSON 内容已等于 `REVIEW_SCHEMA`，且其 `st_mtime` 记为 `T0`
- **WHEN** 连续两次调用 `ensure_schema(schema_path)`
- **THEN** 该文件的 `st_mtime` 仍为 `T0`（未被重写）

#### Scenario: 键序与缩进差异不触发重写
- **GIVEN** `schema_path` 的内容是 `REVIEW_SCHEMA` 以不同缩进、不同键序序列化的结果
- **WHEN** 调用 `ensure_schema(schema_path)`
- **THEN** 该文件未被重写（语义相等）

#### Scenario: schema 文件缺失时创建
- **GIVEN** `schema_path` 不存在
- **WHEN** 调用 `ensure_schema(schema_path)`
- **THEN** 该文件被创建，内容等于 `REVIEW_SCHEMA`
- **AND** 其父目录若不存在则被一并创建

### Requirement: finding 必须携带 spec 归因枚举
`REVIEW_SCHEMA` 的 finding 对象 MUST 新增必需属性 `spec_attribution`，其类型为 string，`enum` 恰为 `["spec-silent", "spec-ambiguous", "spec-contradicted", "impl-deviation"]`。该属性 MUST 出现在 finding 的 `required` 列表中。四值语义 MUST 在 schema 的 `description` 中写明：`spec-silent` = spec 未规定该行为；`spec-ambiguous` = spec 有规定但存在多种合理解读；`spec-contradicted` = 实现与 spec 明文相悖；`impl-deviation` = spec 明确无歧义，实现未照做。

#### Scenario: schema 含四值枚举且为必需
- **WHEN** 读取 `REVIEW_SCHEMA["properties"]["findings"]["items"]`
- **THEN** 其 `properties.spec_attribution.enum` 等于 `["spec-silent","spec-ambiguous","spec-contradicted","impl-deviation"]`
- **AND** `"spec_attribution"` 在其 `required` 列表中
- **AND** 其 `additionalProperties` 仍为 `false`

#### Scenario: 非法枚举值被 schema 拒绝
- **GIVEN** 一个 finding 对象，其 `spec_attribution` 取值为 `"maybe-spec"`
- **WHEN** 用 `REVIEW_SCHEMA` 校验该对象
- **THEN** 校验失败

### Requirement: spec 归因不参与 blocking 判定
`parse_review()` 的 `blocking` 计数 MUST 继续仅由 `severity ∈ {critical, high}` 且 `in_scope == true` 决定。`spec_attribution` 的任何取值 MUST NOT 改变某条 finding 是否计入 blocking。

#### Scenario: 归因值不影响 blocking 计数
- **GIVEN** 两个 review JSON，findings 完全相同，仅 `spec_attribution` 分别为 `spec-silent` 与 `impl-deviation`
- **WHEN** 分别调用 `parse_review()`
- **THEN** 两者的 `blocking` 值相等
- **AND** 两者的 `advisory` 值相等

### Requirement: 派生 spec 归因分布并向后兼容
`parse_review()` 的返回值 MUST 新增键 `spec_attribution_counts`，其值为映射：四个枚举值各自的 finding 计数，外加键 `unknown` 统计缺失 `spec_attribution` 字段的 finding 数。统计范围 MUST 与 `blocking` 一致（仅统计 `in_scope == true` 且 `severity ∈ {critical, high}` 的 finding）。缺失该字段的历史 review.json MUST 被正常解析，MUST NOT 抛异常。

#### Scenario: 历史 review.json 无该字段仍可解析
- **GIVEN** 一个 review JSON，其全部 findings 均不含 `spec_attribution` 键，其中 2 条为 in_scope 的 high
- **WHEN** 调用 `parse_review(data)`
- **THEN** 不抛异常
- **AND** 返回值 `.spec_attribution_counts["unknown"]` 等于 `2`
- **AND** 返回值 `.blocking` 等于 `2`

#### Scenario: 混合归因被正确计数
- **GIVEN** 一个 review JSON，其 in_scope blocking findings 的 `spec_attribution` 依次为 `spec-silent`、`spec-silent`、`impl-deviation`
- **WHEN** 调用 `parse_review(data)`
- **THEN** `.spec_attribution_counts["spec-silent"]` 等于 `2`
- **AND** `.spec_attribution_counts["impl-deviation"]` 等于 `1`
- **AND** `.spec_attribution_counts["unknown"]` 等于 `0`

#### Scenario: advisory finding 不计入归因分布
- **GIVEN** 一个 review JSON，含 1 条 `severity == "low"` 的 finding，其 `spec_attribution == "spec-silent"`，无任何 blocking finding
- **WHEN** 调用 `parse_review(data)`
- **THEN** `.spec_attribution_counts` 的所有值之和为 `0`

### Requirement: telemetry 携带并聚合 spec 归因
`review.round` 事件 MUST 携带 `spec_attribution_counts` 字段，且该字段 MUST 出现在 `telemetry.EMIT_FIELD_CONTRACT["review.round"]` 与 `telemetry_schema_v1.json` 中。`npc telemetry agg` MUST 聚合该字段并输出 `spec_attributable_blocking_rate`，定义为 `(spec-silent + spec-ambiguous + spec-contradicted) / (前三者 + impl-deviation)`；当分母为 `0` 时该值 MUST 为 `null`（**MUST NOT** 为 `0`）。`unknown` MUST NOT 计入分子或分母。

#### Scenario: emit 的字段集合与契约一致
- **GIVEN** 一轮 review 完成并 emit `review.round` 事件
- **WHEN** 捕获实际 emit 的事件字典（monkeypatch `emit_event`）
- **THEN** 其键集合等于 `EMIT_FIELD_CONTRACT["review.round"]`
- **AND** `spec_attribution_counts` 在其中

#### Scenario: 聚合计算归因率
- **GIVEN** telemetry 中两条 `review.round` 事件，`spec_attribution_counts` 分别为 `{"spec-silent":2,"impl-deviation":2,"unknown":0}` 与 `{"spec-ambiguous":1,"impl-deviation":3,"unknown":5}`
- **WHEN** 执行 `npc telemetry agg`
- **THEN** 输出的 `spec_attributable_blocking_rate` 等于 `0.375`（即 `(2+1)/(2+1+2+3)`）

#### Scenario: 分母为零时归因率为 null
- **GIVEN** telemetry 中全部 `review.round` 事件的 `spec_attribution_counts` 仅含 `unknown`
- **WHEN** 执行 `npc telemetry agg`
- **THEN** 输出的 `spec_attributable_blocking_rate` 为 `null`

#### Scenario: 历史事件缺该字段不破坏聚合
- **GIVEN** telemetry 中混有不含 `spec_attribution_counts` 键的历史 `review.round` 事件
- **WHEN** 执行 `npc telemetry agg`
- **THEN** 命令 exit code 为 `0`
- **AND** 不含该键的事件被忽略，不计入分子或分母

### Requirement: 归因字段不得回流生成侧
`npc fixer findings` 渲染给 coder 的 findings 文本 MUST NOT 包含 `spec_attribution` 字段的值或字段名。此约束守核心不变量 1「生成 ⊥ 验证」：归因是验证方对 spec 质量的判断，若泄漏给 coder，等同于把 reviewer 的评判维度提前告知生成方。

#### Scenario: 负向断言——fixer 渲染不含归因
- **GIVEN** 一个 review JSON，其 in_scope blocking finding 的 `spec_attribution == "spec-silent"`
- **WHEN** 执行 `npc fixer findings` 渲染该轮 findings
- **THEN** 输出文本 MUST NOT 含子串 `spec_attribution`
- **AND** 输出文本 MUST NOT 含子串 `spec-silent`

### Requirement: 本 change 不引入任何闸门
本 change MUST NOT 基于 `spec_attribution` 或 `spec_attributable_blocking_rate` 引入任何阻断、阈值、退出码变更或 `auto-decide` 触发条件。

#### Scenario: 归因率极高时流程不受影响
- **GIVEN** 某 change 的全部 blocking finding 归因均为 `spec-silent`（`spec_attributable_blocking_rate == 1.0`）
- **WHEN** 该 change 走完 review → fix → archive 流程
- **THEN** 其 archive 结果与 `spec_attribution` 全为 `impl-deviation` 时完全一致
- **AND** `npc auto-decide` 的可用 trigger 集合未新增任何项

