# run-lessons-feedforward — Design

## Context

`spine-run` 的一个 run 内跑多个 change（`plan_order`）。`parallel-dag-scheduling`（已 archive）已经把执行组织成 DAG 分层 + 层屏障，并且每个 change 的 fix 循环里 fixer 已经在 RESULT 行结构化自报 `categories_scanned` / `regressions_added` / `notes`（`docs/cli.md` §8a `npc fix record`），落盘到该 change 的 `<base>/events.jsonl`（真实行形态：`event=fix.done`, `phase=fix-rN`——per-change events.jsonl 用 `event` 字段命名事件，行内不含 `kind`/`status`；`kind=phase.exit`+`status=done` 是另一条 telemetry 派生流的形态且不携带这三个字段）。这些字段目前只用于该 change 自己的 `spec-report`（§8c）自报核验，run 内其他 change 完全看不到。

Bun 迁移复盘（`docs/optimization-proposals/2026-07-09-bun-migration-lessons.md` 提案 3）的核心洞察：不需要专门设计一个"试点阶段"，多 change plan 里第一个完成的 change 天然就是可以免费获得的 trial run——只要把它暴露的失败模式喂给后面的 change。

## Goals / Non-Goals

**Goals:**

- run 内后完成的 change 能看到同 run 内先完成的 change 的失败模式（结构化、确定性、非 LLM 摘要）。
- 首个 change 不需要任何额外配置就自动成为"试点"——lessons.md 从空文件开始，随每个 archived change 增长。
- 在已有的 DAG 层屏障同步点上，给"用 lessons 反过来修订下游未开始 change"一个显式、可审计、默认保守的通道，而不是让这个诉求野生地长成主 session 里的临时判断。
- 全部机械动作（提炼字段、拼 markdown、判定候选集、游标推进）归 npc；唯一的生成动作（真的改 tasks/design 的文字）仍归 `spine-spec-writer`，且走已有的 `spec write` 三件套，不新造一个 agent。

**Non-Goals:**

- 不做"跨 run"的 lessons 积累——`lessons.md` 落在 `run_dir` 下，run 结束（finalize）后不再被读取、不合并进任何全局知识库。跨 run 沉淀是另一个量级的问题（需要去重、时效衰减、误报过滤），留给指标证明需要时再做（不变量 3）。
- 不对 lessons.md 的内容做任何 LLM 摘要、聚类或"总结教训"——npc 只做字段级确定性拼接。
- 不允许 pilot-rewrite-gate 修订**已经开始**（implement_commit 已存在）或**已终态**的 change——候选集严格限定为 `status == "pending"`，避免和进行中的 fix 循环产生竞态或撤销既成实现。
- 不在 gate 里做语义相关性判断（比如"这条 lesson 是否真的适用于这个下游 change"）——那是生成动作，留给 `spine-spec-writer` 在 write 轮里用它的判断力决定要不要真的改；npc 只给"存在候选 + 存在新增 lessons"这个确定性事实。

## Decisions

### D1: lessons.md 的数据源严格限定为 fixer 自报字段，不解析 summary.md 自由文本

`npc lessons record` 只读 `<base>/events.jsonl` 里 `event == "fix.done"`（`fix-rN` 成功退出）事件的 `categories_scanned` / `regressions_added` / `notes`，不打开、不解析 `round-N.fix.summary.md` 原文。

**Why**：`round-N.fix.summary.md` 是 `spine-coder` 写给人和 `/spine-analyze` 复盘用的自由格式详细日志（`plugins/agent-spine/agents/spine-coder.md` 只要求"至少包含"几类内容，不是固定 schema）——对自由文本做"确定性提炼"要么需要正则硬编（脆弱，字段一改措辞就漏），要么需要 LLM 摘要（违反"npc 只做机械动作"）。而 `categories_scanned`/`regressions_added`/`notes` 三个字段已经是 fixer 在 RESULT 行里**自己填写的结构化自报**，是唯一同时满足"确定性可提取"与"fixer 自报、非 reviewer 文本"两个约束的数据源。

**Alternative rejected**：正则扫描 summary.md 抓"根因"段落——字段名/标题措辞不受契约约束，任何一次 prompt 措辞调整都会让抓取静默失效，且抓取到的仍是自由文本，会诱使后续把它当"结论"用而非"参考"。

### D2: lessons.md 结构 = 逐 change 追加的 markdown 段落，不做跨 change 去重/聚类

