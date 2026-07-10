"""Sub-agent prompt 模板（v1.0.0 起内置）。

历史上这两段模板（§A Implementer / §B Fixer）住在 skill 文档里，由主 session
用 Write 工具拼模板 + 写到 prompt 文件 + 再把整个文件内容作为 Agent 工具的
prompt 字段传入；导致同一份 ~2500 tokens 的模板在主 session context 里出现
两次（Write input + Agent input）。

v1.0.0 把模板下沉到 CLI 包资源：

- `npc agent prompt render` 直接渲染到 disk，主 session 完全不接触模板内容
- `npc agent spawn-prompt` 生成给 Agent 工具 prompt 字段用的薄引导语
  （"请用 Read 读取 X 并执行"），主 session 仅承担 ~150 tokens

模板字段为运行时变量（CHANGE_ID / IMPLEMENT_COMMIT / FIX_ROUND 等）所替换，
其余结构性内容（双产物契约、RESULT schema、修复规则 A-D）属于"项目级硬契约"，
不暴露为可配置项；如需替换契约，应升 npc 版本而非热改 prompt。
"""

from __future__ import annotations


TEMPLATE_VERSION = "1.2.0"

# ============================================================
# 静态通用自检类目清单（单一事实源）
# implement/fix prompt 均引用此常量，类目层级与 review focus 同名但不含 per-change 文本。
# 硬边界：此清单 MUST NOT 包含当次 change 的 review focus 渲染文本、上轮 findings 原文、
# 或 reviewer 的评分 rubric 细则——守核心不变量 1「生成 ⊥ 验证」。
# ============================================================

SELFCHECK_CATEGORIES: tuple[str, ...] = (
    "validation",
    "partial-failure",
    "locking",
    "test-coverage",
    "edge-case",
    "telemetry",
    "concurrency",
    "no-stub",
)

# Markdown 段落形式，直接嵌入 implement/fix prompt
SELFCHECK_RUBRIC_MD: str = """## 提交前自检（静态通用类目，与当次 review 判据无关）

在 commit 之前，逐条自查以下类目，确认未遗漏：

| 类目 | 自查要点 |
|------|---------|
| validation | 所有外部入参 / 边界值是否已校验；非法输入是否快速失败并给出明确错误 |
| partial-failure | 批量操作 / 多步事务是否处理了部分成功场景；失败时是否留痕、可重试 |
| locking | 临界区是否加锁；锁是否会在异常路径下正确释放（死锁 / 泄漏） |
| test-coverage | 新增代码路径是否有对应测试；关键分支（error path / empty / boundary）是否覆盖 |
| edge-case | 空集合、零值、极大值、并发首次初始化等边界条件是否处理 |
| telemetry | 关键事件 / 错误路径是否有 emit；metrics 标签是否正确 |
| concurrency | 共享状态是否线程安全；是否存在竞态或无限重试风险 |
| no-stub | 新增 / 修改的实现是否存在占位返回值、空函数体、被简化到不覆盖核心逻辑的分支；是否有既有测试被删除、注释掉、skip 或断言被放宽 |

> 注意：本清单为**通用**提醒，与 reviewer 对本 change 的具体判据**无关**。
> coder 侧仅见类目层级；reviewer 侧使用当次 change 的具体评审标准——二者不共享。
"""

# ============================================================
# 原子 git add 纪律（单一事实源，implement/fix prompt 均引用）
# 语义须与 plugins/agent-spine/agents/spine-coder.md 的 Guardrails 保持一致
# （允许行文差异，但关键约束与失败路径必须同义）。
# 硬边界：只约束 coder 自身提交行为，不引入任何 npc 确定性 gate，
# 不解析 summary.md 自由文本作为通过/拒绝判据——守核心不变量 1/2。
# ============================================================

