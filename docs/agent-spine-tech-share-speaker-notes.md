# agent-spine 技术分享 · 演讲者文稿

> 配套 `agent-spine-tech-share.html`（15 页）。全程约 20–25 分钟 + Q&A。
> 每页标注了建议时长；【】内是舞台提示，不要念出来。

---

## 01 · 封面（30 秒）

大家好。今天分享一个我最近在做的东西——agent-spine。

一句话介绍：它是一个**跑在 Claude Code 进程内的本地自主 harness**。没有服务、没有容器、没有常驻进程——你在终端里敲一条 `/spine-run 目标 --auto`，它就从 spec 一路跑到结果交付。

【停一拍，让标题里的命令被看清】

这条命令背后发生了什么、为什么这么设计，就是接下来 20 分钟的内容。

---

## 02 · 痛点：人肉调度器（2 分钟）

先说为什么要做这个东西。

如果你用 AI 写过一批需求，你大概经历过这个循环：让模型写完，提 review，盯着 review 结果，有问题再喊模型修，修完再 review，最后归档。**每一步都要人盯着**。你以为你在用 AI 提效，其实你变成了一个人肉调度器。

更难受的是三件事：

第一，你的对话 context 里塞满了 prompt 模板、review 报告原文——本来该做决策的智能，退化成了数据搬运工。

第二，模型跟你说"写好了，测试全过"——**你敢信吗？** 我是不敢信的。

第三，全程跑在最贵的模型上。哪怕是复制粘贴级别的执行杂活，也在烧 premium token。

【指屏幕上的终端梗】`wc -l 你的耐心`——第三个 change 之后，输出是 0。这是我的真实体验。

---

## 03 · 是什么（2 分钟）

所以 agent-spine 是什么？我给它的定位是：**人驾驭的自主 harness**。

它把"长时运行 + 自主决策 + 反复 review 打磨"这套编排逻辑，做成了纯 markdown 的 skill；所有确定性的机械动作，全部委托给一个叫 `npc` 的 Python CLI。

三个组件：

- **/spine-run**：主 harness。init → plan → 对每个 change 做 implement → review → fix → archive → 收尾。支持断点续跑。
- **spine-coder**：专职执行体。被 spawn 出来的 subagent，产出代码 commit 和过程日志，只回一行 RESULT。
- **/spine-analyze**：自迭代入口。读跨 run 的指标，提最多 3 条 harness 改进建议——只读不改，人来审。

注意最下面这行定位：它是一个"从 spec 到结果交付"的 skill，**不是**无人值守的生产系统。人随时可以介入。这个定位会贯穿后面所有设计决策。

---

## 04 · 三层职责（2 分钟）

架构上就一张表：**谁思考、谁动手、谁搬砖**。

- **智能层**是主 session，也就是 /spine-run 本身。它只做两件事：调度和决策——排计划、spawn 执行体、读一行 JSON 做分支。
- **执行层**是 spine-coder subagent。写代码、留过程日志、一行 RESULT 回报。
- **底座**是 npc CLI，纯 Python。状态管理、事件记录、模板渲染、子进程、指标——所有确定性的机械动作。

两条铁律：

主 session **只调度与决策**。它不写业务代码，也不去解析自然语言日志。

凡是能确定性完成的事情，**一律下沉给 npc**。为什么？因为智能层的 context 是最贵的资源，一个字节都不该浪费在搬运模板上。

---

## 05 · 运行循环（2 分钟）

看一个 run 的完整生命周期。

【指流程链】npc init → plan 拆解 → 然后进入核心循环：implement → review → fix，**转到 blocking 归零为止** → archive → finalize。

几个细节：

- 你可以给一个自由目标，比如"给认证模块加限流"。它会先被自动拆解成若干个 openspec change，排出执行顺序。
- 每个 change：spawn coder 去实现，然后 `npc review run` 发起多轮**独立** review——注意是独立的，后面会展开——有 blocking 问题就再 spawn coder 去修。
- 遇到决策点，比如 review 卡死、archive 失败：交互档会弹出来问你，auto 档由 `npc auto-decide` 机械判定。
- 中途断了没关系，再跑一次 /spine-run，它检测到 `needs_resume`，从断点接着来。

---

## 06 · 四条不变量总览（1.5 分钟）

接下来是这次分享我最想讲的部分——4 条核心不变量。我把它们叫"宪法"：任何设计冲突，以这四条为准。

【逐个指卡片，只念标题，不展开】