```markdown
## <change_id> (archived <archive_commit 短 hash>, <rounds> fix rounds)
- categories_scanned: validation, error-handling
- regressions_added: test_foo.py::test_bar
- notes:
  - r1: <fixer 原话，一行>
  - r2: <fixer 原话，一行>
```

无 fix 轮的 change（一次 review 即 blocking==0）不追加条目——没有"失败模式"可提炼，追加空段落只会增加噪音。

**Why**：markdown 人类可读、可被 coder 用 Read 工具直接消化，不需要专门的解析器；逐 change 独立段落使幂等判定简单（"change_id 的 `## ` 标题是否已存在"）。**Alternative rejected**：结构化 JSON——目标读者是下一个 change 的 coder（LLM 阅读），markdown 比 JSON 更省 token 也更自然；聚类/去重（比如把多个 change 的同类 categories 合并成一条"高频失败模式"）属于语义判断，留给 gate 的候选下游 change 自己在 write 轮里读全文判断，npc 不做。

### D3: 注入只给指针，不给内容；条件严格为"存在且非空"

`render_implementer` 新增的段落只是"必读输入"列表里的一条 bullet（指向 `lessons.md` 绝对路径 + 一句限定语），不是把文件内容拼进 prompt 正文。`npc implement run` 在渲染前检查 `run_dir/lessons.md` 是否存在且文件大小 > 0，不存在或为空则完全不渲染该段落（prompt 与现状逐字等价）。

**Why**：与既有 `spine-coder` "薄引导语 + 指向已落盘文件"的既定模式一致（`docs/cli.md` 强调"主 session 不读 prompt 模板/summary 原文"这条不变量 2 的精神同样适用于 coder 侧——npc 不该替 coder 预读并裁剪 lessons 内容，那本身是一种隐性判断）；条件渲染保证 run 内第一个 change 的 prompt 与"本 change 不存在"完全一致，不需要为空文件写特判逻辑。**Alternative rejected**：把 lessons 内容直接拼进 prompt——lessons.md 会随 run 增长，拼全文有失控的 token 增长风险，且丧失了"coder 按需自取"的既定习惯。

### D4: pilot-rewrite-gate 不是 auto_decide 的新 trigger，是独立的只读闸口 + 复用 spec write 三件套执行

`npc auto_decide.py` 现有模型是**per-change 失败恢复决策**：入口是单个 `progress[seq-1]` entry + 一个失败性质的 `trigger`，返回值域固定在 `{continue-retry, skip, force-archive, abort}`，语义是"这个卡住的 change 接下来怎么办"。pilot-rewrite-gate 是完全不同性质的决策点：它不是对失败的响应，而是**层屏障处的一次性机会窗口**，输入是"整层下游候选集 + lessons 增量"，输出是"要不要对某些下游 change 发起一次 write 轮修订"——candidate 是多个 change 而非一个 entry，action 也不是那四个值。把它硬塞进 `VALID_TRIGGERS` 会污染 `_decide()` 的纯函数契约（entry-based）。

因此设计为独立命令 `npc lessons gate`：只读部分（`gate_candidates`）纯函数式给出候选与增量事实；`--apply` 部分把决策落 state（`lessons.gate_decisions` 历史 + 游标推进）。auto 档的"默认动作"直接硬编码在 gate 命令里（固定 `skip-rewrite`），不需要经过 `auto-decide` 那一层，因为这里**没有失败要恢复**、也没有 continue-retry 的概念——lessons 已经通过 D3 的注入路径对下游生效了，rewrite 只是"要不要更进一步"，保守默认自然是"不"。

真正的修订动作（改下游 change 的 tasks.md/design.md 文字）是生成性工作，不该由 npc 代劳——复用已有的 `npc spec write run/record`（`docs/cli.md` §8d）家族：`spec write run` 本来就支持"已存在 change-id 的补全/修复分支"（省略 `--goal` 时不渲染目标段落），新增 `--lessons-path` 只是给这条既有路径多一个可选注入源，`spec write record` 的越界拦截（`out_of_scope_changes`/`unexpected_commit`）零改动直接复用——目标 change 自己的目录本来就是它的写权限边界。

**Why**：不重造决策模型、不重造 agent、不重造边界拦截——三处都直接复用已验证的既有机制，diff 面压到最小。**Alternative rejected**：新造 `spine-lessons-rewriter` agent——与 `spine-spec-writer` 职责完全重叠（都是"改 openspec change artifact"），纯粹的重复建设。

### D5: 候选集判定 = `dag_layer > 当前层 && status == pending`，游标防重复摆出

