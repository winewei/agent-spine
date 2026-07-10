## MODIFIED Requirements

### Requirement: 生成侧不得预知本轮评判标准
`npc spec write run` 渲染给 `spine-spec-writer` 的 prompt 文件 MUST NOT 包含本轮 spec-review 的 focus 渲染文本、评分 rubric 细则、或任何 `SPEC_REVIEW_SCHEMA` 的 `category` 枚举列表。`npc spec fix run` 渲染的 prompt MAY 包含**上一轮已签发**的 blocking findings 原文。`npc spec interrogate run` 渲染的 prompt 同样 MUST NOT 包含上述内容——盘问轮讨论的是仓库内已有的 analog 实现，不是本轮评判标准。此边界为**时点**边界，与 `spine-coder` 的 implement/fix 结构同构。

`spine-spec-writer` 撰写的 `pattern-interrogation.md` 与 `tasks.md` 的落点清单，正文 MUST NOT 引用 `SPEC_REVIEW_SCHEMA` 的任一 `category` 枚举值（`ambiguity`/`missing-scenario`/`implementation-leak`/`untestable`/`deferred-decision`/`contradiction`/`scope-creep`），MUST NOT 引用任何 `round-*.spec-review.json` 的 findings 原文。

`npc spec fix run --change <id> --round N`（`N >= 1`）在渲染任何 fix prompt 之前，MUST 先完成 `run-stale-review-guard` 能力定义的新鲜度校验（扫描该 change 目录下所有 `round-*.spec-review.json`，取轮次号最大值 `max_round`；若 `max_round` 大于 `N-1`，判定为过期输入并以 `stale_review_input` 拒绝，不渲染任何 prompt）。该校验通过之后，才轮到本条要求所约束的"prompt 只含上一轮已签发 findings、不含更晚轮次内容"的正向注入语义——过期输入场景下从一开始就不产出 prompt 文件，故不适用本条的注入语义（不存在的文件不含任何内容，也就无所谓泄漏）。

#### Scenario: 负向断言——write 轮 prompt 不含 rubric
- **GIVEN** `openspec/changes/<id>/pattern-interrogation.md` 已存在，且含 `## Analogs`、`## Assumptions`、`## Open Questions` 三个 H2 标题
- **WHEN** 执行 `npc spec write run --change <id>` 并读取渲染出的 prompt 文件全文
- **THEN** 其文本 MUST NOT 含子串 `scope-creep`
- **AND** MUST NOT 含子串 `implementation-leak`
- **AND** MUST NOT 含任何 `spec-review.json` 的 findings 原文

#### Scenario: fix 轮 prompt 可含上一轮已签发 findings
- **GIVEN** `round-0.spec-review.json` 已产出，含一条 `category == "ambiguity"` 的 blocking finding，且磁盘上不存在轮次号大于 0 的其它 `round-*.spec-review.json`
- **WHEN** 执行 `npc spec fix run --change <id> --round 1` 并读取渲染出的 prompt 文件
- **THEN** 其文本含该 finding 的 `detail` 原文

#### Scenario: 存在更高轮次 review 时拒绝渲染而非过滤内容
- **GIVEN** 磁盘上同时存在 `round-0.spec-review.json` 与 `round-1.spec-review.json`，两者 findings 的 `detail` 互不相同
- **WHEN** 执行 `npc spec fix run --change <id> --round 1`
- **THEN** 返回 `.ok == false`，错误标识 `stale_review_input`
- **AND** `round-1.spec-fix.prompt.md` 未被写出——不存在部分渲染、内容过滤后放行的中间状态

#### Scenario: 上一轮 review 未落盘时 fix 拒绝渲染
- **GIVEN** `round-0.spec-review.json` 不存在
- **WHEN** 执行 `npc spec fix run --change <id> --round 1`
- **THEN** 返回 `.ok == false`
- **AND** 输出含稳定错误标识 `prev_spec_review_missing`

#### Scenario: 负向断言——interrogate 轮 prompt 不含 rubric
- **WHEN** 执行 `npc spec interrogate run --change <id>` 并读取渲染出的 prompt 文件全文
- **THEN** 其文本 MUST NOT 含子串 `scope-creep`
- **AND** MUST NOT 含子串 `implementation-leak`
- **AND** MUST NOT 含任何 `SPEC_REVIEW_SCHEMA` 的 `category` 枚举值
- **AND** MUST NOT 含任何 `round-*.spec-review.json` 原文
