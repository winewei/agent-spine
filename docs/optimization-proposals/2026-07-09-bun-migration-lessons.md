# Bun Zig→Rust 迁移（bun.com/blog/bun-in-rust）经验 → agent-spine 对照提案 — 2026-07-09

**事件背景**：Bun 团队用 11 天（2026-05-03~14）、64 并发 Claude、4 个 worktree，把 1,448 个 `.zig` 文件（~78 万行）全量重写为 Rust。6,502 commits、峰值 695 commits/小时；5.9B uncached in-tokens + 690M out-tokens，API 价 ≈ $165k。全部 60,624 个测试、1,386,826 个 `expect()` 不删不跳全量通过后合入；上线后共修 19 个回归。

**方法论核心**（与本仓库逐条对照的依据）：

1. **文档先行**：写代码前先花 ~3h 与 Claude 讨论 Zig→Rust 模式映射，序列化成 `PORTING.md` + `LIFETIMES.tsv`，且该文档本身也过对抗式评审，之后所有 worker 共读。
2. **对抗式评审**：每个 implementer 配 **2+ 名 adversarial reviewer**；reviewer **只拿 diff**、被告知"假设正确性不可能成立"、唯一任务是找 bug。合并前抓出 libuv double-free、负时间戳 trunc/floor、`unwrap_or` 急切求值三个真 bug。
3. **循环结构**：task → coder 写码 → 2 reviewer 证伪 → **独立 fixer** 应用反馈。评审归属写进 commit subject。
4. **试点先行**：全量转换前先跑 3 个文件的 trial run，验证流程与 prompt 再放量。
5. **编译器即工单**：`cargo check` 的 ~16,000 个错误按 crate/file 分组分发；**慢操作只在 phase 边界跑一次**，循环中间不跑。
6. **git 纪律**：禁 stash/reset 等破坏性操作、按具体文件原子 commit（不 `add -A`）——这是他们第一个 false start 的直接教训。
7. **测试即地面真值**：TypeScript 测试套件与实现语言无关，**一个测试都不许删、不许 skip**。
8. **反 stub 规则**：failure mode 之一是 Claude 把"修编译错误"理解成写空 stub；reviewer 据此加规则——**需要多段注释自我辩护的代码直接拒收**。

---

## 对照：agent-spine 已覆盖的部分（不需要动）

| Bun 实践 | spine 对应物 |
|---|---|
| 生成/验证分离 | 不变量 1「生成 ⊥ 验证」，`npc verify routing` 代码层强制 |
| 破坏性 git 禁令 | `npc init --auto` 写入 deny 底线（`push --force` / `reset --hard` / `Edit(.git/**)`） |
| worktree 隔离 + 串行合入 | run worktree + per-change worktree + merge queue 串行段 |
| 确定性工单分发 | npc CLI 承担全部机械动作，DAG 分层调度 |
| 文档先行 + 文档本身过评审 | openspec artifacts + `/spine-spec` 强制独立语义评审 |
| 诚实失败回报 | spine-coder「tests 如实填，绝不谎报」+ record 返回值唯一真相 |
| 轮次上限 + 停滞闸门 | max_rounds + stale 闸门 |

## 缺口与提案（按预期收益排序；实施逐条走 openspec）

### 提案 1：round-0 增加 diff-only 对抗式评审通道（最高杠杆）

- **观察**：我们每轮只有 **1 个 reviewer**，且 focus 模板是**验收导向**（先读 proposal/specs/design，再查合规）。Bun 的经验是：合规评审与找 bug 是两种任务，找 bug 的 reviewer 要**只拿 diff + "假设它一定有错"的对抗框架**，2+ 名独立评审。他们合并前抓出的 3 个真 bug（double-free、trunc/floor、急切求值）全是这种上下文极简的证伪式评审抓的，不是合规检查抓的。
- **对照 07-08 分析**：r0 一次过率仅 8%，review-fix 占 ~80% token。若对抗通道能在 r0 把深层 bug 一次性暴露（而不是逐轮挤牙膏），长尾会缩短。
- **建议**：`npc review run --round 0` 增加第二个 pass——新 focus 模板：不读 spec 文件、只 `git diff`、指令为「假设这段 diff 必有 bug，你唯一的任务是找出它；重点：资源释放/double-free、边界与符号（trunc vs floor 类）、急切求值/短路语义、并发与生命周期」。两个 pass 的 findings 合并去重后进同一 review.json。round≥1 保持单通道（成本考量）。
- **验证方式**：telemetry 上 r0 blocking 数上升、r2+ 轮次 count 下降（长尾左移）；总 review+fix token 不升。

### 提案 2：反 stub / 反删测硬规则（成本≈0）

- **观察**：Bun 明确踩坑：worker 用空 stub「修」编译错误，靠 reviewer 加规则止血。我们的 spine-coder 契约只有「改动最小」，SELFCHECK_RUBRIC 与 review focus 均无 stub/删测判据。spine 场景下同构风险：coder 为让 `tests=pass` 注释掉失败断言、skip 测试、或写 `pass`/`todo!()` 占位把 task 勾掉。
- **建议**（三处同步，遵守不变量 1 的「类目同名、细则不共享」模式）：
  - spine-coder Guardrails 加一条：**禁止以 stub/占位实现充数勾 task；禁止删除、注释或 skip 任何既有测试来换 tests=pass**；
  - `SELFCHECK_CATEGORIES` 加 `no-stub` 类目；
  - review focus 模板加 blocking 判据：stub/占位实现、被弱化的测试 = blocking；**需要多段注释自我辩护的实现视为可疑**。
- **验证方式**：构造一个诱导 stub 的 fixture change，review 能把 stub 判成 blocking。

### 提案 3：run 级 lessons 前馈（PORTING.md 的跨 change 版）

- **观察**：Bun 让所有 worker 共读一份持续沉淀的模式文档。我们的 project_context 是静态的；同一 run 内 change-2 的 coder 完全不知道 change-1 的 fixer 刚踩过什么坑，同类 finding 跨 change 重复出现（07-08 分析的 category 盲区也指向这里）。
- **建议**：archive 成功后，从该 change 的 **fix summary（fixer 自报，非 reviewer 文本，不破不变量 1）** 提炼失败模式追加到 run 级 `lessons.md`；后续 change 的 implement prompt 注入该文件。天然形成「首 change 即试点」——Bun 的 3-file trial run 语义在多 change plan 中免费获得。
- **验证方式**：多 change run 中，后续 change 的 r0 blocking 数低于首 change；同 category finding 不跨 change 复发。

### 提案 4：原子 add 纪律（小，顺手）

- **观察**：Bun false start #1 是并行 worker 互踩 git 状态，解法之一是**按具体文件 commit，不整仓 add**。我们并行层 per-change worktree 已物理隔离，但单元素层直接在 run worktree 跑，coder `git add -A` 可能卷入无关文件（如并存的 telemetry/状态文件）。
- **建议**：spine-coder 契约加一句：**只 `git add` 自己明确改动的文件，禁止 `git add -A` / `git stash`**。
- **验证方式**：抽查 commit 的文件清单与 summary.md 的改动清单一致。

### 不建议引入的部分

- **64 并发 / 全量重写式放量**：Bun 是一次性迁移专项 + $165k 预算；spine 是常驻 SDD 循环，max_parallel=3 与本地资源匹配，无需对齐。
- **cgroups 隔离（systemd-run）**：Linux 专项压测手段，macOS 本地场景无对应物，收益不成立。

---

*本文件仅为建议，实施需用户逐条点头后走 openspec + 测试（见 CLAUDE.md「harness 改动走 spec」）。*
