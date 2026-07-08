## Context

`/spine-run` Step 2B 目前由主 session 自己拆 change 并补齐 artifact（`plugins/agent-spine/commands/spine-run.md`），无质量门、无评审。而 spec 是整条流水线的上游契约——它是唯一未经独立验证就进入 implement 的产物。

既有可复用的结构（均已核实）：

- `spine-coder` 是 subagent，收一段薄引导语，第一步 Read npc 渲染的 prompt 文件绝对路径，最后回报一行 RESULT。
- `RESULT_REQUIRED_KEYS: dict[str, frozenset[str]]`（`src/npc/pipeline.py:1180`）按 phase 声明必需键，`_parse_and_validate_result_line` 缺键即拒。
- `npc agent prompt|spawn-prompt|timeout-budget|record-timeout` 四件套已封装 in-session subagent 的分发与超时预算。
- `src/npc/schema.py` 的 `REVIEW_SCHEMA` 是 codex review 输出的单一事实源；`ensure_schema` 负责落盘。
- `src/npc/agent.py` 的 `_default_review_path` 恒返回 `round_n - 1` 的 review 路径，且该模块不 import `focus`。

## Goals / Non-Goals

**Goals**

- 把 spec 生成从主 session 剥离到专职 subagent，与 `spine-coder` 结构同构。
- 给 spec 加一道**独立的语义评审**，且它与 spec 生成方不同源。
- 让确定性门（便宜）永远先于语义门（昂贵）执行。
- 全程与既有 code 流水线物理隔离：两条链的 findings/rubric 互不可见。

**Non-Goals**

- 不接管 `/spine-run` Step 2B（独立 change，需先有本 change 的收敛 telemetry）。
- 不复用 code review 的 stale 检测。
- 不引入基于 `spec_attributable_blocking_rate` 的闸门。
- 不改变任何既有 code 流水线命令的行为。
- 不做 spec 评分、排名、跨 spec 对比。

## Decisions

**D1：不变量 1 的边界是时点，不是内容。**
`principles.md` 的散文措辞与 `templates.py` 的代码级注释（`此清单 MUST NOT 包含当次 change 的 review focus 渲染文本、上轮 findings 原文、或 reviewer 的评分 rubric 细则`）曾被读作互相矛盾：前者似乎只约束「谁与谁同源」，后者明令生成侧不得见验证内容。而事实上 `spine-coder` 在 fix 轮**确实**逐字读上一轮 blocking findings。

两个事实同时为真，唯一自洽的解释来自代码：`src/npc/agent.py` 的 `_default_review_path` 恒解析 `round_n - 1`，且 `agent.py` 不 import `focus`（渲染 rubric 的模块）。即生成侧在结构上只可能拿到**上一轮已签发**的判定结果，永远拿不到**本轮**的评判标准。

> 判定签发**前**，生成者不得预知评判标准；判定签发**后**，读 findings 是整改的必要输入。

`templates.py` 那行注释约束的是 implement 轮的自检清单（判定尚未发生），与 fix 轮读 findings 不矛盾。故 `spine-spec-writer` 与 `spine-coder` 严格同构：write 轮不得见 spec-review rubric（含 `category` 枚举列表本身），fix 轮可读上一轮已签发的 findings。此结构由 `spec-routing-invariant` 在配置层保证不同源，由本 change 的负向测试在渲染层保证不泄漏。

**D2：spec review 自有 schema 与 category 枚举，不复用 `REVIEW_SCHEMA`。**
code review 的 category（`validation`/`error-handling`/`edge-case`…）描述**代码缺陷形态**；spec review 需要的是**规范缺陷形态**（`ambiguity`/`missing-scenario`/`implementation-leak`/`untestable`/`deferred-decision`/`contradiction`/`scope-creep`）。两者语义不交叠。且 spec 无 diff 作用域概念，故 finding 不含 `in_scope`；`spec_attribution` 是 code review 对 spec 的归因，反向挂在 spec review 上没有意义。用 `enum` 而非自由文本 —— 顺带修正 `REVIEW_SCHEMA.category` 无 enum 约束的既有缺陷（该缺陷本身不在本 change 范围）。

**D3：质量门按成本递增：`openspec validate --strict` → 配置的 gate 命令 → codex spec review。**
前两道是确定性的、毫秒级、零 token。让它们先跑，能在语义门之前拦掉结构性废品。第二道门经 `.npc/config.toml` 的 `[spec_review] gate_cmd` 调用（本仓库配为 `uv run scripts/check_spec.py`），npc 只解析其 JSON 的 `ok`/`rule_hits`，**不持有任何规则内容**——这守住 `CLAUDE.md` 的 npc 白名单（生命周期钩子 / telemetry / state 读写 / 路由不变量），与 `[verify] test` 同构。`gate_failed` 字段使失败位置可被 telemetry 观察，从而回答「有多少次 spec review 其实根本没必要烧 token」。lint 的 `warning` 不阻断——warning 是观察信号，不是门。

