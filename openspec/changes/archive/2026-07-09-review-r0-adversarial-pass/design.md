## Context

`run_review_round`（`src/npc/pipeline.py:584`）目前对每个 round 恰好执行 1 次引擎调用：`_render_focus` → 单个 `_codex_exec` / `_claude_exec`（重试 `retries` 次）→ `_review.parse_review` → `_do_review_phase_exit_and_trend` → `_telemetry.emit_review_round`。round-0 与 round-N 的差异只在 focus 模板（`focus.py:205` `_round_0_template` vs `focus.py:232` `_round_n_template`），流程结构完全一致。

`REVIEW_SCHEMA`（`src/npc/schema.py:14`）定义 findings 的字段集合（`additionalProperties: False`），`id` 字段只是「本轮唯一 id，建议格式 F1/F2...」的自由字符串，无跨文件命名空间约束。`review.parse_review`（`src/npc/review.py:29`）从单份 review JSON 派生 `verdict`/`blocking`/`advisory`/`categories`/`blocking_findings`/`spec_attribution_counts`，是唯一读取 review JSON 结构的下游消费者（`fixer.render_findings` 和 telemetry 都吃它的输出，不直接碰原始 JSON）。

`EMIT_FIELD_CONTRACT["review.round"]`（`telemetry.py` ~66-70 行）是 `tests/test_structural_invariants.py` 强制的字段白名单，新增字段必须同步登记，否则结构测试 fail。

## Goals / Non-Goals

**Goals**

- round-0 增加一个不读 spec、只看 diff 的对抗式评审通道，与既有 compliance 通道并行产出，最终合并进同一份 `round-0.review.json`。
- 下游（`parse_review` / `fixer.render_findings` / `emit_review_round` 消费 review JSON 的部分）零改动即可工作——合并后的 JSON 必须满足现有 `REVIEW_SCHEMA` 语义。
- 对抗 pass 失败时可降级，不引入新的整轮失败模式。
- round>=1 完全不受影响。

**Non-Goals**

- 不改 round>=1 的模板、流程或 stale 检测。
- 不并行执行两个 pass（v1 顺序执行：先 compliance 后 adversarial），避免同时引入并发执行与合并两个变量。
- 不做语义/模糊去重；只做 `(file, line_range, category)` 精确字符串匹配去重。
- 不给两个 pass 分别配置不同 engine/model；两者沿用 round-0 已解析的 `selected_engine`（同一 `--engine` / `[review].engine` 决策）。
- 不改 `REVIEW_SCHEMA` 的字段结构（不新增 `source`/`pass` 等字段区分来源，避免 `additionalProperties: False` 连锁改动波及 schema 版本与所有既有测试 fixture）。
- 不涉及 spec review 流水线（`spec_pipeline.py` / `SPEC_REVIEW_SCHEMA`）。

## Decisions

**D1：对抗式 pass 不注入 project context、不引用 tasks.md/design.md 的"权威决策免责条款"。**

`focus.py` 的 `DEFAULT_PROJECT_CONTEXT` 与 `load_project_context` 明确写着"tasks.md 中明确指定的实现方式视为项目权威决策，不报告与之冲突的建议"——这是 compliance pass 用来压制误报的机制，但同一机制会压制真实 bug（对着"这是故意的"打消念头，恰恰是 Bun 经验里合规评审漏抓 double-free 的原因）。故对抗式模板 **MUST NOT** 调用 `load_project_context`、**MUST NOT** 指示读取 `proposal.md` / `tasks.md` / `specs/**/spec.md` / `design.md` / `openspec/project.md` / `CLAUDE.md`；唯一指示是 `git --no-pager diff HEAD~1..HEAD` 与对抗式框架文案。这是刻意的职责切分：pass1 判"合规"，pass2 只判"有没有 bug"。

**D2：两个 pass 共用 `REVIEW_SCHEMA`，不新增字段区分来源；来源信息靠 id 前缀 + 合并顺序隐式保留。**

若给 finding 加 `source: "compliance"|"adversarial"` 字段，需要同步改 `REVIEW_SCHEMA`（`additionalProperties: False`）、`SPEC_REVIEW_SCHEMA` 是否同构、`ensure_schema` 的落盘重写、以及所有断言 findings 字段集合的既有测试——影响面远超本 change 的目标。合并函数改为在**合并阶段**重新分配 id（见 D4），来源可从合并前的原始两份 JSON（各自落盘保留，见 D3）回溯，不需要在 schema 里显式携带。

