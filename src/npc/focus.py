"""Codex review focus 文本渲染。

- Round 0 与 Round N 用不同模板（N>=1 强调 carry-over 与 Fixer 自报证据校验）
- PROJECT_REVIEW_CONTEXT 抽取：从 openspec/project.md / CLAUDE.md 找特定章节，
  都没有则用默认中性约束
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable

from . import _io, paths as _paths


# ============================================================
# Already-Fixed History（fix.summary.md → focus 注入）
# ============================================================

_FINDING_RE = re.compile(r"^-\s*F(\d+)\s+\((.+)\)\s*:\s*(.+?)\s*$")


def _extract_per_finding_resolution(summary_text: str) -> list[tuple[str, str, str]]:
    """从 fix.summary.md 文本里抽取 ``## Per-Finding Resolution`` 段的 finding 列表。

    返回 ``[(id, title, resolution), ...]``。其它段落（Locations Scanned 等）一律跳过。
    """
    out: list[tuple[str, str, str]] = []
    in_section = False
    for line in summary_text.splitlines():
        m_head = re.match(r"^##\s+(.+?)\s*$", line)
        if m_head:
            in_section = m_head.group(1).strip().lower().startswith("per-finding resolution")
            continue
        if not in_section:
            continue
        m = _FINDING_RE.match(line.rstrip())
        if not m:
            continue
        out.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
    return out


def extract_fixed_history(base: Path, up_to_round_exclusive: int) -> list[dict]:
    """读 ``<base>/round-{1..up_to_round-1}.fix.summary.md`` 的 Per-Finding Resolution。

    ``up_to_round_exclusive`` 是当前正在跑的 review round（不读 fix-r{up_to_round_exclusive}
    自己的 summary，因为还没写）。返回每条 ``{round, id, title, resolution}``，按
    round 升序、同 round 内按 id 升序。
    """
    out: list[dict] = []
    for r in range(1, up_to_round_exclusive):
        path = base / f"round-{r}.fix.summary.md"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for fid, title, resolution in _extract_per_finding_resolution(text):
            out.append({"round": r, "id": fid, "title": title, "resolution": resolution})
    out.sort(key=lambda x: (int(x["round"]), int(x["id"])))
    return out


def render_fixed_history_section(items: list[dict]) -> str:
    """渲染为 focus.md 注入用的 markdown 段（含 ## 标题与提示语）。空时返回 ""。"""
    if not items:
        return ""
    lines = [
        "## Already-Fixed History (do not re-flag these unless re-broken)",
        "",
        "下列 finding 已在前几轮 fix 中处理；若 diff 显示它们仍未解决或新引入了等价问题，请明确指出；否则不要重复报告。",
        "",
    ]
    for it in items:
        lines.append(
            f"- [r{it['round']} F{it['id']}] {it['title']} → {it['resolution']}"
        )
    lines.append("")
    return "\n".join(lines)


def write_fixed_history_json(base: Path, items: list[dict]) -> Path:
    """把 fixed history 序列化到 ``<base>/fixed-history.json`` 供调试。返回路径。"""
    target = base / "fixed-history.json"
    base.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


REVIEW_HEADING_PATTERNS = [
    "评审重点",
    "威胁模型",
    "Review Context",
    "Threat Model",
]

DEFAULT_PROJECT_CONTEXT = """项目级评审约束（默认）：
- 本次评审目标是验证「实现是否对齐 spec」，请优先核对 proposal / tasks / specs / design 中的明确要求。
- tasks.md 中明确指定的实现方式（如"取 cells[0]"、"取最新 timestamp"）视为项目权威决策，不报告与之冲突的"应改为..."建议。
- 项目级文档（project.md / CLAUDE.md）中的约定（如错误处理 pattern、测试规范、命名）视为约束，违反这些才报；未在文档明确的"业界最佳实践"不作为 high/critical 阻塞，必要时归类为 medium/low advisory。
- 若不确定某个行为是 spec 故意决策还是实现疏漏，优先查阅 design.md 的 Decisions 段，再决定是否报告。
- 与本次 change diff 无直接关联的既有问题，请置 in_scope=false。"""


