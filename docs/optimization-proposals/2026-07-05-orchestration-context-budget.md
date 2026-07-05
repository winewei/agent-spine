# v1.5 迭代设计：主 session 上下文预算与编排边界下推

- 日期：2026-07-05
- 状态：**已实施（v1.5.0，本分支）**——P1/P2（`npc change run` / `npc integrate`）、P3（triage guardrail 入 v4 skill）、P4（`status --brief` / `state note`）、P5（`verify tasks`）、P6（re-plan 入 v4 skill Step 3e）、P7（telemetry `deviation` 记账）、P8（cron 化文档，docs/usage.md）、P9（new-plan-changes-v4 skill）、P10（`init-run --goal` + summary Goal Coverage）全部落地；§4 缓建清单维持缓建。契约见 docs/cli.md §8f，设计对齐见 design.md §11.10
- 关联：[principles.md](../principles.md)（不变量过滤）、[design.md](../design.md) §11.2/11.3/11.6、[2026-07-03-npc-handoff.md](2026-07-03-npc-handoff.md)（同一"一行收单"思想）
- 基线：`feat/npc-v1.4-plan-waves`（commit `0115530`）

---

## 0. 定位共识（owner 陈述，本文一切设计的前提）

**人驾驭 AI：人设目标 → Agent 辅助拆解成 OpenSpec changes → Agent 调度执行。**
OpenSpec changes 既是约束也是牵引，让 Agent 更确定地完成目标。

规模现实：一个目标拆出**几十个 change、每个含 10–20 个 task，合计一两百个 task**。在这个量级下，工具要解决三个管理维度：

1. **Sub-agent context 管理**——绝不污染主 session context；
2. **长任务跟踪与推进**——每一步可控、可续、可观测；
3. **高频确定性动作**——不让 Agent 自己生成命令再灌 prompt。

由此得出全文的**核心设计不变量**：

> 主 session 是一个工作记忆极小的调度器。**每推进一个 change，主 session 消耗的 context token 必须是 O(1)，且常数要被工程化压到最小。盘面状态以磁盘为唯一真相，context 只是缓存；异常向上冒泡，状态留在下面。**

---

## 1. 现状评估（v1.4 基线）

agent-spine 的"迭代"分三层，逐层结论：

| 层 | 组成 | 评估 |
|---|---|---|
| ① 规划层 | `plan.py` / `waves.py`（Kahn 分层 + 路径前缀着色）/ v3 dag-analyst + 双架构师裁定 | ✅ 机械候选+语义裁定分工正确；❌ **一次性**——plan_order 开局冻结，无重规划回路 |
| ② 微迭代 | review→fix 循环 + `trend.py` stale 闸门 + `auto-decide` | ✅ 有真终止保证（rsd≥3 + ≤20 轮）；⚠️ 循环体活在 skill 里（见 §2 账目） |
| ③ 自迭代 | `telemetry.py` 派生指标 → `/spine-analyze` → optimization-proposals | ✅ 派生量+指针的取舍正确；❌ 只有手动触发，design §11.6 第二阶段未落地 |

四条宪法不变量真实落到了代码层（`verify routing` / `verify tests` / `verify manifest` / 结构化契约），不是口号。

### 1.1 偏差修复机制的层级覆盖（讨论第二轮结论）

工程链路"目标→设计→拆解→实现"中，修复机制密度随层级升高急剧衰减：

| 偏差产生层 | 现有机制 | 覆盖 |
|---|---|---|
| 实现↔spec | review→fix + stale | ✅ |
| 自报↔实际 | verify tests / verify manifest | ✅ |
| 状态↔现实 | state repair / drift 检测 | ✅ |
| 拆解↔现实（DAG 漏边） | cherry-pick abort + 串行重实施 | ⚠️ 只有兜底 |
| 设计↔目标 / spec↔现实 / 目标↔需求 | 无 | ❌（人驾驭定位下由**人**承担，见 §1.2） |

关键原则：**偏差应在产生层修复，而非在发现层被反复补偿**。设计层的错在实现层表现为反复 blocking→stale→skip；skip 是止损不是纠偏。