**D3：每个 pass 的原始产物独立落盘，合并结果覆盖既有 `round-0.review.json` 路径。**

文件命名：

| 内容 | 路径 |
|---|---|
| pass1 (compliance) focus | `round-0.focus.md`（不变） |
| pass1 (compliance) 原始 review JSON | `round-0.review.pass1.json`（新） |
| pass1 events | `round-0.events.jsonl`（不变） |
| pass2 (adversarial) focus | `round-0.adversarial.focus.md`（新） |
| pass2 (adversarial) 原始 review JSON | `round-0.review.pass2.adversarial.json`（新） |
| pass2 events | `round-0.adversarial.events.jsonl`（新） |
| 合并后的最终产物 | `round-0.review.json`（**路径不变**，内容为合并结果） |

`round-0.review.json` 路径不变是关键约束：`_review_round_duration_ms` 之外的所有下游（`parse_review`、`fixer.render_findings`、`_telemetry.emit_review_round` 里 `_build_tokens(focus_md, review_json)` 的路径推导）都硬编码 `round-{round_n}.review.json`，改路径会连锁改一批调用点。保留原路径、只改内容来源，是影响面最小的接入点。

**D4：合并规则——精确去重 + 顺序重新编号 + 规则化重算 verdict。**

`merge_review_passes(pass1: dict, pass2: dict) -> tuple[dict, dict]`（新增到 `review.py`，纯函数，返回 `(merged, stats)`）：

1. 取 `pass1["findings"]`（原序）与 `pass2["findings"]`（原序）。
2. 去重键 `(finding["file"], finding["line_range"], finding["category"])`（三者精确字符串相等）。已在 pass1 中出现的键，pass2 中的同键 finding 被丢弃（pass1 优先，因为它多一层 spec 上下文核实）。
3. 保留顺序：pass1 全部（原序）→ pass2 去重后剩余（原序）。
4. 按最终顺序重新赋 `id`：`F1, F2, ..., Fn`（**丢弃引擎自报的原始 id**，避免"两个 pass 各自都从 F1 编号"的必然碰撞；`REVIEW_SCHEMA.id` 的描述本身就是"建议格式"而非强约束，重新编号不违反 schema）。
5. 重新计算 `verdict`：按 `REVIEW_SCHEMA.verdict` 的既有语义规则（`review.py` 里 `BLOCKING_SEVERITIES = {"critical","high"}`）在合并后的 findings 全集上重算——存在 `severity ∈ {critical,high} and in_scope=true` → `changes-requested`；否则存在任意 finding → `passed-with-advisory`；否则 → `approve`。**不采信任一 pass 自报的 verdict**，因为两个独立判断合并后旧 verdict 不再有效。
6. 产出 `stats` side-channel：在步骤 2-3 合并期间（重编号之前，来源尚可区分时）统计 `adversarial_blocking_count`——来源于 pass2、未被去重丢弃、且 `severity ∈ BLOCKING_SEVERITIES and in_scope=true` 的 finding 数，放入返回值 `stats` dict。**telemetry 的 `adversarial_blocking_count` 取自这个 side-channel，绝不从合并后的 `round-0.review.json`（来源信息已被重编号抹去）反推。**

去重是保守的（只精确匹配三元组），因此"同一处 bug 被两个 pass 用不同措辞描述、不同 line_range"不会被去重，可能造成一次性的重复 blocking——这是刻意的取舍（宁可误报重复，不做语义去重引入新的不确定性），见 Risks。

**D5：pass2 的 `spec_attribution` 与 `in_scope` 填法靠 focus 文案里的固定指令，不靠 schema 变更。**

对抗式模板不读 spec，无法真实判断 `spec_attribution`（该字段仍是 `REVIEW_SCHEMA` 必填项）。focus 文案固定指令：无法判断时一律填 `spec-silent`（四值语义之一：spec 未规定该行为，这是唯一不要求"读过 spec"就能诚实给出的选项）。`in_scope` 同理固定指令：因为审查范围就是 diff 本身，findings 默认 `in_scope=true`，仅当讨论的是 diff 之外未修改的既有代码时才填 `false`。

