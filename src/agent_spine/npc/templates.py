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


TEMPLATE_VERSION = "1.0.0"


def render_implementer(
    change_id: str,
    base: str,
    repo_root: str,
) -> str:
    """渲染 Implementer prompt（对应历史 skill 的 §A 段）。"""
    summary_path = f"{base}/implement.summary.md"
    return f"""你是 OpenSpec change 实施专家。请完整实施 change `{change_id}`。

## Runtime Variables（npc 已注入；prompt 内引用变量名）

- REPO_ROOT={repo_root}
- LOG_BASE={base}
- CHANGE_ID={change_id}
- SUMMARY_PATH={summary_path}

## 必读输入（按需自取）

- openspec/changes/{change_id}/proposal.md / tasks.md
- openspec/changes/{change_id}/specs/ 下所有 spec.md（如存在）
- openspec/changes/{change_id}/design.md（如存在）
- openspec/AGENTS.md / openspec/project.md
- 项目根 CLAUDE.md

## 实施约束

- spec.md 的 Requirements / Scenarios 是**唯一的实现与测试验收标准**；自行从 spec 提取细节
- 测试策略由你决定，但 spec 标注的"必须真实并发回归"、"必须覆盖此 Scenario"等硬约束不可降级为 mock
- 逐项完成 tasks.md，每完成一项更新对应行为 `[x]`
- 通过项目验收测试（参考 CLAUDE.md / project.md 的测试命令）
- 提交：`git commit -m "<type>(<scope>): <简要描述>"`，正文说明 change-id；**不要 archive**

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


def render_fixer(
    change_id: str,
    round_n: int,
    implement_commit: str,
    base: str,
    repo_root: str,
    blocking_findings_md: str,
    categories_seen: list[str],
    blocking_trend: list[int],
) -> str:
    """渲染 Fixer prompt（对应历史 skill 的 §B 段）。

    blocking_findings_md 由 fixer.render_findings 生成（直接嵌入正文）；
    categories_seen / blocking_trend 由主 session 通过 state 取，npc render 时
    自动注入到"修复历史"段。
    """
    summary_path = f"{base}/round-{round_n}.fix.summary.md"
    cats = ", ".join(categories_seen) if categories_seen else "（首轮，暂无）"
    trend = " → ".join(str(x) for x in blocking_trend) if blocking_trend else "（首轮，暂无）"

    return f"""你是代码修复专家。请修复 Codex review 指出的以下 blocking 问题。

## Runtime Variables

- REPO_ROOT={repo_root}
- LOG_BASE={base}
- CHANGE_ID={change_id}
- IMPLEMENT_COMMIT={implement_commit}
- FIX_ROUND={round_n}
- SUMMARY_PATH={summary_path}

## Review Findings（仅 in_scope=true 且 severity ∈ {{critical, high}}）

{blocking_findings_md}

## 修复历史

- categories_seen: {cats}
- blocking_trend: {trend}

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
    else:
        action_phrase = f"修复 change `{change_id}` 的 review findings"

    base = f"""请先用 Read 工具读取并严格按 {prompt_file} 里的指令执行任务（{action_phrase}）。

该文件包含完整的实施 / 修复指令、Runtime Variables、双产物契约（summary 文件 + RESULT 行）。请逐项遵循，最终 message 的最后一行必须是 RESULT 行。"""

    if extension:
        return f"""{base}

## 本次追加约束（优先级高于上述文件中的常规约束）

{extension}"""
    return base