**D3b：`gate_cmd` 为 argv 数组，npc 追加 `--change <id>`，`shell=False`。**
字符串形态需要 shell 解析，会把 `--change <id>` 的拼接变成注入面。argv 数组 + 固定追加两个元素 + `shell=False`，从形态上杜绝注入。npc 只读 gate 输出的 `ok` 与 `rule_hits` 两个键——**读规则名就等于持有规则语义**，故加负向测试断言 npc 源码中不出现任何规则名字符串或词表常量。`gate_cmd` 未配置 → `gate_skipped=true` 继续；命令不可执行或输出非合法 JSON → 视为门失败（`gate_output_invalid`），**不静默放行**。

**D3b2：`gate_cmd` 返回 `ok == false` 的分支不是死代码。**
`repo-spec-lint` 交付时四条规则全为 `warning`，故本仓库配置的那个 gate 命令在 v1 里**永远返回 `ok == true`**。这容易让实现者把 `ok == false` 分支当死代码删掉。不可以：`gate_cmd` 是 npc 侧的通用机制，`ok == false` 可由任意项目的任意 gate 命令产生，也可由「命令不可执行」「stdout 非合法 JSON」两条路径产生。该分支的测试 MUST 用**桩 gate 命令**（一个直接打印 `{"ok": false, ...}` 的脚本），MUST NOT 依赖 `scripts/check_spec.py` 的实际行为——否则测试会随 `repo-spec-lint` 的规则升级而意外变绿/变红。

**D3c：评审结果写轮次化路径 `round-{N}.spec-review.json`。**
原设计写单一 `spec-review.json`，与「fix 第 N+1 轮只读第 N 轮」的时点边界直接冲突——同一路径存不下两轮。轮次化命名与既有 code review 的 `round-{N}.review.json` 一致，且使 `npc spec fix run --round N` 能确定性地解析 `round-{N-1}.spec-review.json`（与 `agent.py:_default_review_path` 同构）。上一轮文件不存在时 fix MUST 拒绝渲染（`prev_spec_review_missing`），不得静默降级为「无 findings」。

**D4：固定 fix 次数上限，明确拒绝移植 stale 检测。**
code review 的 `rounds_since_strict_decrease` 隐含前提是「blocking 单调下降代表收敛」。这在代码域成立：缺陷是有限集，修一个少一个。在 spec 域**不成立**：改写一段 spec 以消除 `ambiguity`，完全可能引入新的 `scope-creep` 或 `contradiction`，blocking 数可以反弹。且阈值 `3` 是从 code review 的 telemetry 方差中标定的经验常数，把它当通用原语搬运，正是不变量 3 禁止的「未被真实方差打出来的硬轨」。spec review 目前**零 telemetry 样本**。

因此本 change 只用固定上限作为**防失控兜底**。语义定死：`max_rounds = N` 表示「最多 `N` 次 spec fix」，review 轮次索引 `0..N`（共 `N+1` 次 review）。`N=0` 是合法取值，表示只审不修。达上限仍有 blocking 即 `needs-user-decision` 交人，绝不自动 archive。等 `spec_review.round` 攒够样本，再谈是否需要收敛判据。

**D5：`spec_write` 的 RESULT 必需键不含 `commit`。**
spec writer 的产物是 `openspec/changes/<id>/` 下的 artifact 文件，不是代码 commit。沿用 `implement` 的 `commit`/`tests` 键会逼它做无意义的动作。改为 `{change, artifacts, validate, summary}`：`artifacts` 列出写了哪些文件，`validate` 记录 `openspec validate --strict` 的自检结果。注意 `validate` 是 writer 的**自报**，不构成信任来源——真相仍由 `npc spec review run` 内部重新执行的确定性门给出（对齐「不裸信 RESULT 自报」的既有纪律，见 `npc verify tests`）。

**D5b：职责边界靠确定性校验，不靠 prompt 文案。**
`spine-spec-writer` 需要 `Bash`（跑 `openspec validate` 自检），而 `Bash` 天然能 `git commit`、能改 `src/npc/`。「在 agent 契约里写一句不许提交代码」是散文约束，违反不变量 2 的精神。故把边界做成确定性校验：`npc spec write|fix record` 在装订 RESULT 前用 `git status --porcelain` 取变更集，任何落在 `openspec/changes/<id>/` 之外的路径 → `out_of_scope_changes` 拒绝装订；record 前后 `HEAD` 变化 → `unexpected_commit` 拒绝。文案约束仍写，但只是第二道；第一道是代码。

**D5c：v1 恒 in-session；路由合法性的真相源唯一，是 `check_routing`。**
`npc agent` 的 dispatch/timeout 四件套与 `SUPPORTED_DISPATCH_VALUES` 是为 implement/fix 语义标定的。给 spec writer 引入第二套分发路径会同时改变「谁写 spec」与「怎么调度」两件事，任一出错都难归因。v1 恒返回 `deferred=true`。超时预算复用既有四件套，phase 名 `spec_write` / `spec_fix-r{N}`。这也让 `spec-routing-invariant` 无需为 spec 侧定义 dispatch 配置。