`gate_candidates` 的候选下游集合是确定性集合运算：`plan_order` 中 `progress[].dag_layer` 严格大于当前刚收敛的层号、且 `status == "pending"`（尚未 `npc implement run` 过，`implement_commit` 为空）。已经在 implementing/reviewing/fix-loop 或已到终态的 change 不进候选集——避免和进行中的执行产生竞态、避免撤销已经产生的实现。

`lessons.gate_processed_cursor`（记录已经在某次 gate 里"摆过"给用户/auto 看过的最后一个 lessons 条目序号）确保同一批 lessons 条目不会在下一层屏障重复触发候选判断（除非期间又有新 change 完成 archive、追加了新条目）——`has_candidates` 的判定是"候选集非空 **且** lessons.md 里存在游标之后的新条目"，两者缺一都直接短路返回 false，编排者零判断直接跳过闸口。

**Why**：层屏障在 DAG 场景下会触发多次（每层一次），如果每次都无条件把"改不改下游"摆出来，交互档会被反复打断问同样的问题；游标把"有没有新东西值得问"这个判断也做成确定性的，不依赖编排者记忆。**Alternative rejected**：只在"首层"触发一次——与用户原始目标的字面表述一致，但会错过第二层之后新产生的 lessons（比如层 2 的 change 又踩了新坑，层 3 还没开始，理应也能受益）；限定"首层"没有实质收益，只是徒增特判分支。

## Risks / Trade-offs

- [`events.jsonl` 里 `categories_scanned`/`notes` 字段可能是空字符串（fixer 没填或填了 `-`）] → 提炼时对空值做过滤，条目里对应子项省略而非留空占位；一个 change 全部字段皆空时仍追加一条只含 `rounds` 数的最简条目（保留"这个 change 返工过 N 轮"这条最基本信号）。
- [下游候选 change 已经被交互档用户手改过 tasks.md（比如临时调整），gate 触发的 write 轮可能覆盖] → `spec write run` 的既有职责就是"补全/修复"现有草稿而非从零重写，`spine-spec-writer` 按其现有职责边界（读现状 + 最小改动）执行；这是复用既有机制自带的行为，本 change 不额外收紧。
- [auto 档默认永远 skip-rewrite，lessons 的"回写"价值只体现在交互档] → 符合不变量 3（没人在回路时不加判断分支）；如果后续指标证明 auto 档也该有条件回写，那是需要专门 telemetry 支撑的下一步，不在本 change 范围内。
- [lessons.md 随 run 内 change 数增长，注入指针虽不含内容，但下游 coder 打开它仍要花 token 读全文] → 这与 review focus / 既有必读输入列表同量级（coder 本就要读 proposal/tasks/specs 全文），且只在文件非空时才发生；比起重复踩坑的返工成本，这笔 token 花费是设计上刻意接受的。

## Migration Plan

1. `src/npc/lessons.py` 纯增量：新文件、新 CLI 子命令，不改任何现有命令的默认行为。
2. `templates.py` / `pipeline.py` / `spec write run` 的改动均为**新增可选参数**，未传入或对应文件不存在时行为与现状逐字等价——旧 run（无 `lessons.md`）resume 不受影响。
3. `spine-run.md` Step 3c 增补 `npc lessons record`（非阻塞，紧邻既有 `spec-report render`，同一容错风格：失败不回滚 archive、不重跑）；层屏障后增补 `npc lessons gate` 分支（`has_candidates:false` 时零开销跳过，绝大多数单层或短 lessons 的 run 不会感知到这条新逻辑的存在）。
4. 回滚：删除 `spine-run.md` 里新增的两处调用即可完全回到现状；`lessons.py` 本身不侵入任何既有状态字段的语义。

## Open Questions

- `gate_candidates` 是否需要感知"下游 change 与已完成 change 的文件重叠"（即只把 lessons 摆给路径相关的下游，而非全体 pending change）——当前设计为简化实现，摆给**全部** pending 下游，交由人/`spine-spec-writer` 自行判断相关性；若后续语料显示交互档被无关噪音淹没，再引入 `npc plan dag` 已有的 touched-paths 数据做过滤。
- `notes` 字段单行自报文本理论上可能被 fixer 无意写入较长内容——是否需要截断上限（比如 200 字符）防止 lessons.md 无限增长，留待实现时按现有 `spec_report.MD_LINE_LIMIT` 同类惯例定（倾向需要，但非本 change 语义焦点）。