### 1.2 宪法过滤后的能力判决（讨论第三轮结论）

用 principles.md 的三条尺子（职责归属 / 不变量 3"是因为去掉人吗" / 证据驱动"hotspots 打出来"）过滤所有候选能力：

| 能力 | 判决 | 理由 |
|---|---|---|
| steering 中途转向通道 | **该有** | 强化人在回路；npc 侧仅确定性落盘/读取 |
| meta-loop 定时化 | **该有** | design §11.6 自己规划的第二阶段，人闸不动 |
| re-plan（剩余集合重跑 waves） | **该有** | `waves.py` 是纯函数，重跑零新机器；何时重排是 skill 本职 |
| 偏差记账（归因落 telemetry） | **该有，且是前置** | 是宪法要求的"先收证据后建轨"的证据收集步骤 |
| 收尾报告"目标→change 覆盖对照表" | **该有** | 便宜，服务于人做验收 |
| 自动验收 agent / 归因升级阶梯 L2-L3 / 自适应阈值 / 预算控制器 / fresh review | **缓建** | 无人值守机器；等 telemetry 证实方差点或定位转向后再建 |
| `npc plan amend`（spec 修订命令） | **不建** | 普通编辑+人闸+重跑 spec analyze 已够，造命令是过度工程 |

---

## 2. 核心问题：循环体在 skill 里，常数太大

对现状（v3）每 change 的主 session 流量算账：

```
add-change + phase rotate + spawn(~150t) + verify manifest
+ Step 9 整合编排（cherry-pick / sed 换 hash / record / verify tests / revert，~40 行伪 bash）
+ review-fix 循环每轮 2 次 JSON 往返 × 平均 3–4 轮
+ archive
≈ 每 change 2–3k tokens
```

**50 个 change ≈ 100–150k tokens 纯记账流量**——不算思考与异常处理就注定 compaction。单次交互再干净（一行 JSON），**循环体放在主 session 里，交互次数本身就是污染源**。

而 review→fix 循环的控制流**已经是纯确定性的**：分支条件只有 `blocking` / `stale` / 轮数上限 / `auto-decide` 四个 action，没有一步需要主 session 的语义判断。它留在 skill 里是 v1 bash 时代的历史位置。

---

## 3. 设计方案

### 3.1 认知三层安置（总纲）

| 层 | 放什么 | 判据 |
|---|---|---|
| **主 session** | 波次边界推进、needs-decision 裁定、与人交互、异常升级 | 需要跨 change 全局视野 + 语义判断 |
| **sub-agent** | token 重的认知：DAG 抽取、架构师裁定、implement、**失败诊断（triage）** | 读大量原文、结论可压成一行 JSON |
| **npc** | 一切确定性的事，**包括循环与多步编排** | 控制流写得成 if/while |

### 3.2 P1 — `npc change run`：内环下沉（最高优先级）

把 implement → review → fix 循环 → archive 整个 per-change 内环吃进 npc 一条命令。

**决策点处理是可行性关键**（保证下沉不杀死人驾驭）：

- `--auto`：内部调 auto-decide 一路跑完，返回一行终态 JSON；
- 交互档：跑到决策点（stale / implement 失败 / archive 失败）**带 `status=needs-decision` + 局面摘要退出**；主 session 转成 AskUserQuestion，拿到人的选择后 `--decision <action>` 续跑。

哲学收益：**人驾驭的粒度从"盯每一轮机械转动"提升到"只在真正分叉点出场"**。

下沉后账目：每 change = spawn(~150t) + integrate 一行 + change run 一行 ≈ **400 tokens**；50 change ≈ 20k，**单 context window 全程无 compaction**。

**CLI 契约草案**（按 docs/cli.md 格式，实施时定稿）：