1. **生成 ⊥ 验证**——写代码的角色，绝不评判自己的代码。
2. **只信结构化契约**——状态以落盘数据为准，不信 LLM 的散文。
3. **笼子 ∝ 1/(人在回路)**——人驾驭时硬轨最小化，按需再加。
4. **成本分层**——廉价模型只许执行，决策与验证恒 premium。

这四条不是我拍脑袋想的，是从生产级零信任架构（aidevos）里蒸馏出来、再适配"人驾驭 skill"这个定位的最小集。下面逐条展开。

---

## 07 · 不变量 1：生成 ⊥ 验证（2 分钟）

第一条，也是最重要的一条：**执行者永远不给自己盖章**。

- coder 只负责**生成**。合格不合格，由**独立的 review** 判定——用 codex 或 Claude 引擎。
- 关键约束：review 引擎**绝不可以和 coder 同源**。比如 coder 用 MiMo 跑，review 必须还是 codex 或 Claude。同一个模型自己查自己，等于没查。
- archive 的闸门只认一个东西：review 的 `blocking == 0`，或者人类显式 override。
- coder 自己说"我写好了""测试全过"？**不认。**

【指终端示例】看这个输出：`npc archive run` 返回 `ok: false, reason: review.blocking=2`——门不开。就这么简单，没有商量余地。

---

## 08 · 不变量 2：结构化契约是唯一真相（2 分钟）

第二条：**轨迹与结构化契约是唯一真相**。

- 主 session 做分支决策时，只读 npc 返回的**一行 JSON 里的关键字段**。
- 角色之间的交接全部走结构化契约：coder 回一行 RESULT，npc 回一行 JSON。
- 全部轨迹落盘到 `~/task_log/`，跨 run 的指标落到 `_telemetry/`。

反模式是什么？读 summary 原文做决策、把 prompt 模板搬进 context。npc 这个 CLI 存在的意义，就是消灭这类行为。

【指终端示例】`npc review run` 的返回就长这样：`blocking: 0, warnings: 3, round: 2`。主 session 看一眼 blocking 字段就知道走哪条分支——不需要读三千字的 review 报告。

顺便说个真实教训：我们之前用 `echo | jq` 解析这行 JSON，被 zsh 的转义搞崩过一次，后来换成 `printf` 修掉了。连"解析一行 JSON"这么小的事，在自主链路上都要硬化——因为它是决策依据。

---

## 09 · 不变量 3：笼子 ∝ 1/(人在回路)（2.5 分钟）

第三条是我认为最有意思的一条：**确定性约束的强度，和人在回路的程度成反比**。

什么意思？当一个系统是人驾驭的——你坐在终端前，每一步都看得到、能打断、能纠偏——人本身就是最好的安全机制，这时候硬性约束应该做到**最小**。规则加多了反而碍手碍脚。

所以 spine 不照搬那些重机器：policy 闸门、不可变 ledger、日历锁——那些是给"全程无人"的系统准备的。

我们加任何新硬轨之前，先问一个问题：**"这是因为去掉人了吗？"** 如果不是，就不加。

那什么时候加？由数据说话——`npc telemetry hotspots` 指出真实的方差点位，硬轨是被真实问题"打"出来的，不做预先的过度设计。

最后澄清一个容易误解的点：`--auto` 是"少打断人"的便利档，**不等于**无人产品。人退出多少回路，轨就补多少——信任预算是守恒的。

---

## 10 · 不变量 4：成本分层（2 分钟）

第四条：**廉价层只许执行**。

- 便宜模型——比如 MiMo——**只可以用在 coder 层**。决策、review、分析，**永远留给 premium**（Claude / codex）。
- 路由细节：premium coder 默认走 in-session subagent，这是为了对冲 headless `claude -p` 的计费风险；MiMo 恒走 headless。
- MiMo 默认不启用。要用就显式开：全局开、按 phase 开（比如只把 fix 阶段给 MiMo）、或者临时 `--backend mimo`。

这条不是靠自觉遵守的——`npc verify routing` 在代码层强制检查：review 里出现 mimo，就是 violation；mimo 配 in-session，也是 violation。

【指终端示例】正常输出长这样：coder 是 mimo，review 是 codex，violations 空。省钱可以，省在验证上不行。

---

## 11 · 两种运行档（1.5 分钟）

实际使用时有两个档位。

**交互档**是默认的：plan 排好先给你确认；review 卡死或 archive 失败时，弹窗让你拍板。适合高风险的改动，或者你第一次上手。

**--auto 档**是 fire-and-forget：绝不打断人。例行决策交给 `npc auto-decide` 机械判定，一路跑到底，只在真正卡死时才停下来。