ATOMIC_ADD_DISCIPLINE_MD: str = """## 原子 git add 纪律（强约束，违反视为失败）

- **只暂存自己明确改动的文件**：`git add` 时逐一枚举本次任务实际改动的文件路径。
  **禁止** `git add -A`、`git add .`，也禁止任何会隐式匹配未审视文件的通配路径 add——
  本 worktree 内可能并存 npc 写入的 telemetry/state 等无关文件，通配 add 会把它们误卷入 commit。
- **禁止破坏性 git 操作**：MUST NOT 执行 `git stash`、`git reset --hard`，或任何会丢弃未提交改动的
  `git checkout` / `git restore` 工作区调用来"清理"未预期状态。
- **commit 前 MUST 核验 index**：`git diff --cached --name-only` 检查暂存内容；若存在无法归因给本次任务的
  staged 条目，MUST NOT 直接 commit。仅允许**非破坏性 unstage**（`git restore --staged <path>` 或
  `git reset -- <path>`，只改 index、不动工作区），unstage 后重新核验直到 index 只剩本次任务明确改动的文件。
- **同一文件混有无法归因 hunk 时禁止整文件 add**：若目标文件的未暂存改动里混入了无法归因给本次任务的 hunk，
  MUST NOT 整文件 `git add`；只能在能精确核验的前提下做 hunk 级暂存（`git add -p` 逐 hunk 确认，或 `git diff`
  核对后精确暂存），否则按下面的失败路径停止。
- **commit 文件清单 ↔ summary.md 一致（自报口径）**：本次 commit 实际改动的文件集合，必须与 summary.md
  "Files Modified" 段逐文件列出的清单一致（无遗漏、无多余）。此为自报纪律，供 reviewer / 人工事后核验，
  不构成 npc 确定性 gate。
- **无法归因就停止并按既有失败态 RESULT 上报**：若工作区 / index 存在无法归因给本次任务的改动或冲突文件，
  以致无法只暂存自己明确改动的文件、也无法通过非破坏性 unstage 达成干净边界，MUST NOT 继续提交、
  MUST NOT 静默忽略。停止提交流程，使用**本阶段既有的失败态 RESULT schema**（key 集合不变，不新增/删除/改写任何 key），
  `notes` 只写一行简短阻塞原因，完整文件路径清单与逐项状态写入 summary.md，交由 reviewer / 编排者 / 人工处理。
"""


# ============================================================
# 工作目录契约（单一事实源，全部 agent prompt 均注入）
# 背景：Agent 工具 spawn 的 subagent 不继承编排者 shell cwd（harness 会把
# cd 到非授信目录的 shell cwd 静默重置回主 checkout），而主 checkout 与
# run worktree 内容几乎一致，agent 在错误目录工作无任何可感知异常。
# 已实证的事故模式：coder 的实现 commit 落到 main 而非 run worktree
# （2026-07-10 两次复现）。防御必须由 prompt 内的绝对路径锚定 + agent
# 自检承担，不能依赖 spawn 时的任何 cwd 假设。
# ============================================================


def cwd_contract_md(repo_root: str) -> str:
    """渲染工作目录契约段（以具体 repo_root 插值，agent 无需解析变量名）。"""
    return f"""## 工作目录契约（最高优先级，违反视为失败）

- 你的 shell 初始 cwd **不可信**：它可能是另一个 checkout（如主仓库），那里有一模一样的文件。本任务唯一合法的工作树是 `{repo_root}`。
- **动手前自检**：先执行 `git -C "{repo_root}" rev-parse --show-toplevel`，输出必须等于 `{repo_root}`；不等（或命令失败）则立即停止，不改任何文件，按本阶段失败态 RESULT 汇报，`notes=cwd-mismatch`。
- **每个 Bash 调用**都显式锚定：以 `cd "{repo_root}" && …` 开头，或对 git 用 `git -C "{repo_root}" …`。shell cwd 不保证跨调用持久（harness 可能随时重置），绝不依赖上一条命令留下的 cwd。
- Read / Write / Edit 一律使用以 `{repo_root}` 开头的绝对路径；禁止相对路径，禁止其他 checkout 的绝对路径。
- 若任务含 `git commit`：commit 前最后断言一次 `git -C "{repo_root}" rev-parse --show-toplevel` 输出等于 `{repo_root}`。
"""


