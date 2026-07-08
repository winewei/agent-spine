## Why

`spec-schema-hardening` 之后，artifact 存在性已由 `apply.requires` 硬门守住。但**语义层**仍完全不设防。实测（openspec 1.1.1）确认：一个 Requirement 写 `The system SHALL handle input appropriately and quickly. TBD: error codes.`、Scenario 正文写 `It just works, trust me.`（无 WHEN/THEN）、只有 happy path 的 spec，`openspec validate --strict` 判定 `is valid`。

一条规则具备可复现的方差证据。对 6 个带 `design.md` 的已归档 change 做「`## Decisions` 段内联延迟措辞」检测（先剥离 code span 与围栏块，再按 `##` 段落定界）：

| change | Decisions 段内联留白 | `total_rounds` |
|---|---|---|
| `parallel-dag-scheduling` | **2 处** | **6**（全仓唯一长尾） |
| `reduce-review-fix-cost` | 0 | 1 |
| `robust-orchestrator-json-parsing` | 0 | 0 |
| `coder-dispatch-routing` / `npc-init-worktree-lifecycle` / `finalize-ff-merge-teardown` | 0 | 无 telemetry |

命中 1/6，且命中者恰为唯一长尾；其余 5 个零误报。**关键的是后两行**：`reduce-review-fix-cost` 与 `robust-orchestrator-json-parsing` 的 `## Open Questions` 段分别有 4 条与 2 条未决项，却分别只跑了 1 轮与 0 轮——它们被本规则**正确放行**。

**因此本规则的语义不是「禁止推迟决策」，而是「禁止让未决决策伪装成已拍板的决策」。** 推迟本身无害且诚实；把它写进 `## Decisions` 的正文才有害。

**必须同时记录一条反面事实，否则这条规则会被过度推销**：`parallel-dag-scheduling` 的 r0 blocking finding F3（pointer 文件从未接进 `load_paths`）所对应的那句留白，**本来就正确地写在 `## Open Questions` 段内**（`design.md:104`），本规则会放行它。规则之所以对该文件报错，是因为**同一个决策同时泄漏进了 `## Decisions` 正文**（`design.md:43`：「…或写入指向父 run 的 pointer 文件，二选一实施时定」）。换言之：**把留白诚实声明进 Open Questions 并没有阻止该缺陷发生。** 本规则拦的是「伪装成决策的留白」这一**写作诚实性**问题，**不是**缺陷预防机制。

精确率 1/1（文件级），误报率 0/5。但**这不足以立一个默认阻断门**：正类样本 N=1、因果未证、且上一段的反面事实直接削弱了它的作用机理。按不变量 3 严格读，**本 change 交付的四条规则 severity 一律为 `warning`**（shadow mode），只产出 `rule_hits` 计数。

**升级判据**（写入脚本 docstring）：当 `spec_review.round` 或 code review 的 `spec_attribution` 聚合显示某规则命中与 `spec-silent`/`spec-ambiguous`/`spec-contradicted` 类 blocking 存在跨 change 的稳定关联（**正类样本 ≥ 3 个独立 change**）时，方可将该规则升为 `error`。`errors` 通道与 exit-code 语义在本 change 中一并定义，供未来升级时零改动启用。

同时已识别一个真实的误报模式：`openspec/changes/spec-schema-hardening/` 的 3 个文件出现 `TBD`，**全部是在讨论该规则本身**，且均位于反引号代码 span 或引号列表内。朴素子串匹配会误报。此语料 MUST 作为回归 fixture。

**落点必须是仓库本地脚本，不是 npc——但理由不是「npc 没有 lint 引擎先例」。** 事实上 `src/npc/spec_analyze.py` 明确自述为「实现前的 spec↔tasks 漂移/覆盖确定性闸门……不依赖 openspec CLI，纯读文件 + 解析 markdown」，即 npc **已经**持有一个自解析 markdown 的 spec 质量闸门。

真正的分界是**规则内容的通用性**：`npc spec analyze` 检查的是 artifact 之间的结构一致性（proposal 声明的 capability 有没有对应 spec.md、spec 有没有对应 tasks），这对任何使用 openspec 的项目都成立，零项目品味。而本 change 的规则内容——中文延迟措辞词表、含糊副词表、「必须有 Non-Goals 段落」——是 agent-spine 的写作品味，外部没有任何来源要求它们。按 `CLAUDE.md`（npc 只放跨项目通用的原子操作；项目的业务校验属各自仓库的 `scripts/check-*` 家族），这些词表 MUST 留在仓库脚本内。

## What Changes