auto 档有一条硬规则：**绝不调用 AskUserQuestion**。每一个分叉点，要么走确定性默认，要么走 auto-decide。

另外每个 run 都在独立的 git worktree 里跑，最后 ff-only 合回主干——对你的工程目录零侵入，跑失败了也不会弄脏你的工作区。

---

## 12 · 轨迹与自迭代（1.5 分钟）

所有轨迹都留在 `~/task_log/` 下面：

【指目录树】run.json 和 active.json 是状态机；events.jsonl 记每一步事件；每个 change 有每轮的 review JSON 和 coder 的 summary.md；`_telemetry/` 是跨 run 的指标。

有了这些数据，harness 可以**自己迭代自己**：`/spine-analyze` 读跨 run 指标，提最多 3 条改进建议。注意——**只读不改，人审闸门**。它可以建议，动手改必须过人。

复盘也不用靠回忆了：review 平均几轮、fix 了几次、卡点集中在哪个阶段，全部可查。

---

## 13 · 安装（1 分钟）

上手很快，三层配置，5 分钟搞定：

1. **npc CLI**，机器级：在 agent-spine 仓库根跑 `uv tool install`。
2. **plugin**，用户级：已经上了内部 hub，`qlj-skills plugin install agent-spine` 一条命令。
3. **CLAUDE.md 片段**，项目级：告诉主 session 什么时候该用 harness。

外部依赖就三个：git 必需；openspec 用于 archive 和目标拆解;codex 是默认 review 引擎，也可以切 Claude。

---

## 14 · Demo（2 分钟）

【如果能现场跑就现场跑；不能就走这页的录屏/静态输出】

看一次完整的运行。输入一句话：`/spine-run 给认证模块加请求限流和审计日志 --auto`。

- plan 阶段自动拆成两个 change：add-rate-limit 和 add-audit-log。
- 第一个 change：实现完，第一轮 review 发现 1 个 blocking，自动 spawn coder 修掉，第二轮 review blocking 归零，归档。**注意：这中间没有人参与。**
- 第二个 change 更顺，一轮就过。
- 最后：2 个归档，轨迹和指标都落了盘。

前提只有一个：你的工程是 git 仓库，带 `openspec/` 目录。如果已经有写好的 change，直接 `/spine-run change名` 就行。

---

## 15 · Roadmap + 收尾（1.5 分钟）

最后说说"设计了、但暂时不做"的东西——注意，不做的理由正是不变量 3：按需再加。

- **复跑测试硬轨**：npc 真实复跑测试、不裸信 coder 的 RESULT——只有真正去掉人的时候才必需。
- **风险分级人在回路**：按 change 的爆炸半径决定哪里问人。
- **fix 阶段成本升级**：早期轮次用 MiMo，连续卡住自动升级到 Claude。

今天的 takeaway 就三句话：

1. **执行者不给自己盖章**——生成与验证分离。
2. **只信落盘的结构化数据**——不信模型的散文。
3. **人退出多少回路，硬轨就补多少**——信任预算守恒。

装上试试：`qlj-skills plugin install agent-spine`，在你的工程里跑一个 change。

谢谢大家，Q&A 时间。

---

## 附录 · 预判 Q&A

**Q: 和 GitHub Actions / CI 里跑 agent 有什么区别？**
A: spine 是本地、进程内、人驾驭的。没有基础设施成本，人随时可介入；CI 方案适合团队级无人流水线，二者定位不同、不互斥。

**Q: review 用 LLM，LLM review 靠谱吗？**
A: 单轮不一定靠谱，所以是多轮独立 review + blocking 收敛判定 + 引擎与 coder 强制不同源。且 archive 只认 blocking==0 这个结构化信号，不认任何一方的自述。

**Q: 为什么不直接全用便宜模型省钱？**
A: 不变量 4：验证质量决定系统下限。执行错了 review 能拦住；review 错了没人拦。所以省钱只能省执行层。

**Q: --auto 跑飞了怎么办？**
A: worktree 隔离保证主干不脏；全轨迹落盘可复盘；ff-only 合并意味着任何冲突都会停下来。最坏情况是浪费一个 worktree。

**Q: 为什么编排层是 markdown skill 而不是代码？**
A: 需要判断力的部分（调度、分支决策）留给 LLM 读 markdown 契约执行；确定性部分全部下沉到 npc（Python）。代码写编排会把判断力也写死，纯 prompt 编排又不可靠——这是折中后的分界线。