**D6：pass2 失败走独立降级路径，不复用 pass1 的"整轮失败"语义。**

`run_review_round` 现有失败路径（`review_data is None` → `phase.exit status=failed` → 整轮失败）适用于 pass1：pass1 失败视为 round-0 失败，行为不变，pass2 MUST NOT 被执行。pass2（对抗）在其自身重试次数（复用同一个 `retries` 参数）耗尽后若仍未产出合法 JSON，**不使整轮失败**：合并时把 `pass2` 视为空 findings 替身 `{"findings": []}`（合并只消费 findings 数组、verdict 恒重算，替身不含 verdict 字段也不落盘，故不受 `REVIEW_SCHEMA` verdict 必填约束；`merge_review_passes(pass1_data, {"findings": []})` 等价于只用 pass1），继续走 D4 之后的既有单 pass 成功路径。

**telemetry 新字段取值矩阵（与 spec.md「telemetry 透出对抗通道运行状态」需求逐字一致，唯一权威来源）：**

| # | 情形 | round_n | adversarial_round0 | pass1 | pass2 | `adversarial_pass_ran` | `adversarial_blocking_count` |
|---|---|---|---|---|---|---|---|
| 1 | 双 pass 成功 | 0 | true | 成功 | 成功 | `true` | `int >= 0`（取自 D4 步骤 6 的 `stats` side-channel：合并期间统计的来源 pass2 且未被去重丢弃的 blocking finding 数） |
| 2 | pass2 失败降级 | 0 | true | 成功 | 失败 | `false` | `None` |
| 3 | pass1 失败（整轮失败，`ok=false`） | 0 | true | 失败 | 不执行 | `false` | `None` |
| 4 | 对抗通道禁用 | 0 | false | 成功或失败 | 不执行 | `false` | `None` |
| 5 | round>=1 | >=1 | 任意 | 既有单 pass | 不适用 | `false` | `None` |

关键点：`adversarial_pass_ran` 恒为 `bool`（情形 3/4/5 也是显式 `False`，不是 `None`/缺省/"未运行语义"的松散写法）；情形 2/3/4/5 的取值组合完全相同（`false`/`None`），但触发原因互斥（分别是"跑了但失败""pass1 就失败""配置关闭""round 不是 0"），调用方靠 `round_n`/`ok`/配置项区分，字段本身不额外编码来源。

选择"降级不失败"而非"pass2 失败即整轮失败"：对抗 pass 是新增的加成通道，不应让它的偶发不稳定（engine 抖动、schema 输出格式问题）拖累本已存在且稳定运行的 compliance 通道，二者可靠性不应被强行绑定。

**D7：顺序执行，不引入并发。**

先跑 pass1（compliance，沿用现有代码路径与产物命名），成功后再跑 pass2（adversarial）。两个变量（"引入第二次调用"与"两次调用并发执行"）分开处理：本 change 只处理前者。若未来需要为了缩短 wall-clock 时间并发跑两个 pass，是独立的后续 change（需要处理两个子进程日志交织、超时预算如何在两个并发调用间分配等新问题，不在本次范围）。

**D8：配置开关 `[review].adversarial_round0`，默认 `true`。**

`ReviewEngineConfig`（`config.py:55`）新增 `adversarial_round0: bool = True`。为 `true` 是本 change 的目的（默认开启新行为），但保留显式关闭路径以应对成本敏感场景（对抗 pass 是一次完整的额外引擎调用，token 成本翻倍到 round-0）。`round_n != 0` 时该配置项无效（round>=1 恒单通道，读它是 no-op）。

## Risks / Trade-offs