- **新增** 仓库本地脚本 `scripts/check_spec.py`，以 `uv run scripts/check_spec.py --change <id>` 调用，stdout 输出单行结构化 JSON。
- **新增** 四条规则，**severity 一律为 `warning`**（shadow mode，不阻断）：`deferred_decision_outside_open_questions`、`scenario_missing_when_then`、`vague_adverb`、`proposal_missing_non_goals`。
- **新增** `deferred_decision_outside_open_questions` 的判定语义：`design.md` 中 `## Open Questions` 段落**之外**出现延迟决策措辞即命中。匹配 MUST 跳过 fenced code block 与 inline code span，MUST 段落定界。词表条目 MUST 是「延迟 + 决策动词」的谓语短语，MUST NOT 收录裸的时间副词（实测：裸的 `届时` 会在普通时间状语上误报）。
- **新增** `errors` 通道与退出码语义（有 error → 1），本版本无 error 级规则，供未来升级零改动启用。
- **新增** `--change <id>`（单段 id，拒绝 `/` 与 `..`）与 `--dir <path>`（供 fixture 与 archive 语料）两种入口。
- **新增** `rule_hits` 输出：键集合恒为全部规则名（含零命中），使「某规则从未触发」成为可观察的减法信号。
- **新增** 回归 fixture：把两份真实语料**快照**进 `tests/fixtures/`——`spec-schema-hardening` 的三个 artifact 为负例（零误报），`archive/2026-07-03-parallel-dag-scheduling/design.md` 为正例（命中 2 处）。快照而非引用活体目录，避免测试真值随仓库演进而漂移。

**非目标（Non-Goals）**：

- **不改任何 `src/npc/` 源码**，不新增任何 npc 子命令。若本 change 需要改 npc，说明落点选错了。
- **不自行解析 spec delta 的 markdown 结构**。Requirement / Scenario MUST 取自 `openspec show <id> --json --deltas-only` 的结构化产物。`design.md` / `proposal.md` 的行级段落扫描（识别 `##` 标题、剥离 code span）不属此列——那不是 delta 解析，也不构建 AST。
- 不扫描 `openspec/specs/`。实测该目录 33/33 个文件的 Purpose 段含 openspec `archive` 工具自动插入的桩文本，扫描必然全红。范围 MUST 限定为 `openspec/changes/<id>/`。
- 不把软规则升为硬门（无方差证据，守不变量 3）。
- 不校验 artifact 存在性（已由 `spec-schema-hardening` 的 `apply.requires` 覆盖，避免两套真相源）。
- 不校验 Requirement 含 `SHALL`/`MUST`、不校验 Scenario 存在性（`openspec validate` 已作为 ERROR 强制，加不加 `--strict` 均如此；重复实现会制造漂移）。
- 不 emit telemetry（脚本不依赖 npc；telemetry 由后续 `spine-spec-writer` 的 `npc spec review run` 透传 `rule_hits` 完成）。
- 不接入 `/spine-run` 的任何闸口。
- 不做 spec 评分、跨 spec 对比或质量排名。

## Capabilities

- **New Capabilities**: `repo-spec-lint` —— agent-spine 仓库本地的确定性 spec 静态语义检查，硬门只覆盖有方差证据的规则，其余规则以 `rule_hits` 观察其触发率。

## Impact

- **受影响代码**：新增 `scripts/check_spec.py`、`tests/test_check_spec.py`。**零 `src/npc/` 变更。**
- **兼容性**：纯新增脚本。不改变任何既有命令的行为与退出码。
- **CLAUDE.md 边界**：本 change 完全落在「各自项目仓库的 `scripts/check-*` 家族」一侧，不触碰 npc 的四项白名单。npc 与本脚本的接口留到 `spine-spec-writer`：届时 `npc spec review run` 通过 `.npc/config.toml` 的 `[spec_review] gate_cmd` 调用本脚本，并把其 JSON 中的 `rule_hits` 原样透传进 telemetry——npc 不需要知道任何规则名，与 `[verify] test = "uv run pytest -q"` 是同一模式。
- **不变量影响**：
  - 不变量 1（生成⊥验证）：lint 是**确定性静态判定**，非 LLM 评审，不产生任何 rubric。**不适用**。
  - 不变量 2（不信 LLM 散文）：输出为结构化 JSON。**满足**。
  - 不变量 3（新硬轨须被真实方差打出来）：**本 change 不立任何硬轨**。四条规则一律 `warning`，只产出 `rule_hits` 观察信号。唯一有方差证据的 `deferred_decision_outside_open_questions` 也是 warning——N=1 且机理被反面事实削弱，不足以阻断。`errors` 通道与退出码语义已定义，供未来按明文升级判据（正类样本 ≥ 3 个独立 change）零改动启用。**满足**。
