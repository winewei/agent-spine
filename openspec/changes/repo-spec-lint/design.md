## Context

`spec-schema-hardening` 把 artifact 存在性交给 openspec 项目 schema 的 `apply.requires`。`openspec validate` 已把「Requirement 含 `SHALL`/`MUST`」与「Requirement 至少一个 Scenario」作为 ERROR 强制（实测：加不加 `--strict` 均报）。剩下的真空是**语义层**与**跨段落结构**。

四条已核实的实测事实构成本 change 的全部依据：

1. `TBD` + 含糊副词 + happy-path-only 的 spec → `openspec validate --strict` 判 `is valid`。
2. `openspec show <id> --json --deltas-only` 输出结构化解析产物（`deltas[].requirement.text`、`scenarios[].rawText`），stdout 纯净（deprecation 警告走 stderr）。
3. 对 6 个带 `design.md` 的已归档 change 做「`## Decisions` 段内联延迟措辞」检测：命中 1/6（`parallel-dag-scheduling`，2 处，`total_rounds=6` 为唯一长尾），其余 5 个零误报——其中 `reduce-review-fix-cost`（OQ 4 条 / 1 轮）与 `robust-orchestrator-json-parsing`（OQ 2 条 / 0 轮）的未决项均正确声明在 `## Open Questions` 内，被正确放行。
4. 在编写姊妹 change 期间发现的误报模式：`openspec/changes/spec-schema-hardening/` 的三个文件出现 `TBD`，**全部是在陈述该规则本身**，且均位于反引号代码 span 或引号列表内。

## Goals / Non-Goals

**Goals**

- 补上 openspec 不管的语义层，且只在有方差证据处立硬门。
- 让「哪条规则从未触发」可被观察，从而支持后续做减法。
- 复用 openspec 的解析产物，不在仓库内制造第二个 markdown parser。
- 严守 `CLAUDE.md` 的 npc 职责边界：本 change 零 npc 变更。

**Non-Goals**

- 不改 `src/npc/`，不新增 npc 子命令。
- 不实现 openspec 已强制的三类校验（SHALL/MUST、Scenario 存在性、artifact 存在性）。
- 不把无方差证据的规则升为硬门。
- 不扫描 `openspec/specs/`。
- 不 emit telemetry（脚本不依赖 npc）。
- 不接入 `/spine-run` 闸口。
- 不做评分、排名、跨 spec 对比。

## Decisions

**D1：落点是仓库本地脚本，不是 npc——分界是规则内容的通用性，不是「有没有 lint 引擎先例」。**
先纠正一个曾被用作论据的错误事实：npc **已经**持有一个自解析 markdown 的实现前 spec 闸门。`src/npc/spec_analyze.py` 的模块 docstring 自述「npc spec analyze —— 实现前的 spec↔tasks 漂移/覆盖确定性闸门……不依赖 openspec CLI，不需要 active run」。因此「npc 白名单不含 lint 引擎」这个论证是**站不住的**。

真正的分界在**规则内容**：`npc spec analyze` 检查的是 artifact 之间的结构一致性（`capability-no-spec` / `orphan-spec` / `no-tasks`），对任何使用 openspec 的项目都成立，零项目品味。而本 change 的规则内容——中文延迟措辞词表、含糊副词表、「必须有 Non-Goals 段落」——是 agent-spine 的写作品味，外部无任何来源要求。按 `CLAUDE.md`（npc 只放跨项目通用的原子操作；项目的业务校验属各自仓库的 `scripts/check-*` 家族），词表 MUST 留在仓库脚本内。

被否决的备选方案是「引擎进 npc、词表进 `.npc/config.toml`」：它需要为一个只有单一消费者的词表引入一层配置，制造第二个可能与脚本漂移的真相源，收益为零。

npc 与本脚本的接口留到 `spine-spec-writer`：到那一步 `npc spec review run` 通过 `.npc/config.toml` 的 `[spec_review] gate_cmd` 调用本脚本，并把其 JSON 中的 `rule_hits` **原样透传**进 telemetry。npc 因此不需要知道任何规则名——这与既有的 `[verify] test = "uv run pytest -q"` 完全同构：npc 不硬编码「测试命令是什么」，只硬编码「要真实复跑测试」。减法信号（哪条规则从未触发）由透传的 `rule_hits` 保住，边界也保住。