def render_implementer(
    change_id: str,
    base: str,
    repo_root: str,
    lessons_path: str | None = None,
) -> str:
    """渲染 Implementer prompt（对应历史 skill 的 §A 段）。

    ``lessons_path``：run 级 ``lessons.md`` 绝对路径。非 None 时在「必读输入」追加一条
    指针 bullet + 限定语（仅供参考、不改变 tasks/spec 验收标准）。为 None 时不渲染该
    条目，此时 prompt 与历史行为逐字等价——**只注入指针，不内联 lessons 内容**
    （coder 自行 Read）。守核心不变量 1：lessons.md 只含 fixer 自报字段，不含 reviewer 产出。
    """
    summary_path = f"{base}/implement.summary.md"
    lessons_bullet = (
        f"\n- {lessons_path}"
        "（同 run 前置 change 的失败模式参考；**仅供参考，不改变 tasks/spec 的验收标准**——"
        "验收依据仍以本 change 的 spec.md Requirements/Scenarios 为唯一准绳）"
        if lessons_path
        else ""
    )
    return f"""你是 OpenSpec change 实施专家。请完整实施 change `{change_id}`。

## Runtime Variables（npc 已注入；prompt 内引用变量名）

- REPO_ROOT={repo_root}
- LOG_BASE={base}
- CHANGE_ID={change_id}
- SUMMARY_PATH={summary_path}

{cwd_contract_md(repo_root)}
## 必读输入（按需自取）

- openspec/changes/{change_id}/proposal.md / tasks.md
- openspec/changes/{change_id}/specs/ 下所有 spec.md（如存在）
- openspec/changes/{change_id}/design.md（如存在）
- openspec/AGENTS.md / openspec/project.md
- 项目根 CLAUDE.md{lessons_bullet}

## 实施约束

- spec.md 的 Requirements / Scenarios 是**唯一的实现与测试验收标准**；自行从 spec 提取细节
- 测试策略由你决定，但 spec 标注的"必须真实并发回归"、"必须覆盖此 Scenario"等硬约束不可降级为 mock
- 逐项完成 tasks.md，每完成一项更新对应行为 `[x]`
- 通过项目验收测试（参考 CLAUDE.md / project.md 的测试命令）
- 提交：`git commit -m "<type>(<scope>): <简要描述>"`，正文说明 change-id；**不要 archive**

{ATOMIC_ADD_DISCIPLINE_MD}
{SELFCHECK_RUBRIC_MD}
## 双产物契约（缺一视为失败）

(1) 用 Write 工具创建摘要到 `{summary_path}`（≤ 80 行）：

```markdown
# Implement Summary — {change_id}

Commit: <hash>
Tasks Completed: <count> / <total>
Tests: pass | fail
Files Modified: <bulleted list>

## Key Decisions
<非平凡决策，每条 1 行>

## Issues Encountered
<如无写 "None">

## Verification
<运行的验证命令与结果>
```

(2) 最终 message **最后一行**严格输出：

```
RESULT: commit=<hash> tasks=<count> tests=<pass|fail> summary={summary_path} notes=<一行说明，无则填 ->
```

失败时也必须输出 RESULT 行：

```
RESULT: commit=- tasks=<已完成数> tests=fail summary=<path or -> notes=<关键错误一行>
```

两条产物缺一不可：summary 文件必须真的 Write，RESULT 行必须出现在 message 最后一行。
"""


def _render_escalation_section(
    escalated: set[str],
    streaks: dict[str, int],
    recurred: set[str],
    threshold: int,
) -> str:
    """渲染"连续复现升级（强制穷举落点清单）"段。

    escalated 为空集时返回 ""（fix prompt 与本 change 引入前逐字等价，既有"任意
    重复即全扫"规则文字不受影响）；非空时对每个升级 category 标注其连续 streak 值与
    是否存在复现信号（unsubstantiated），并把该 category 段落格式从"列出即可"升级为
    强制穷举三态清单（已覆盖 / 新增覆盖 / 确认不可达 + 理由）。

    语义边界（design D7）：复现是"上一轮自报未被后续轮次证实"的强信号，文案统一使用
    "复现 / 未被证实"，MUST NOT 使用"证伪 / 为假"。
    """
    if not escalated:
        return ""
    lines: list[str] = [
        "### 连续复现升级（强制穷举落点清单）",
        "",
        (
            f"以下 category 已达连续复现阈值（streak ≥ {threshold}），或存在后续轮次"
            "复现信号（unsubstantiated，即上一轮全落点扫描自报未被后续轮次证实——"
            "这是强复现信号，表示上一轮自报未被证实，需本轮重新穷举落点）："
        ),
    ]
    for c in sorted(escalated):
        n = int(streaks.get(c, 0))
        flags: list[str] = []
        if n >= threshold:
            flags.append(f"连续 {n} 轮")
        if c in recurred:
            flags.append("复现/未被证实")
        lines.append(f"- {c}（{'，'.join(flags)}）")
    lines.append("")
    lines.append(
        "对上列每个 category，本轮 fix.summary.md 的 \"Locations Scanned\" 段 MUST 从"
        "\"列出即可\"升级为**强制穷举清单**：逐条列出该 category 不变量在整个 change 范围内的"
        "**每一个**已知落点，并对每条精确标注三态之一——**已覆盖** / **新增覆盖** / "
        "**确认不可达（附理由）**。遗漏任一落点或未逐条标注三态，视为本轮修复未完成。"
    )
    return "\n\n" + "\n".join(lines)


