"""npc spec analyze —— 实现前的 "spec↔tasks 漂移/覆盖" 确定性闸门。

对标 GitHub spec-kit 的 ``/analyze``：在写一行实现代码之前，纯读文件 + 解析
markdown，确定性地检查一个 openspec change 的内部一致性：

- proposal 声明的 capability 有没有对应 spec.md（``capability-no-spec``）；
- specs/ 下有 spec.md 但 proposal 没声明（``orphan-spec``）；
- spec.md 里写了 Requirement 但 tasks.md 完全没提该 capability（启发式
  ``requirement-maybe-uncovered``，可能误报）；
- tasks.md 缺失或没有任务项（``no-tasks``）；
- 任务全是 ``[x]`` 已完成、却在 implement 前调用（信息性 ``tasks-all-done``）。

不依赖 openspec CLI，不需要 active run，只需 change 目录存在。

handler：:func:`run`；纯函数：:func:`analyze_change`（便于单测）。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from . import _io


# ============================================================
# markdown 解析（纯函数）
# ============================================================

# proposal.md 里 New Capabilities 条目：`- \`name\`: 描述` 或 `- \`name\` 描述`
_CAP_ITEM_RE = re.compile(r"^\s*[-*]\s+`([^`]+)`")
# 段标题（## / ### / #### 任意层级）
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# spec.md 的 Requirement 段：`### Requirement: 名`
_REQUIREMENT_RE = re.compile(r"^#{1,6}\s+Requirement:\s*(.+?)\s*$", re.IGNORECASE)
# tasks.md 的任务项：`- [ ] ...` / `- [x] ...`（x 大小写均可）
_TASK_ITEM_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s*(.*)$")


def parse_new_capabilities(proposal_text: str) -> list[str]:
    r"""从 proposal.md 文本提取 New Capabilities 段声明的 capability 名列表。

    定位 ``## Capabilities`` → ``### New Capabilities`` 子段，收集其下
    ``- \`name\`: ...`` 形态的条目，直到遇到下一个同级或更高级标题。
    顺序保留、去重。
    """
    lines = proposal_text.splitlines()
    in_new_caps = False
    new_caps_level = 0
    caps: list[str] = []
    seen: set[str] = set()

    for line in lines:
        m_head = _HEADING_RE.match(line)
        if m_head:
            level = len(m_head.group(1))
            title = m_head.group(2).strip().lower()
            if not in_new_caps:
                # 进入 New Capabilities 段（任意标题层级，标题文本须精确匹配，
                # 避免 `### New Capabilities Extended` 之类误激活收集）
                if title == "new capabilities":
                    in_new_caps = True
                    new_caps_level = level
                continue
            # 已在段内：遇到同级或更高级标题则退出该段
            if level <= new_caps_level:
                in_new_caps = False
            continue
        if in_new_caps:
            m_item = _CAP_ITEM_RE.match(line)
            if m_item:
                name = m_item.group(1).strip()
                if name and name not in seen:
                    seen.add(name)
                    caps.append(name)
    return caps


def parse_requirements(spec_text: str) -> list[str]:
    """从单个 spec.md 文本提取 ``### Requirement: <名>`` 列表（保留顺序）。"""
    out: list[str] = []
    for line in spec_text.splitlines():
        m = _REQUIREMENT_RE.match(line)
        if m:
            name = m.group(1).strip()
            if name:
                out.append(name)
    return out


def parse_tasks(tasks_text: str) -> list[dict]:
    """从 tasks.md 文本提取任务项列表。

    每项 ``{"done": bool, "text": str}``。仅认行内复选框 ``- [ ]`` / ``- [x]``。
    """
    out: list[dict] = []
    for line in tasks_text.splitlines():
        m = _TASK_ITEM_RE.match(line)
        if m:
            done = m.group(1).lower() == "x"
            out.append({"done": done, "text": m.group(2).strip()})
    return out