**D2：四条规则全部以 `warning` 交付（shadow mode），本版本不设任何阻断门。**
`deferred_decision_outside_open_questions` 是四条中唯一有方差证据的（文件级精确率 1/1、误报率 0/5、唯一命中者恰为唯一长尾）。但证据强度不足以立阻断门：**正类样本 N=1、因果未证**，且 D2b 记录的反面事实直接削弱其作用机理——引发该长尾 r0 blocking 的那句留白本就诚实声明在 `## Open Questions` 内，本规则会放行它。按不变量 3「新硬轨须被真实方差打出来」严格读，N=1 且机理被削弱的证据只支持**观察**，不支持**阻断**。

因此：四条规则一律 `warning`，只产出 `rule_hits`。`errors` 通道与 exit-code 语义在本 change 一并定义好，供未来零改动升级。

**升级判据**（MUST 写入脚本 docstring，避免日后凭感觉升级）：当 `spec_review.round` 或 code review 的 `spec_attribution` 聚合显示某规则命中与 `spec-silent` / `spec-ambiguous` / `spec-contradicted` 类 blocking 存在跨 change 的稳定关联（**正类样本 ≥ 3 个独立 change**）时，方可升为 `error`。反之，若某规则在观察窗口内长期零触发，按 `npc telemetry cages` 的既有减法纪律删除它。

**D2b：被证伪的备选规则——「`## Open Questions` 必须为空才能进 implement」。**
这条规则看起来更强、更能防缺陷，但被本仓库语料**直接证伪**：`robust-orchestrator-json-parsing` 带着 2 条未决 Open Questions 进 implement，`total_rounds=0`；`reduce-review-fix-cost` 带着 4 条，`total_rounds=1`。该规则会拦下这两个收敛良好的 change，误报率 2/3。放弃。

与之相关的一条**必须记录的反面事实**：`parallel-dag-scheduling` 的 r0 blocking F3 所对应的留白，本来就正确声明在 `## Open Questions` 内（`design.md:104`），本 change 的规则会放行它。规则之所以对该文件报错，是因为同一决策同时泄漏进了 `## Decisions` 正文（`design.md:43`）。**诚实声明留白并没有阻止该缺陷。** 本规则治的是写作诚实性，不是缺陷预防——收益账不得记到「防缺陷」名下。

**D2c：回归 fixture 必须是快照，不是活体目录引用。**
把「对 `openspec/changes/spec-schema-hardening` 跑脚本应零误报」写成永久测试，等于把测试真值绑在一个会持续演进（并最终被 archive）的目录上。故把两份真实语料**快照**进 `tests/fixtures/`：负例取 `spec-schema-hardening` 合入时的三个 artifact，正例取 `archive/2026-07-03-parallel-dag-scheduling/design.md`。「对全仓 active change 跑一遍确认零误报」降级为**一次性人工验证任务**，不进永久测试。

**D2d：`--change` 只接受单段 id，archive 与 fixture 走 `--dir`。**
`--change <id>` 映射到 `openspec/changes/<id>/`，MUST 拒绝含 `/` 或 `..` 的取值（`invalid_change_id`），防止路径穿越。archive 语料与测试 fixture 位于该目录之外，故另开 `--dir <path>` 入口。`--dir` 模式下 `openspec show <id>` 无从调用（它只认 active change id），因此依赖 spec delta 解析的两条规则（`scenario_missing_when_then` / `vague_adverb`）MUST 被跳过并在 `rule_hits` 中记 `0`——这是有意的能力缺口，不是 bug。

**D3：匹配必须跳过 inline code span 与 fenced code block。**
不这么做，任何讨论该规则的文档都会自我触发——这不是假想，姊妹 change `spec-schema-hardening` 的三个文件已构成活的误报语料。实现上先剥离围栏块、再剥离行内 span（被剥离处以等长空白占位以保持行号对应），然后才做段落定界与措辞匹配。该语料直接固化为回归 fixture，防止后续实现退化。

**D4：段落定界以 `## Open Questions` 为界，而非全文匹配。**
规则的意图不是「禁止推迟决策」——推迟决策本身是合法且诚实的。意图是**禁止把未决决策藏在 `## Decisions` 的正文里**，让它看起来像已拍板。因此合法出口是显式的 `## Open Questions` 段落：它把隐形留白变成必须被 reviewer 看见并签字的显式契约。这也正是 openspec 内置 `spec-driven` schema 中 design.md 模板已有 `**Open Questions**: Outstanding decisions or unknowns to resolve` 一节的原始意图——本 change 只是把它从建议变成强制。