def render_fixer(
    change_id: str,
    round_n: int,
    implement_commit: str,
    base: str,
    repo_root: str,
    blocking_findings_md: str,
    categories_seen: list[str],
    blocking_trend: list[int],
    eviction_md: str = "",
    category_streaks: dict[str, int] | None = None,
    recurred_categories: list[str] | None = None,
    category_streak_threshold: int = 2,
) -> str:
    """渲染 Fixer prompt（对应历史 skill 的 §B 段）。

    blocking_findings_md 由 fixer.render_findings 生成（直接嵌入正文）；
    categories_seen / blocking_trend 由主 session 通过 state 取，npc render 时
    自动注入到"修复历史"段。
    eviction_md：驱逐上下文 Markdown 段（非空时注入 prompt），包含冲突文件、
    diff 摘要及"解冲突 → git add → git rebase --continue"指令。

    category_streaks / recurred_categories / category_streak_threshold：
    change ``fix-prompt-exhaustive-sweep`` 的确定性升级层。二者均由 ``trend.py`` 的
    共享纯函数从 ``entry["phases"]`` 现场重算（design D1/D2），渲染时合并为
    ``escalated``（连续 streak ≥ 阈值 ∪ 存在复现信号）；``escalated`` 为空集时
    prompt 与本 change 引入之前逐字等价（既有"任意重复即全扫"规则文字不变）。
    """
    summary_path = f"{base}/round-{round_n}.fix.summary.md"
    cats = ", ".join(categories_seen) if categories_seen else "（首轮，暂无）"
    trend = " → ".join(str(x) for x in blocking_trend) if blocking_trend else "（首轮，暂无）"
    eviction_section = f"\n{eviction_md}\n" if eviction_md else ""

    streaks = category_streaks or {}
    recurred = list(recurred_categories or [])
    escalated = {
        c for c, n in streaks.items() if n >= category_streak_threshold
    } | set(recurred)
    escalation_section = _render_escalation_section(
        escalated, streaks, set(recurred), category_streak_threshold
    )

    return f"""你是代码修复专家。请修复 Codex review 指出的以下 blocking 问题。

## Runtime Variables

- REPO_ROOT={repo_root}
- LOG_BASE={base}
- CHANGE_ID={change_id}
- IMPLEMENT_COMMIT={implement_commit}
- FIX_ROUND={round_n}
- SUMMARY_PATH={summary_path}

{cwd_contract_md(repo_root)}
## Review Findings（仅 in_scope=true 且 severity ∈ {{critical, high}}）

{blocking_findings_md}
{eviction_section}
## 修复历史

- categories_seen: {cats}
- blocking_trend: {trend}{escalation_section}

## 上下文

- 本次修复针对 change `{change_id}`，已实施第 {round_n} 轮 fix
- 通过 `git log --oneline -5` 与 `git diff {implement_commit}~1..HEAD` 查看累计 diff

## 修复规则（强约束，违反任一视为失败）

**A. Root-cause 全落点扫描**

- 当某条 finding 的 `category` 此前任意轮次（含本轮）出现过，**强制**枚举并修复该 category 不变量在整个 change 范围内的**所有**落点，不是只改被点名那一行。
- 范围 = 本次 change diff 涉及的所有文件 + 调用同一 helper / 同一 API contract / 同一不变量的所有上下游。
- RESULT 行 `categories_scanned=<list>` 列出本轮枚举扫过的 category；fix.summary.md "Locations Scanned" 段列出所有已检查位置（文件:行号），即使没改也要列。

**B. 并发 / 事务 / 锁 / 重试 / 竞态 / 部分失败 类 finding 的真实回归**

- category ∈ {{concurrency, transaction, locking, retry, race-condition, partial-failure}}：mock-only 不够，必须写真实回归测试触发实际代码路径。
- RESULT 行 `regressions_added=<list>`；fix.summary.md "Real Regressions" 段说明测试如何触发被修复路径。
- 若无法写真实回归，在 "Real Regression Skipped" 段说明：(a) 不可行原因 (b) 替代验证 (c) 残余风险。

**C. 自检**

通读修复 diff，确认未引入 reviewer 反复关注的失败模式（无限重试 / 锁未释放 / 竞态扩大 / partial-failure 静默吞掉），结果写到 "Self-Check" 段。

**D. 常规约束**

- 只修 blocking + in_scope=true；不做额外重构
- 提交 `git commit -m "fix({change_id}): review round {round_n} — <摘要>"`，每轮 fix 独立 commit

{ATOMIC_ADD_DISCIPLINE_MD}
{SELFCHECK_RUBRIC_MD}
## 双产物契约

(1) 用 Write 工具创建修复摘要到 `{summary_path}`（≤ 120 行）：

```markdown
# Fix Round {round_n} Summary — {change_id}

Commit: <hash>
Findings Addressed: <list with status，按 id 排序>
Tests After Fix: pass | fail
Files Modified: <bulleted list>

## Per-Finding Resolution
- F1 (<title>): <如何修复的简述>
- F2 (<title>): <...>

## Locations Scanned (Root-Cause Sweep)
- category=validation:
  - path/a.py:42 (修)
  - path/b.go:81 (已正确，未改)
- category=concurrency: ...

## Real Regressions（仅并发 / 事务 / 锁 / 重试 / 竞态 / 部分失败 类）
- tests/test_x.py::test_real_concurrent_write → 启动 8 个真实 goroutine 并发写，断言无丢失

## Real Regression Skipped（若上一段为空）
<无可行真实回归时列原因 + 替代验证 + 残余风险；否则 "None">

## Self-Check
<修复 diff 自检：是否引入无限重试 / 锁未释放 / 竞态扩大 / partial-failure 静默吞掉>

## Side Effects
<为修复主 finding 而附带改动的代码或测试；每条 1 行。若无写 "None"。>
```

(2) 最终 message **最后一行**严格输出：

```
RESULT: commit=<hash> fixed=<count> tests=<pass|fail> summary={summary_path} categories_scanned=<comma-sep> regressions_added=<comma-sep|-> notes=<一行说明，无则填 ->
```

失败时：

```
RESULT: commit=- fixed=0 tests=fail summary=<path or -> categories_scanned=- regressions_added=- notes=<关键错误一行>
```

两条产物缺一不可。
"""