- **[精确去重漏判语义重复]** → 同一 bug 被两个 pass 用不同 file/line_range/category 描述时不会被去重，导致该轮 blocking 数虚高、多打一轮 fix。缓解：这是 D4 的刻意保守取舍；后续若 telemetry 观察到高频重复模式，可在独立 change 里升级去重键（如加入 `title` 模糊匹配），但需要新的方差证据支撑，不在本 change 预判。
- **[对抗 pass 缺 spec 上下文导致误报]** → pass2 可能标记一个 tasks.md 里明确认可的实现决策为"bug"。缓解：D5 的固定 `spec_attribution=spec-silent` 指令让这类 finding 在 telemetry 里可被 `spec_attribution_counts` 区分出来；且 round-N 模板已有"对前几轮已标注 spec-aligned 不修的 finding 不再重报"机制（`focus.py:265`），能在后续轮次吸收这类一次性噪音。
- **[round-0 token 成本翻倍]** → 每次 round-0 review 从 1 次引擎调用变 2 次。缓解：D8 的配置开关；且提案的假设是"总 review+fix token 不升"（长尾左移抵消 round-0 增量），需 telemetry 验证，不在本 change 内断言。
- **[pass2 失败静默降级可能被忽视]** → 若对抗 pass 长期失败（如 prompt 格式问题）而无人发现，等同于"配置开了但从未真正生效"。缓解：D6 的 `adversarial_pass_ran` telemetry 字段是可观测信号，`npc telemetry agg` 后续可对该字段做失败率聚合（不在本 change 范围，但字段已就位）。

## Migration Plan

1. `config.py`：`ReviewEngineConfig` 新增 `adversarial_round0: bool = True`，`__post_init__` 校验类型。
2. `focus.py`：新增 `_adversarial_round_0_template(change_id: str) -> str`（不读 project context，含 D1/D5 固定指令与四个重点关键词）。
3. `review.py`：新增 `merge_review_passes(pass1: dict, pass2: dict) -> tuple[dict, dict]` 纯函数（D4 规则，返回 `(merged, stats)`），含单元测试覆盖去重/重新编号/verdict 重算/stats 统计四条规则。
4. `pipeline.py`：`run_review_round` 在 `round_n == 0` 且 `review_cfg.adversarial_round0 is True` 时，pass1 成功后追加执行 pass2（新文件命名，D3），失败走 D6 降级路径，最终把 `merge_review_passes` 结果写入既有 `round-0.review.json` 路径再继续既有 `parse_review` 起的流程。
5. `telemetry.py`：`EMIT_FIELD_CONTRACT["review.round"]` 新增 `adversarial_pass_ran` / `adversarial_blocking_count`；`emit_review_round` 签名与 record 构造同步新增两个参数。所有调用点按 D6 状态矩阵传值：`round_n != 0` 或 `adversarial_round0 is False` 或 pass1 失败时，恒传 `adversarial_pass_ran=False`（`bool` 字面量，不是 `None`）与 `adversarial_blocking_count=None`；不影响既有 round>=1 事件其余字段的值语义——只是记录新增两个 key。
6. `docs/cli.md`：`npc review run` 一节补充双 pass 行为、新产物文件清单、`[review].adversarial_round0` 配置项、新 telemetry 字段。
7. 测试：
   - `focus.py` 负向测试：对抗式模板文本 MUST NOT 含 `proposal.md` / `tasks.md` / `specs/` / `design.md` / `project.md` / `CLAUDE.md` 字样；MUST 含四个重点关键词的中文表述。
   - `review.py` 的 `merge_review_passes` 单元测试：去重、重新编号、verdict 重算、stats 的 `adversarial_blocking_count` 统计（含被去重丢弃不计数）、pass2 为空 findings 时退化为 pass1-only（stats 计数为 0）。
   - `pipeline.py` 集成测试（monkeypatch 双 engine 调用）：round-0 两次调用均成功 → 合并产物；pass2 失败 → 降级、round 仍 `ok=true`、`adversarial_pass_ran=false`；round>=1 只调用一次引擎（回归既有行为不变）。
   - `test_structural_invariants.py` 的 `EMIT_FIELD_CONTRACT` 断言随新字段更新。
   - `config.py` 的 `adversarial_round0` 默认值与类型校验测试。
8. 回滚：`[review].adversarial_round0 = false` 全局关闭即可恢复 round-0 单通道行为，无需代码回滚；彻底回滚则删除本 change 引入的函数与字段，`round-0.review.json` 的写入路径与内容语义可完全回退到 pass1-only。

## Open Questions

无。本 change 的产物命名（D3）、合并规则（D4）、失败降级路径（D6）、配置开关（D8）均已给出确定性规则，不存在留待实施时决定的机制。
