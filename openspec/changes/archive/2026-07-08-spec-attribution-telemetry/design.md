## Context

目标是给 harness 装一把尺子：让「code review 抓到的 blocking finding」能被机械地归因到 spec 侧还是实现侧，从而使 spec 写法可以被 telemetry 驱动地迭代。

现状盘点（均已核实）：

- `src/npc/schema.py` 的 `REVIEW_SCHEMA` 是 codex review 输出契约的单一事实源，`findings.items.additionalProperties == false`，`required` 含 9 个字段（`id`/`severity`/`category`/`title`/`file`/`line_range`/`detail`/`recommendation`/`in_scope`）。
- `ensure_schema(schema_path)` 当前实现为 `if schema_path.exists(): return False` —— **只在文件缺失时写盘**。而 `~/task_log/.new-plan-review-schema.json` 已存在（本机 mtime 为 6 月 22 日）。因此改 `REVIEW_SCHEMA` 的代码**不会**改变 codex 实际收到的 schema。
- `src/npc/review.py` 的 `parse_review()` 是纯函数，从 findings 派生 `verdict`/`blocking`/`advisory`/`categories`/`blocking_findings`；`blocking` 判定为 `severity ∈ {critical, high}` 且 `in_scope`。
- `review.round` 的 telemetry 字段集合由 `telemetry.EMIT_FIELD_CONTRACT["review.round"]` 硬约束，且有结构测试断言实际 emit 的键集合与契约相等。

## Goals / Non-Goals

**Goals**

- 让 schema 演进真正生效（修 `ensure_schema` 的 write-once 缺陷）。
- 让每条 blocking finding 携带一个**结构化四值归因**，由验证方产出。
- 让归因沿 `parse_review → review.round → telemetry agg` 完整贯通，产出一个可长期观察的比率。
- 全程保持对历史 `review.json` 与历史 telemetry 事件的向后兼容。

**Non-Goals**

- 不引入任何基于归因的闸门、阈值或阻断（严守不变量 3：先有方差，再有硬轨）。
- 不改 `blocking` 判定逻辑。
- 不给 `category` 补 enum（是独立缺陷，不在本 change 范围）。
- 不改 `npc fixer findings` 给 coder 的输入。
- 不做 spec 质量评分、排名或跨 change 对比。

## Decisions

**D1：先修 `ensure_schema`，再加字段。**
若不修，本 change 的 schema 变更对 codex 完全不可见，测试会绿而线上无效——这是最危险的失败模式（静默无效）。改为「解析后 JSON 对象不相等则重写」。选择语义相等而非字节相等，避免 `indent`/键序波动导致每次运行都重写文件（无谓 IO 与 mtime 抖动）。

**D2：归因由 reviewer 产出，不由 coder 自报。**
coder 自报「我这条实现是不是 spec 没说清」会撞不变量 1，且属于让被告写判决书。归因是**验证侧对 spec 质量的判断**，与 verdict 同源、同一时点产出，天然属于 reviewer 的输出契约。

**D3：归因用四值枚举，不用自由文本。**
不变量 2「不信 LLM 散文，只信结构化契约」。四值经 JSON Schema `enum` 强制，非法值在 codex 输出阶段即被 schema 校验拦截，无需下游做字符串规整。四值的切分依据是「spec 说了没有 / 说了但可多解 / 说了但实现反着来 / 说清了实现没做」——前三者指向 spec 侧，第四者指向实现侧，故归因率的分子恰为前三者之和。

**D4：`unknown` 既不进分子也不进分母。**
历史 review.json 无该字段，若把 `unknown` 计入分母会把归因率系统性稀释向 0；计入分子则凭空抬高。唯一诚实的处理是从比率中完全排除，只在 `spec_attribution_counts` 里保留其计数以供审计。分母为 0 时比率取 `null` 而非 `0` —— 与仓库既有约定一致（缺数据不得伪装成「表现良好」）。

**D5：归因字段不得回流生成侧。**
`npc fixer findings` 在 fix 轮把 blocking findings 原文渲染给 coder。按已裁定的不变量 1 边界（**时点**边界：判定签发后可读整改输入；判定签发前不得预知评判标准），fix 轮读 findings 本身是合法的。但 `spec_attribution` 是 reviewer 对**评判维度**的显式标注，把它喂给 coder 等于在下一轮生成前告知「reviewer 正按 spec 符合度这个维度打分」，实质是 rubric 泄漏。故显式加负向 Requirement + 负向测试，与 `templates.py` 中 `SELFCHECK_RUBRIC_MD` 的既有防护模式一致。

**D6：本 change 只造尺子，不动闸门。**
按不变量 3，新硬轨必须先被真实 telemetry 方差打出来。当前关于「spec 缺陷占比」的唯一数据是一次 N=4 changes / 12 findings 的人工抽样，且带检出偏差。因此本 change 的正确产出是**度量本身**；是否要据此收紧任何门，留给这把尺子攒够样本之后再议。

## Risks / Trade-offs

- **[reviewer 归因带有与人工归因相同的检出偏差]** → `spec_attributable_blocking_rate` 只能覆盖**已被检出的** finding。因 spec 含糊而从未被任何人发现的缺陷，永远不会出现在分子里。缓解：在 proposal 与聚合输出的文档中显式标注该比率为**下界**而非真值；禁止后续 change 把它当作「spec 质量真值」使用。
- **[`ensure_schema` 行为变更会重写用户既有文件]** → 该文件位于 `~/task_log/`，是跨项目共享的派生产物而非用户资产，重写是期望行为。缓解：重写前后内容均由 `REVIEW_SCHEMA` 唯一决定，可确定性复现；不做备份（无信息损失）。
- **[新增 required 字段会让旧版 codex prompt 产出的 JSON 校验失败]** → 仅影响**新一轮** review（schema 与 prompt 同批更新）。历史 `review.json` 文件由 `parse_review` 侧向后兼容处理，不重新校验。
- **[`spec_attribution_counts` 是 dict 型 telemetry 字段]** → 与既有 `blocking_categories`（list 型）形态不同。缓解：`telemetry_schema_v1.json` 显式声明其为 object；聚合侧参照 `blocking_categories` 已有的 `defaultdict(int)` 累加模式实现，避免另造一套。

## Migration Plan

1. 修 `ensure_schema`（含幂等测试），此时 schema 内容未变，线上无可见变化。
2. 向 `REVIEW_SCHEMA` 加 `spec_attribution`；下次 `npc review run` 时 `ensure_schema` 自动重写磁盘 schema。
3. 同批更新 `focus.py` 中给 reviewer 的输出要求文案（列出四值语义），使 prompt 与 schema 同步。
4. `parse_review` 加派生 + 向后兼容；`EMIT_FIELD_CONTRACT` 与 `telemetry_schema_v1.json` 加字段；`aggregate` 加比率。
5. 回滚：还原 `REVIEW_SCHEMA` 后，下次运行 `ensure_schema` 会自动把磁盘 schema 改回。无残留状态。

## Open Questions

无。四个候选决策点（何时修 `ensure_schema`、归因由谁产出、枚举 vs 自由文本、`unknown` 如何参与比率）均已在上文定稿并给出理由，不存在留待实施时决定的机制。