def _extract_section(text: str, patterns: Iterable[str]) -> str | None:
    """从 markdown 文本中抽取标题匹配 patterns 的章节内容。

    匹配规则：章节标题（# / ## / ### ...）的文本部分包含任一 pattern 即命中。
    抽取范围：从命中标题（不含）到下一个同级或更高级标题（不含）之间。
    """
    lines = text.splitlines()
    sections: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(#+)\s+(.*?)\s*$", line)
        if m:
            level = len(m.group(1))
            heading = m.group(2)
            if any(p.lower() in heading.lower() for p in patterns):
                # 收集到下一个 level <= 当前 level 的 heading
                start = i + 1
                j = start
                while j < len(lines):
                    m2 = re.match(r"^(#+)\s+", lines[j])
                    if m2 and len(m2.group(1)) <= level:
                        break
                    j += 1
                body = "\n".join(lines[start:j]).strip()
                if body:
                    sections.append(f"### {heading}\n\n{body}")
                i = j
                continue
        i += 1

    if not sections:
        return None
    return "\n\n".join(sections)


def load_project_context(
    repo_root: Path, override_path: Path | None = None
) -> tuple[str, str]:
    """返回 (context_text, source_label)。

    source_label 取值：override / openspec/project.md / CLAUDE.md / both / default
    """
    if override_path is not None:
        if not override_path.exists():
            raise FileNotFoundError(f"--project-context 文件不存在：{override_path}")
        return override_path.read_text(encoding="utf-8").strip(), "override"

    parts: list[str] = []
    sources: list[str] = []

    proj_file = repo_root / "openspec" / "project.md"
    if proj_file.exists():
        sec = _extract_section(proj_file.read_text(encoding="utf-8"), REVIEW_HEADING_PATTERNS)
        if sec:
            parts.append(sec)
            sources.append("openspec/project.md")

    claude_file = repo_root / "CLAUDE.md"
    if claude_file.exists():
        sec = _extract_section(claude_file.read_text(encoding="utf-8"), REVIEW_HEADING_PATTERNS)
        if sec:
            parts.append(sec)
            sources.append("CLAUDE.md")

    if parts:
        label = sources[0] if len(sources) == 1 else "both"
        return "\n\n".join(parts), label
    return DEFAULT_PROJECT_CONTEXT, "default"


SPEC_ATTRIBUTION_ENUM_SEMANTICS = """spec_attribution 四选一，用于判断该 finding 的根因是否可归因于 spec 文档本身：spec-silent = spec 未规定该行为；spec-ambiguous = spec 有规定但存在多种合理解读；spec-contradicted = 实现与 spec 明文相悖；impl-deviation = spec 明确无歧义，实现未照做。"""


# reviewer 侧「反 stub / 反删测」的 blocking 判据单一来源。
# 放进 _output_requirements_block() 供 Round 0 / Round N（及对抗式 pass）共享，
# 避免两份模板各自维护导致判据漂移或缺失（参照 SPEC_ATTRIBUTION_ENUM_SEMANTICS 先例）。
# 守不变量 1：本常量文案（尤其「多段注释自我辩护」这一启发式）属 reviewer 侧判据，
# MUST NOT 回流进 coder 侧的 SELFCHECK_RUBRIC_MD / implement / fix prompt。
STUB_AND_TEST_TAMPERING_BLOCKING = """stub / 占位实现（空函数体、恒定返回值、未覆盖核心逻辑的简化分支）MUST 视为 blocking；既有测试被删除、注释掉、skip 或断言被弱化（如断言范围被放宽、关键分支被跳过）以换取测试通过，同样 MUST 视为 blocking；需要多段注释自我辩护的实现视为可疑信号，需核实是否掩盖 stub 或测试弱化，而非直接认定合规。"""


def _output_requirements_block(authority_disclaimer: bool = True) -> str:
    """「输出要求」文案的参数化单一来源，含 spec_attribution 四值语义。

    单一来源，避免多份模板各自维护导致 reviewer 提示漂移/缺失
    spec_attribution 字段说明（见 fix round 1 finding F1）。

    ``authority_disclaimer``：
    - ``True``（默认，pass1 / round-N compliance 变体）：保留「与 tasks.md /
      design.md 决策一致的实现不作为 finding 报告」免责条款，现文案不变。
    - ``False``（pass2 对抗式变体）：MUST NOT 含该免责条款及任何
      tasks.md / design.md 字样——对抗式 pass 刻意不引用项目权威决策，
      避免"这是故意的"压制真实 bug（见 change review-r0-adversarial-pass D1）。
    """
    disclaimer_line = (
        "\n- 与 tasks.md / design.md 决策一致的实现不作为 finding 报告。"
        if authority_disclaimer
        else ""
    )
    return f"""**输出要求（极重要）**：
- 你的最终消息必须是**且仅是**一个合法的 JSON 对象，符合本次调用提供的 output-schema。
- 字段含义：
  - verdict: "approve" = 无任何 in_scope blocking 且无 advisory；"passed-with-advisory" = 无 in_scope blocking 但有 advisory；"changes-requested" = 至少 1 个 in_scope blocking。
  - 每条 finding 必须包含 id / severity / category / title / file / line_range / detail / recommendation / in_scope / spec_attribution。
  - in_scope=true 表示与本 change diff 直接相关；diff 之外的既有问题或越界建议必须 in_scope=false，不计入 blocking。
  - {SPEC_ATTRIBUTION_ENUM_SEMANTICS}{disclaimer_line}
- 反 stub / 反删测判据：{STUB_AND_TEST_TAMPERING_BLOCKING}
- 不要返回 markdown 包裹、不要返回散文、不要返回额外字段。"""


def _adversarial_round_0_template(change_id: str) -> str:
    """round-0 对抗式（pass2）focus 模板：不注入 project context、不指示读
    proposal/tasks/specs/design/project.md/CLAUDE.md，只看 diff、假设必有 bug。

    见 change review-r0-adversarial-pass D1/D5：唯一输入是
    ``git --no-pager diff HEAD~1..HEAD``，唯一任务是证伪式找 bug。
    """
    return f"""本次审查的是 OpenSpec change `{change_id}` 引入的代码 diff。这是一次纯对抗式（找 bug）评审。

请在仓库内运行：
    git --no-pager diff HEAD~1..HEAD
这是你唯一需要查看的输入——只看这段 diff，不要去读任何需求、规格、设计或项目约定类文档。

**对抗式框架（极重要）**：请假设这段 diff **一定隐藏着至少一个 bug**，你唯一的任务就是把它找出来。不要试图确认它是对的；默认它是错的，然后去证明错在哪里。

审查重点（四个方向，逐一排查）：
1. 资源释放 / double-free：资源是否被重复释放、释放后仍被使用、异常路径漏释放（泄漏）、锁是否在异常路径未释放。
2. 边界与符号处理：off-by-one、空集合 / 零值 / 负值 / 极大值、负数或符号位在截断 / 取整 / 移位时的处理。
3. 急切求值 / 短路语义：本应惰性求值的表达式被急切求值（默认值在取用前就已计算并产生副作用 / 异常）、`||` `or` `unwrap_or` 一类短路语义被误用。
4. 并发与生命周期：竞态、共享可变状态、初始化顺序、对象在被引用期间被销毁 / 移动、迭代过程中修改容器。

填写约定（本次不读规格文档，据此诚实填写）：
- 无法判断某 finding 与规格的关系时，`spec_attribution` 一律填 `spec-silent`。
- 审查范围就是这段 diff 本身，findings 默认 `in_scope=true`；仅当讨论的是 diff 之外未修改的既有代码时才填 `false`。

{_output_requirements_block(authority_disclaimer=False)}
"""


def _round_0_template(change_id: str, project_context: str) -> str:
    return f"""本次审查的是 OpenSpec change `{change_id}` 的代码 diff。
请先在仓库内运行：
    git --no-pager diff HEAD~1..HEAD
查看本次 change 引入的全部 diff，再开始审查。

请你在评审前先读取以下文件了解需求、规格与设计约束：
- openspec/changes/{change_id}/proposal.md（变更动机与范围）
- openspec/changes/{change_id}/tasks.md（任务清单与验收要点）
- openspec/changes/{change_id}/specs/ 目录下所有 spec.md（目标规格；若目录不存在则跳过）
- openspec/changes/{change_id}/design.md（设计方案；若文件不存在则跳过）
- openspec/project.md（项目级技术约定）
- 项目根 CLAUDE.md（验收规范与提交约束）

{project_context}

审查重点（按重要性排序）：
1. 实现是否完整覆盖 proposal 中所列 requirements，以及 tasks.md 中所列 task
2. 是否符合 design 中明确的接口契约、不变量、错误处理与边界条件
3. 是否引入与目标 spec.md 冲突的行为（命名、字段语义、状态机、错误码等）
4. 测试是否覆盖 spec 列出的验收场景与显式标注的边界情况；对并发 / 事务 / 锁 / 重试 / 竞态 / 部分失败场景，mock-only 测试视为"未充分覆盖"
5. 与 project.md / CLAUDE.md 中规定的项目级约束的一致性

{_output_requirements_block()}
"""


def _round_n_template(
    change_id: str,
    round_n: int,
    implement_commit: str,
    project_context: str,
    fixed_history_md: str = "",
) -> str:
    history_block = f"\n{fixed_history_md}" if fixed_history_md else ""
    return f"""本次审查的是 OpenSpec change `{change_id}` 的代码 diff（base = {implement_commit}~1）。
这是第 {round_n} 轮 re-review，前 {round_n} 轮 review-fix 历史与已修复 findings 见 $LOG_BASE/change.md。
请先在仓库内运行：
    git --no-pager diff {implement_commit}~1..HEAD
查看本次 change 累计 diff，再开始评审。

请你在评审前先读取以下文件了解需求、规格与设计约束：
- openspec/changes/{change_id}/proposal.md
- openspec/changes/{change_id}/tasks.md
- openspec/changes/{change_id}/specs/ 下 spec.md（如存在）
- openspec/changes/{change_id}/design.md（如存在）
- openspec/project.md 与项目根 CLAUDE.md
- $LOG_BASE/change.md 的 Round 0 ~ Round {round_n - 1} 段落（已识别 findings、已落地修复、Issue Category Tracker）
- 上轮 fix.summary.md：$LOG_BASE/round-{round_n}.fix.summary.md 中的 "Locations Scanned" 和 "Real Regressions" 段——这是 Fixer 自报的修复证据，请验证是否属实

{project_context}
{history_block}

审查重点：
1. 上轮 findings 是否被 Fixer 真正修复（含同类问题是否扫描完毕，避免「打地鼠」）；对照 fix.summary.md 的 Locations Scanned 段，验证 Fixer 是否真的去看了那些位置
2. 修复是否引入新的与 spec 冲突的行为或回归
3. 对并发 / 事务类 finding：Fixer 提供的真实回归是否真的触发了被修复路径；如果只有 mock-only 测试，请在 finding 里明确指出"需补真实回归"
4. 是否仍存在 spec 列明但实现遗漏的 requirement / 边界场景
5. 与 project.md / CLAUDE.md 约束的一致性

请直接报告本轮的新 findings 或仍未修复的 carry-over findings；不要重复列已修复的项。**对前几轮已被标注为"spec-aligned 不修"的 finding（见 $LOG_BASE/change.md 的 Carried Over / Advisory 段），不再重报**。

{_output_requirements_block()}
"""


def render(args: argparse.Namespace) -> None:
    """focus render --round N --change-id ID --output PATH [--implement-commit HASH] [--project-context PATH]。

    v1.1：round >= 1 时自动从 state 取 base，读 ``round-{1..N-1}.fix.summary.md``
    抽出 Per-Finding Resolution 注入 focus.md，避免 Codex 跨轮重报已修问题。
    同时序列化 ``<base>/fixed-history.json`` 供调试。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    override = Path(args.project_context) if args.project_context else None
    try:
        ctx, source = load_project_context(p.repo_root, override)
    except FileNotFoundError as e:
        _io.emit_error("project_context_missing", str(e), exit_code=3)
        return

    round_n = args.round_n
    fixed_count = 0
    fixed_json_path: str | None = None
    if round_n == 0:
        text = _round_0_template(args.change_id, ctx)
    else:
        if not args.implement_commit:
            _io.emit_error(
                "missing_implement_commit",
                "round >= 1 时必须提供 --implement-commit",
                exit_code=2,
            )
            return
        # 从 state 取 base 以便读 fix.summary.md
        from .state import read_state as _read_state

        history_md = ""
        try:
            state = _read_state(p.state_json)
            progress = state.get("progress") or []
            entry = next((e for e in progress if e.get("change_id") == args.change_id), None)
            if entry is not None:
                base = Path(entry.get("base") or _paths.base_for(p, entry["seq"], args.change_id))
                items = extract_fixed_history(base, round_n)
                fixed_count = len(items)
                if items:
                    history_md = render_fixed_history_section(items)
                    fixed_json_path = str(write_fixed_history_json(base, items))
        except (FileNotFoundError, OSError):
            # state 缺失或读取失败：focus 仍可渲染，只是不注入历史
            pass

        text = _round_n_template(
            args.change_id, round_n, args.implement_commit, ctx, fixed_history_md=history_md
        )

    output.write_text(text, encoding="utf-8")
    _io.emit(
        {
            "ok": True,
            "output": str(output),
            "bytes": len(text.encode("utf-8")),
            "project_context_source": source,
            "fixed_history_items": fixed_count,
            "fixed_history_json": fixed_json_path,
        }
    )