def render_spec_interrogator(
    change_id: str,
    base: str,
    repo_root: str,
    goal: str | None = None,
) -> str:
    """渲染 interrogate 轮 prompt（对应 change ``spec-writer-pattern-interrogation``）。

    盘问轮**先于**任何 write/fix 轮执行：要求 ``spine-spec-writer`` 在动笔撰写
    任何 artifact 之前，先枚举「仓库里已经有哪些与本次改动最相似的实现」，把关键
    假设摆出来，并列出开放问题——产物落到
    ``openspec/changes/<id>/pattern-interrogation.md``。

    硬边界（不变量 1，与 :func:`render_spec_writer`/:func:`render_spec_fixer` 同约束）：
    本函数 MUST NOT 引用 ``SPEC_REVIEW_SCHEMA`` 的 category 枚举、任何 spec-review 的
    rubric 细则，或任何 ``round-*.spec-review.json`` 的 findings 原文——盘问轮讨论的是
    仓库内已有的近似实现，不是本轮怎么被打分。

    ``goal``：来自 ``/spine-spec`` 的用户一句话原始目标，原文透传，不做任何改写/摘要。
    为 ``None``（或空串）时不渲染该段落（已存在 change-id 补全/修复分支）。
    """
    summary_path = f"{base}/pattern-interrogation.summary.md"
    interrogation_path = f"openspec/changes/{change_id}/pattern-interrogation.md"
    goal_section = (
        f"""
## 用户原始目标（原文保留，作为本次 change 的语义锚点）

{goal}
"""
        if goal
        else ""
    )
    return f"""你是 OpenSpec change 撰写专家。在为 change `{change_id}` 撰写任何 artifact 之前，请先完成一次**模式盘问**：枚举仓库里已有的最近似实现、摆出你的关键假设、列出开放问题。

## Runtime Variables（npc 已注入；prompt 内引用变量名）

- REPO_ROOT={repo_root}
- LOG_BASE={base}
- CHANGE_ID={change_id}
- SUMMARY_PATH={summary_path}
{goal_section}
{cwd_contract_md(repo_root)}
## 必读输入（按需自取）

- openspec/changes/{change_id}/ 下已存在的任何草稿（如有）
- 仓库源码：用 `Grep`/`Glob`/`Read` 主动搜索与本次改动最相似的既有实现（函数、模块、命令），**读到文件+函数级**再动笔
- openspec/AGENTS.md / openspec/project.md（写作规范与项目背景）
- 项目根 CLAUDE.md

## 你的产物：`pattern-interrogation.md`（结构化盘问，先于序列化）

用 `Write` 工具创建 `{interrogation_path}`，MUST 含以下三个 H2 段落（缺任一段，`npc spec interrogate record` 会以 `pattern_interrogation_missing_section` 拒绝装订）：

```markdown
## Analogs

- 逐条列出仓库里与本次改动最近似的既有实现，**每条都给文件路径 + 函数/符号名级引用**
  （例如 `src/npc/spec_pipeline.py::spec_write_run` 处理 routing→marker→render→return 的形态）。
  盘问的价值就在这里：动笔前先确认"仓库里已经怎么做同类事"，而不是等语义评审才发现漏读。

## Assumptions

- 逐条列出你为本次 change 所做的关键假设（数据流、既有 schema、调用点边界等），
  每条都可被人或后续评审单独挑战。

## Open Questions

- 逐条以顶层 `- ` bullet 列出需要用户拍板的开放问题；确无开放问题则保留本段标题、其下留空。
  （npc 会独立解析本段顶层 bullet 数决定是否要问用户，不采信你自报的任何数字。）
```

## 职责边界（MUST，确定性校验；不是口头约束）

- 只写 / 改 `openspec/changes/{change_id}/` 目录下的文件；MUST NOT 修改该目录之外的任何文件
- MUST NOT 运行 `git commit`（本 phase 的 RESULT 契约不含 `commit` 键，自报的 commit 无处安放）
- 本轮**只**产出 `pattern-interrogation.md`（盘问），MUST NOT 提前撰写 proposal/design/tasks/specs——那些留给后续 write 轮

## 双产物契约（缺一视为失败）

(1) 用 Write 工具创建摘要到 `{summary_path}`（≤ 80 行）：

```markdown
# Pattern Interrogation Summary — {change_id}

Artifacts: pattern-interrogation.md
Analogs Found: <条数与一句话概述>
Open Questions: <条数>
Summary: <一段话：本次 change 与哪些既有实现最像、最关键的假设是什么>
```

(2) 最终 message **最后一行**严格输出：

```
RESULT: change={change_id} artifacts={interrogation_path} summary={summary_path}
```

两条产物缺一不可：`pattern-interrogation.md` 与 summary 文件必须真的 Write，RESULT 行必须出现在 message 最后一行。
"""