# ============================================================
# 覆盖启发式（纯函数）
# ============================================================


def _normalize_for_match(s: str) -> str:
    """归一化用于粗粒度关键词覆盖匹配：小写，非字母数字折成空格。"""
    return re.sub(r"[^a-z0-9]+", " ", s.lower())


def capability_mentioned_in_tasks(capability: str, tasks_text: str) -> bool:
    """粗粒度启发式：capability 名（或其分词）是否在 tasks.md 全文出现。

    把 capability 拆成 token（按非字母数字分割），只要任一非平凡 token
    （长度 ≥ 3）以词边界出现在归一化后的 tasks 文本里就算 "提到"。这是粗粒度
    关键词覆盖启发式，可能误报或漏报，调用方会标注 ``可能误报``。
    """
    haystack = _normalize_for_match(tasks_text)
    tokens = [t for t in _normalize_for_match(capability).split() if len(t) >= 3]
    if not tokens:
        # capability 名太短/无字母数字：退化为整体词边界匹配
        # （仍用空格包裹，避免 "no" 命中 "not" 这类子串误匹配）
        norm = _normalize_for_match(capability).strip()
        return bool(norm) and f" {norm} " in f" {haystack} "
    return any(f" {t} " in f" {haystack} " for t in tokens)


# ============================================================
# 核心分析（纯函数）
# ============================================================