```
### `npc change run --seq N [--from implement|review|fix|archive] [--decision ACTION] [--max-rounds 20]`

一条命令跑完单 change 内环：implement → review round-0 → (fix → review)* → archive。
复用既有 pipeline 命令（implement run / review run / fix run / archive run）与 auto-decide，不重写。

- `--from`：断点重入（默认 implement；needs-decision 后续跑用）
- `--decision`：对上次 needs-decision 的人工裁定（continue-retry|skip|force-archive|abort），
  仅当 state 中该 seq 存在 pending_decision 时合法，否则 exit 2

stdout（终态，一行）:
  {"ok":true,"seq":N,"change":"cid","status":"archived","rounds":3,
   "archive_commit":"<hash>","blocking_trend":[5,2,0]}
stdout（决策点退出，一行）:
  {"ok":false,"seq":N,"status":"needs-decision","trigger":"review-stale",
   "round":4,"blocking_trend":[5,4,4,4],"suggested":"skip",
   "pointer":{"review_json":"...","summary_md":"..."}}

退出码: 0 终态成功(archived) / 1 终态失败(skipped|aborted) /
        5 needs-decision（交互档专用；--auto 下不会出现） / 2 用法 / 3 环境 / 4 依赖
状态装订: pending_decision 写入 state.json 对应 progress entry，续跑/compaction 后可恢复
```

### 3.3 P2 — `npc integrate`：波次整合编排下沉

v3 SKILL Step 9 的 cherry-pick + hash 翻译（sed）+ implement record + verify tests + fail 则 revert，是最大的"skill 内多行 bash"违规者，且 sed 换 hash 是 LLM 易错点。下沉：

```
### `npc integrate --seq N --result '<RESULT行>' --manifest PATH [--no-verify-tests]`

worktree 产物整合进 main：verify manifest → cherry-pick <manifest.commit> →
hash 翻译（RESULT 的 commit=Wc → 整合后 HEAD）→ implement record →
verify tests（fail 则 git revert + 状态回退）。全步骤单命令、失败自动收拾现场。

stdout: {"ok":true,"seq":N,"integrated_commit":"<H>","verify_tests":"pass|skipped"}
        {"ok":false,"step":"cherry-pick|verify-manifest|record|verify-tests",
         "reason":"...","reverted":"<H|null>","pointer":{...}}
退出码: 0 成功 / 1 任一步失败（现场已收拾干净，main 保持绿） / 2/3/4 同全局约定
```

### 3.4 P3 — 异常路径的 triage agent 模式 + 指针纪律

现状"结构化契约是唯一真相"只覆盖 happy path；失败时主 session 最易破戒去 cat 日志。skill guardrail 增加硬约定：

> 主 session **永不读日志/summary/review 原文**。任何 `ok:false` 需要细节时，spawn 只读 triage sub-agent（喂 error JSON 里的 `pointer.*`），收一行诊断 JSON 做决策。

npc 侧配套：所有 error emit 保证带 `pointer` 字段（多数已有，补齐缺口即可）。

### 3.5 P4 — compaction 重定向契约 + 编排意图落盘

- **`npc status --brief`**：单命令重入点——当前波次 / 各 change 相位 / pending_decision / 建议下一步动作，一行 JSON。skill guardrail 写死：**"任何 compaction 或续跑后，先 status --brief 重建盘面，绝不信任记忆里的进度"**（context 是缓存的行为化）。
- **`npc state note --text "..."`**（追加式编排日志）：承载两类内容——编排器自己的意图备忘（为什么这么排、悬而未决什么）与**人的中途转向指令（steering）**。`status --brief` 带出最近 N 条未消费 note；主循环在 change 边界消费。一条基建同时解决 §1.2 判决表的 steering 与"意图跨 compaction"。

### 3.6 P5 — task 维度：派生计数，绝不进主 context

change 是调度量子；一两百个 task 的状态活在各 change 的 tasks.md checkbox 里，由 coder 在自己 context 维护。补一个确定性核对：

```
### `npc verify tasks --change ID`
解析 tasks.md checkbox 完成度，与 implement RESULT 自报的 tasks= 字段交叉验证。
stdout: {"ok":true,"tasks_done":14,"tasks_total":17,"result_claim":"partial","consistent":true}
```

主 session 与人永远只看 `tasks_done/tasks_total` 两个数，不看清单。人的驾驭界面 = `npc status` + `npc watch` + `notify` 仪表盘，不是滚动的 session 记录。