def render_spec_writer(
    change_id: str,
    base: str,
    repo_root: str,
    goal: str | None = None,
    lessons_path: str | None = None,
) -> str:
    """渲染 spec writer 的 write 轮 prompt（对应 change ``spine-spec-writer``）。

    硬边界（不变量 1）：本函数 MUST NOT 引用 ``SPEC_REVIEW_SCHEMA`` 的
    category 枚举、任何 spec-review 的 rubric 细则，或任何 ``round-*.spec-review.json``
    的 findings 原文——write 轮生成侧不得预知本轮评判标准。

    ``goal``：来自 ``/spine-spec`` 命令行参数的用户一句话原始目标，原文透传，
    不做任何改写/摘要。为 ``None``（或空串）时表示调用方走的是"已存在
    change-id 补全/修复"分支，没有自由目标文本可传，此时不渲染该段落
    （不得伪造/编造目标文本）。

    ``lessons_path``：run 级 ``lessons.md`` 绝对路径（pilot-rewrite-gate 回写场景注入）。
    非 None 时渲染一个**独立于 ``goal`` 的**参考段落（同 run 前置 change 的失败模式，
    参考、非强制），与目标段落并列、互不覆盖；不改变"生成侧不得预知本轮评判标准"边界
    （lessons.md 只含 fixer 自报字段，不含任何 reviewer 产出）。为 None 时不渲染。
    """
    summary_path = f"{base}/spec-write.summary.md"
    goal_section = (
        f"""
## 用户原始目标（原文保留，作为本次 change 的语义锚点；不是评审标准）

{goal}
"""
        if goal
        else ""
    )
    lessons_section = (
        f"""
## 同 run 前置 change 失败模式（参考，非强制；不是评审标准）

同一 run 内已完成的前置 change 暴露的失败模式已确定性汇总在下述文件（每条均为
fixer 自报字段，不含任何 reviewer 产出）。**仅供参考**：你可自行判断哪些与本 change
相关并据此收紧 tasks/design，也可判断全部无关而不改。它不构成新的验收标准。

- {lessons_path}
"""
        if lessons_path
        else ""
    )
    return f"""你是 OpenSpec change 撰写专家。请为 change `{change_id}` 撰写 / 完善 artifact（proposal.md / design.md / tasks.md / specs/**/spec.md）。

## Runtime Variables（npc 已注入；prompt 内引用变量名）

- REPO_ROOT={repo_root}
- LOG_BASE={base}
- CHANGE_ID={change_id}
- SUMMARY_PATH={summary_path}
{goal_section}{lessons_section}
{cwd_contract_md(repo_root)}
## 必读输入（按需自取）

- openspec/changes/{change_id}/pattern-interrogation.md（**本轮之前已完成的模式盘问产物**，必读）
- openspec/changes/{change_id}/ 下已存在的任何草稿（如有）
- openspec/AGENTS.md / openspec/project.md（写作规范与项目背景）
- 项目根 CLAUDE.md

## 消费模式盘问产物 `pattern-interrogation.md`（MUST）

判据是一个**纯字符串存在性检查**：`pattern-interrogation.md` 是否含 `## User Decisions (Interactive)` 这一 H2 标题。**MUST NOT** 逐条揣测某个 Open Question 的语义状态——只看标题在不在：

- 若含 `## User Decisions (Interactive)` 标题：把 `## Open Questions` + `## User Decisions (Interactive)` 段原样写入 design.md 的 `## Pattern Mapping` 段。
- 若不含该标题：把 `## Open Questions` + `## Assumptions` 段原样写入 design.md 的 `## Pattern Mapping` 与 `## Assumptions` 段。

无论哪个分支，`## Open Questions` 段若为空（无 bullet）也按"该段为空"原样处理，不跳过整个指令。`## Analogs` 段的 analog 引用应指导你在 design.md 的 `## Pattern Mapping` 里说明本次实现沿用/偏离了哪些既有实现。

## 落点清单的确定性枚举（MUST）

若本 change 的 tasks.md 需要列出涉及 ≥2 处调用点/文件的落点清单，MUST 先执行确定性搜索命令（`grep`/`rg`/`git grep`）枚举涉及 ≥2 处调用点/文件的落点清单，并把命令原文与匹配计数写入 tasks.md 对应段落——使覆盖率判据从"reviewer 觉得全了"变成"清单能对着一条确定性命令逐项勾完"。

## 职责边界（MUST，确定性校验；不是口头约束）

- 只写 / 改 `openspec/changes/{change_id}/` 目录下的文件；MUST NOT 修改该目录之外的任何文件
- MUST NOT 运行 `git commit`（本 phase 的 RESULT 契约不含 `commit` 键，自报的 commit 无处安放）
- 完成后自行运行 `openspec validate {change_id} --type change --strict` 自检（结果记入 RESULT 的 `validate` 键；此为自报，不构成信任来源——真相由后续 `npc spec review run` 内部重新执行的确定性门给出）

## 双产物契约（缺一视为失败）

(1) 用 Write 工具创建摘要到 `{summary_path}`（≤ 80 行）：

```markdown
# Spec Write Summary — {change_id}

Artifacts: <bulleted list of files written>
Validate: pass | fail
Summary: <一段话概述本次 change 的目标与关键设计取舍>
```

(2) 最终 message **最后一行**严格输出：

```
RESULT: change={change_id} artifacts=<comma-sep files> validate=<pass|fail> summary={summary_path}
```

两条产物缺一不可：summary 文件必须真的 Write，RESULT 行必须出现在 message 最后一行。
"""