def _read_text(path: Path) -> str | None:
    """读取文本；不存在或读失败返回 None。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def analyze_change(change_dir: Path) -> dict:
    """对单个 openspec change 目录做确定性分析，返回结构化结果。

    返回 ``{change, requirements_count, tasks_count, capabilities, findings}``。
    每个 finding ``{kind, severity, detail}``。

    调用方负责把 change 目录不存在转成 emit_error(exit 3)；本函数假定目录存在。
    """
    change = change_dir.name
    findings: list[dict] = []

    # --- proposal: New Capabilities ---
    proposal_text = _read_text(change_dir / "proposal.md") or ""
    declared_caps = parse_new_capabilities(proposal_text)

    # --- specs/<cap>/spec.md ---
    specs_dir = change_dir / "specs"
    spec_caps: dict[str, str] = {}  # cap -> spec.md 文本
    if specs_dir.is_dir():
        for child in sorted(specs_dir.iterdir()):
            if not child.is_dir():
                continue
            spec_md = child / "spec.md"
            if spec_md.is_file():
                spec_caps[child.name] = _read_text(spec_md) or ""

    # --- tasks ---
    tasks_text = _read_text(change_dir / "tasks.md")
    tasks = parse_tasks(tasks_text) if tasks_text is not None else []
    tasks_count = len(tasks)

    # requirements_count：所有 spec.md 的 Requirement 总数
    requirements_count = sum(len(parse_requirements(txt)) for txt in spec_caps.values())

    # capabilities：声明 + spec 实际存在的并集（保留 declared 顺序，再补 orphan）
    capabilities: list[str] = list(declared_caps)
    for cap in spec_caps:
        if cap not in capabilities:
            capabilities.append(cap)

    # --- finding 1: no-tasks ---
    if tasks_text is None or tasks_count == 0:
        findings.append(
            {
                "kind": "no-tasks",
                "severity": "high",
                "detail": (
                    "tasks.md 缺失"
                    if tasks_text is None
                    else "tasks.md 存在但没有任何任务项（- [ ] / - [x]）"
                ),
            }
        )

    # --- finding 2: capability-no-spec ---
    for cap in declared_caps:
        if cap not in spec_caps:
            findings.append(
                {
                    "kind": "capability-no-spec",
                    "severity": "high",
                    "detail": (
                        f"proposal 声明 capability `{cap}` 但 specs/{cap}/spec.md 不存在"
                    ),
                }
            )

    # --- finding 3: orphan-spec ---
    declared_set = set(declared_caps)
    for cap in spec_caps:
        if cap not in declared_set:
            findings.append(
                {
                    "kind": "orphan-spec",
                    "severity": "high",
                    "detail": (
                        f"specs/{cap}/spec.md 存在但 proposal 的 New Capabilities 未声明 `{cap}`"
                    ),
                }
            )

    # --- finding 4: requirement-maybe-uncovered（启发式）---
    # 仅当 tasks 实际存在且有任务项时才做覆盖启发式：tasks 缺失/空时已由
    # no-tasks(high) 覆盖，不再叠加误导性的 medium 噪声。
    if tasks_text is not None and tasks_count > 0:
        for cap, spec_txt in spec_caps.items():
            reqs = parse_requirements(spec_txt)
            if not reqs:
                continue
            if not capability_mentioned_in_tasks(cap, tasks_text):
                findings.append(
                    {
                        "kind": "requirement-maybe-uncovered",
                        "severity": "medium",
                        "detail": (
                            f"capability `{cap}` 有 {len(reqs)} 条 Requirement，但 tasks.md "
                            f"未出现该 capability 关键词（启发式覆盖检查，可能误报）"
                        ),
                    }
                )

    # --- finding 5: tasks-all-done（信息）---
    if tasks_count > 0 and all(t["done"] for t in tasks):
        findings.append(
            {
                "kind": "tasks-all-done",
                "severity": "low",
                "detail": (
                    f"全部 {tasks_count} 个任务已标记 [x] 完成，但命令在 implement 前调用"
                ),
            }
        )

    return {
        "change": change,
        "requirements_count": requirements_count,
        "tasks_count": tasks_count,
        "capabilities": capabilities,
        "findings": findings,
    }


# ============================================================
# 共享：repo / change 定位（便于测试 monkeypatch）
# ============================================================


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    """定位 repo_root。spec analyze 只需 git 仓库（无需 active run / npc init）。

    优先 git toplevel；仅当 cwd 不在 git 仓库时回退 load_paths（兼容显式调试）。
    """
    from . import paths as _paths

    try:
        return _paths.detect_repo_root()
    except _paths.PathsError:
        return _paths.load_paths(args).repo_root


def _arg_change(args: argparse.Namespace) -> str | None:
    """读取 change-id：优先 ``args.change``，回退带连字符的 ``change-id``。"""
    for attr in ("change", "change-id", "change_id"):
        val = getattr(args, attr, None)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


# ============================================================
# handler：npc spec analyze
# ============================================================

# 触发非 0 退出码（drift）的 severity
_DRIFT_SEVERITIES = {"high", "medium"}


def run(args: argparse.Namespace) -> None:
    """``npc spec analyze``：emit 分析结果。

    退出码：ok（无 high/medium finding）→ 0；有 high/medium drift → 1；
    change 目录不存在 → 3(env)；缺 --change → 2(usage)。
    """
    change_id = _arg_change(args)
    if change_id is None:
        _io.emit_error(
            "usage",
            "缺少 --change：请指定 openspec change-id（npc spec analyze --change <id>）",
            exit_code=2,
        )
        return

    from . import paths as _paths

    try:
        repo_root = _resolve_repo_root(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", f"未能定位 repo_root：{e}", exit_code=3)
        return

    change_dir = repo_root / "openspec" / "changes" / change_id
    if not change_dir.is_dir():
        _io.emit_error(
            "change_not_found",
            f"change 目录不存在：{change_dir}",
            exit_code=3,
        )
        return

    result = analyze_change(change_dir)
    has_drift = any(f["severity"] in _DRIFT_SEVERITIES for f in result["findings"])
    _io.emit(
        {
            "ok": not has_drift,
            "change": result["change"],
            "requirements_count": result["requirements_count"],
            "tasks_count": result["tasks_count"],
            "capabilities": result["capabilities"],
            "findings": result["findings"],
        }
    )
    if has_drift:
        raise SystemExit(1)