### 3.7 P6 — re-plan 解禁：计划从名词变动词

新增 skill 层约定（npc 零新代码）：当出现 ①某 change 被 skip 且有下游依赖、②cherry-pick 冲突暴露 DAG 漏边、③人经 note 转向时，对**剩余未完成集合**重新走 §4.0 dag-analyst → `npc plan waves` → （交互档）人确认 → `state init-run` 追加式更新剩余 plan_order。留痕：run.events.jsonl 记 `{"type":"v3.replan","reason":...,"before":[...],"after":[...]}`。

### 3.8 P7 — 偏差记账（telemetry 扩展，为未来升级阶梯收证据）

- telemetry 增加 kind：`deviation`——每次 skip / stale / cherry-pick 冲突 / verify-tests revert / replan 落一条，字段含 `layer`（impl|decompose|design|unknown）、`trigger`、`repair_action`、`cost_rounds`。
- `v3.wave_done` 的波次质量信号（冲突率 / 架构师降级 / plan-only 重试）纳入 `telemetry agg` 新维度 `by-wave`。
- **明确不做**：自动归因裁定、升级阶梯 L2/L3 自动化——等这份记账跑出 hotspots 再按宪法建轨。

### 3.9 P8 — meta-loop 定时化（design §11.6 第二阶段落地）

CronCreate 定时跑 `/spine-analyze`（只读派生指标 <5KB，产出 ≤3 条提案落 optimization-proposals/）。**人闸不动：只提议不改码**。

### 3.10 P9 — skill v4 瘦身：skill 文本本身就是 context

SKILL.md 每次触发整篇进 context。v3 现 536 行，大量是维护者向内容（伪代码、陷阱清单、v2 对照）。P1/P2 落地后 v4 收敛为 **~200 行纯决策文档**：参数 / Step 0 前置 / DAG 抽取契约 / 波次裁定契约 / 波次循环（每步一条 npc 命令）/ needs-decision 映射 / 收尾。维护者向内容移入 docs/。

**开发纪律（石蕊测试）**：*skill 文档里任何超过三行的 bash 块，都是一个待下沉的 bug。*

### 3.11 P10 — 收尾报告增加"目标→change 覆盖对照表"

`summary render` 输出补一节：原始目标（人设的那句话）→ 各 change 一句话意图 → 终态。**人**据此做 run 级验收（人驾驭定位下验收是人的活；自动验收 agent 按 §1.2 缓建）。

---

## 4. 缓建清单（含启用触发条件）

| 项 | 触发条件 |
|---|---|
| 自动验收 agent | `--auto` 长链成为主用法，且出现"逐 change 全过、组合不达标"实例 |
| 归因升级阶梯 L2/L3 | P7 记账显示 stale→skip 是复发 hotspot |
| 自适应 stale 阈值 / 预算控制器 | 定位实际转向无人值守（宪法不变量 3 原文条件） |
| fresh review（抗 reviewer 锚定） | telemetry 证实历史注入导致后轮 review 漏检 |
| 并行 fix（v3.1，npc worktree 感知） | 波次内 fix 串行成为墙钟瓶颈的实测数据 |

---

## 5. 实施顺序

| 阶段 | 内容 | 验收 |
|---|---|---|
| S1 | P1 `change run` + P2 `integrate`（含 needs-decision 状态装订、测试） | 单 change 全内环一条命令跑通；交互档 exit 5 可续 |
| S2 | P4 `status --brief` + `state note`；P3 error pointer 补齐 | compaction 模拟后单命令重建盘面 |
| S3 | P9 skill v4 重写（基于 S1/S2 新命令）+ P3 triage guardrail + P6 re-plan 段落 + P10 | v4 ≤250 行；实跑 ≥5 change 无 compaction |
| S4 | P5 `verify tasks` + P7 偏差记账 + P8 cron 化 | telemetry 出现 deviation/by-wave 维度 |

验证指标（对齐 spine-analyze 格式）：主 session 每 change 平均 token（目标 ≤500）、50-change 模拟 run 是否触发 compaction（目标：否）、needs-decision 人工介入次数/次均耗时。