def render_spec_fixer(
    change_id: str,
    round_n: int,
    base: str,
    repo_root: str,
    blocking_findings_md: str,
) -> str:
    """渲染 spec fixer 的 fix 轮 prompt。

    ``blocking_findings_md`` 只含**上一轮已签发**（``round-(round_n-1)``）的
    blocking findings 原文——时点边界由调用方（``spec_pipeline.spec_fix_run``）
    保证，本函数不做取舍，只负责嵌入。
    """
    summary_path = f"{base}/round-{round_n}.spec-fix.summary.md"
    return f"""你是 OpenSpec change 撰写专家。请修复上一轮 spec 语义评审指出的以下 blocking 问题。

## Runtime Variables

- REPO_ROOT={repo_root}
- LOG_BASE={base}
- CHANGE_ID={change_id}
- FIX_ROUND={round_n}
- SUMMARY_PATH={summary_path}

{cwd_contract_md(repo_root)}
## 上一轮已签发的 Blocking Findings

{blocking_findings_md}

## 职责边界（MUST，确定性校验；不是口头约束）

- 只写 / 改 `openspec/changes/{change_id}/` 目录下的文件；MUST NOT 修改该目录之外的任何文件
- MUST NOT 运行 `git commit`
- 完成后自行运行 `openspec validate {change_id} --type change --strict` 自检（结果记入 RESULT 的 `validate` 键）

## 双产物契约（缺一视为失败）

(1) 用 Write 工具创建摘要到 `{summary_path}`（≤ 100 行）：

```markdown
# Spec Fix Round {round_n} Summary — {change_id}

Findings Addressed: <list with status，按 id 排序>
Validate: pass | fail
Artifacts: <bulleted list of files touched>
```

(2) 最终 message **最后一行**严格输出：

```
RESULT: change={change_id} fixed=<count> validate=<pass|fail> summary={summary_path}
```

两条产物缺一不可。
"""