曾考虑在 `npc spec write run` 里自己判一句「backend 是 mimo 就报 `spec_writer_backend_unsupported`」。**否决**：那是第二个后端白名单，与 `check_routing` 必然漂移——正是 `spec-routing-invariant` 在修的那类 bug（codex 进了 coder 白名单，同源判定却没跟上）。改为：`spec write|fix run` 在渲染 prompt 之前调 `check_routing(cfg)`，任一 `spec_` 前缀 violation 即以 `spec_routing_violation` 拒绝，并原样透出 `rule` / `detail`。

两个名字各司其职、不重复：`spec_mimo_in_session` 是**规则名**（`npc verify routing` 的输出，配置期），`spec_routing_violation` 是**命令级错误标识**（`npc spec write run` 的输出，运行期）。运行期这道检查不可省——用户完全可能不跑 `npc verify routing` 就直接 `/spine-spec`。加负向测试断言 `spec_pipeline.py` 不含任何 `SUPPORTED_SPEC_*` 常量。

**D6：`gate_failed` 时仍 emit telemetry。**
若门失败就不 emit，则「有多少 spec 连结构门都没过」这个信息永远丢失，而它恰恰是衡量 spec writer 质量的第一手信号。故门失败时 emit 事件、`gate_failed` 非空、`verdict` 为 `null`（不是 `"changes-requested"`——没跑评审就没有 verdict，缺数据不得伪装成判定结果）。

**D7：不接管 Step 2B。**
把主 session 的拆解逻辑替换成 spawn `spine-spec-writer` 是一个诱人的一步到位，但它会同时改变两件事：spec 的生成者，以及 `/spine-run` 的控制流。任一出错都难以归因。本 change 只提供 `/spine-spec` 独立入口，其产物可被 `/spine-run <change-name>` 直接消费。等 `spec_review.round` 的收敛行为被观察到之后，再开独立 change 做接管。

## Risks / Trade-offs

- **[spec fix 循环可能不收敛]** → 改写 spec 消除一类 finding 可能长出另一类。缓解：固定轮次上限 + `needs-user-decision` 交人；`spec_review.round` 的 `blocking_categories` 序列会直接暴露「反弹」形态，为后续设计收敛判据提供方差证据。
- **[两条 review 链的 findings 可能互相污染]** → 两者的 JSON 落在同一 run 目录下，且都是 `round-N.*` 前缀。缓解：文件名区分为 `round-N.review.json`（code）与 `round-N.spec-review.json`（spec），并加**双向**负向测试：implement prompt 不含 spec review 内容；spec write prompt 不含 code review 内容。
- **[`spine-spec-writer` 拥有 Bash 即可提交代码]** → 越界风险，且 prompt 文案挡不住。缓解见 D5b：`record` 前的 `git status --porcelain` 变更集校验 + `HEAD` 未变校验，均为确定性硬轨。
- **[新增两个 telemetry kind 与两个 RESULT phase]** → `EMIT_FIELD_CONTRACT` 与 `RESULT_REQUIRED_KEYS` 均有结构测试守护，成本已知。回归测试须显式断言既有 phase 的必需键集合未被改动。

## Migration Plan

1. 新增 `SPEC_REVIEW_SCHEMA` 与其落盘（复用 `spec-attribution-telemetry` 修好的 `ensure_schema` 语义相等重写逻辑）。
2. 新增 `src/npc/spec_pipeline.py`：`spec write run|record`、`spec fix run|record`、`spec review run`。
3. `RESULT_REQUIRED_KEYS` 加 `spec_write` / `spec_fix`；telemetry 加 `spec_review.round`。
4. 新增 subagent 与命令文件，注册进 `plugin.json`。
5. 端到端 fixture 分两条，**不可混为一谈**：
   - **`gate_cmd` 失败路径**用**桩 gate 命令**（stdout 恒为 `{"ok": false, "rule_hits": {}}`）构造。MUST NOT 用 `scripts/check_spec.py`——它交付时四条规则全为 `warning`，恒返回 `ok == true`（`repo-spec-lint` 的 D2），拿它构造失败会写出一个永远失败的测试。
   - **`rule_hits` 透传路径**用真实的 `scripts/check_spec.py` + 归档的 `parallel-dag-scheduling` 语料构造：期望 `ok == true`、`gate_rule_hits["deferred_decision_outside_open_questions"] == 2`、**继续进入 LLM 语义门**。
6. 回滚：删除 `npc spec write|fix|review` 子命令注册与两个 plugin 文件。无持久化状态迁移；既有 code 流水线未被触碰。

## Open Questions

无。十二个决策点（不变量 1 的时点边界、spec review 自有 schema、质量门顺序、`gate_cmd` argv 契约、`ok==false` 分支非死代码、轮次化 review 路径、拒绝移植 stale、RESULT 键集合、越界的确定性校验、v1 恒 in-session 且路由真相源唯一、门失败仍 emit、不接管 Step 2B）均已在上文定稿，各自给出代码级依据或明确的不变量援引。不存在留待实施时决定的机制。