**D5：`design.md` 缺失时跳过该规则，而非报错。**
schema 的 design instruction 明确写道 design.md「create only if any apply」，即条件产物。29 个已归档 change 中仅 6 个有 `design.md`。对无 design.md 的 change 报错等于强制每个琐碎 change 写设计文档。

**D6：解析 spec delta 一律走 `openspec show --json --deltas-only`。**
自建 markdown parser 会与 openspec 的解析器漂移——尤其 openspec 对 `#### Scenario:` 的四井号要求「用三井号或 bullet 会静默失败」，这类边角语义不该重新实现一遍。代价是脚本依赖 openspec 二进制在 `PATH`；缺失时输出稳定的 `openspec_missing` 结构化错误，不崩溃。只读 stdout（deprecation 警告在 stderr，不污染 JSON）。

**D7：`rule_hits` 必须含零命中的规则名。**
只报告命中的规则，就无法区分「规则不存在」与「规则存在但零命中」。后者是减法信号的唯一来源。故 `rule_hits` 的键集合恒等于全部规则名集合。

**D8：词表以模块级常量落在脚本内，不做配置化。**
本脚本本身就是 agent-spine 的项目资产，词表改动直接改脚本、走 git diff 即可。引入一层配置只会增加一个可能与脚本漂移的真相源，且没有第二个消费者。

**D9：词表条目 MUST 是「延迟 + 决策动词」的谓语短语，MUST NOT 是裸的时间副词。**
本决策由 dogfood 实测逼出：把裸的 `届时` 放进词表后，它在本文档自身命中 2 次——两处都是「到那时」的普通时间副词用法（"接口留到 spine-spec-writer，届时…"），与推迟决策无关。同理，裸的 `再定` 是句子片段，`实现时` 是普通时间状语。

因此词表只收**自身即表达「决策未拍板」**的短语：`实施时定` / `届时决定` / `届时再定` / `实现时再定` / `后续再定` / `待定` / `暂定` / `后补` / `TBD` / `TODO` / `to be determined` / `decide later`。新增条目前 MUST 先对全仓 `openspec/changes/**/design.md` 跑一遍确认零误报——这是本 change 的回归 fixture 覆盖的场景之一。

## Risks / Trade-offs

- **[延迟措辞可被同义改写绕过]** → 用词表之外的同义表述即可逃逸。缓解：**接受**。该规则不是安全控制，而是防止**无意识留白**的减速带；有意绕过它的人也会有意绕过任何静态规则。真正拦截语义缺陷的是后续 `spine-spec-writer` 的 codex spec review（语义门）。本 change 的收益账**不得**把语义门的收益记进来。
- **[误报会侵蚀信任]** → 一次误报就足以让人绕过 lint。缓解：D3 的 code-span 剥离 + D9 的词表约束 + 以真实语料快照作正负回归 fixture；且本版本**零阻断门**，误报的代价只是一行 warning。
- **[脚本与 npc 双轨，可能出现「谁来跑它」的空白]** → 本 change 只提供脚本，不接任何闸口。缓解：这是有意的分期；接线在 `spine-spec-writer` 中通过 `[spec_review] gate_cmd` 完成，那个 change 自带独立的 spec 与测试覆盖。
- **[与 `npc spec analyze` / `npc spec-report` 的命名相邻]** → `spec-report` 是**交付后**的 agent 表现回执，与 spec 写作无关。三者职责必须在 `scripts/README.md` 中并列澄清，避免后续误用。本脚本不进 npc CLI，故不碰 `docs/cli.md`（那是 npc 的契约文档）。

## Migration Plan

1. 新增 `scripts/check_spec.py`（纯函数为主：`strip_code_spans`、`section_of_line`、`lint_change`），模块 docstring 载明升级判据。
2. 把两份真实语料快照进 `tests/fixtures/`，作为正负回归 fixture。
3. 一次性人工验证：对全仓 active change 跑一遍，确认零误报（不进永久测试）。
4. 回滚：删除脚本文件即可。无持久化状态、无对既有命令的行为改动、无 npc 依赖。

## Open Questions

无。十一个决策点（落点、全 warning 交付、被证伪的更强规则、fixture 快照化、`--change`/`--dir` 入口、code-span 剥离、段落定界、design.md 缺失处理、解析产物来源、`rule_hits` 零命中语义、词表是否配置化）均已在上文定稿，且各自给出可复现的依据或明确的不变量援引。不存在留待实施时决定的机制。