def render_spawn_prompt(
    phase: str,
    change_id: str,
    prompt_file: str,
    extension: str | None = None,
) -> str:
    """渲染给 Claude `Agent` 工具 `prompt` 字段的薄引导语。

    主 session 拿到这条字符串（~150 tokens）作为 Agent.prompt 传入，
    sub-agent 启动后自己 Read prompt_file 拿到完整指令——这样原本 ~2500 tokens
    的 §A/§B 模板内容不再流过主 session context。

    phase 仅用于 description 文案；实际指令完全来自 prompt_file。
    """
    if phase == "implement":
        action_phrase = f"实施 OpenSpec change `{change_id}`"
    elif phase == "spec_interrogate":
        action_phrase = f"为 OpenSpec change `{change_id}` 撰写模式盘问 pattern-interrogation.md"
    elif phase == "spec_write":
        action_phrase = f"撰写 OpenSpec change `{change_id}` 的 artifact"
    elif phase == "spec_fix":
        action_phrase = f"修复 change `{change_id}` 的 spec 语义评审 findings"
    else:
        action_phrase = f"修复 change `{change_id}` 的 review findings"

    base = f"""请先用 Read 工具读取并严格按 {prompt_file} 里的指令执行任务（{action_phrase}）。

该文件包含完整的实施 / 修复指令、Runtime Variables、双产物契约（summary 文件 + RESULT 行）。请逐项遵循，最终 message 的最后一行必须是 RESULT 行。"""

    if extension:
        return f"""{base}

## 本次追加约束（优先级高于上述文件中的常规约束）

{extension}"""
    return base
