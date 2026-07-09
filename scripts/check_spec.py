#!/usr/bin/env python3
"""仓库本地 spec 静态语义 lint —— agent-spine 的写作品味检查，不进 npc。

用法::

    uv run scripts/check_spec.py --change <id>
    uv run scripts/check_spec.py --dir <path>

stdout 输出单行合法 JSON，含键 ``ok``、``change``、``errors``、``warnings``、
``rule_hits``。``errors``/``warnings`` 为数组，每项含 ``rule``、``file``、
``line``、``detail`` 四个键。``rule_hits`` 为映射，键集合恒等于本脚本实现的
全部规则名集合（含零命中项）。当且仅当 ``errors`` 为空时 ``ok`` 为 ``true``；
退出码：存在 ``errors`` → ``1``，否则（仅 ``warnings`` 或全部干净）→ ``0``。

本脚本实现五条**语义层**规则，用于补上 ``openspec validate`` 不管的真空
（``openspec validate`` 只强制 delta header 合法 / Requirement 含
SHALL/MUST / 每条 Requirement 至少一个 Scenario——这三类本脚本 MUST NOT
重复实现）：

- ``deferred_decision_outside_open_questions``：``design.md`` 的
  ``## Open Questions`` 段落之外出现延迟决策措辞。
- ``scenario_missing_when_then``：Scenario 的 ``rawText`` 不同时含
  ``WHEN`` 与 ``THEN``。
- ``vague_adverb``：Requirement / Scenario 正文命中含糊副词表。
- ``proposal_missing_non_goals``：``proposal.md`` 无 Non-Goals/非目标段落。
- ``touchpoint_list_missing_search_command``：``tasks.md`` 中某段落声明了
  多落点清单（≥3 条引用不同文件路径的列表项）却无确定性搜索命令依据
  （段内无含 ``grep``/``rg``/``git grep``/``git diff --name-only`` 的围栏代码块）。

**本版本交付的五条规则 severity 一律为 ``warning``（shadow mode）**：任一
命中都 MUST NOT 使 ``.ok`` 变为 ``false``，MUST NOT 使退出码非零。系统级
结构性问题（``invalid_change_id`` / ``change_not_found`` / ``openspec_missing``）
走 ``errors`` 通道，与这四条内容规则的 severity 无关。

升级判据（写死在此处，供未来对照，不得凭感觉升级）：当
``spec_review.round`` 或 code review 的 ``spec_attribution`` 聚合显示某规则
命中与 ``spec-silent``/``spec-ambiguous``/``spec-contradicted`` 类 blocking
存在跨 change 的稳定关联（**正类样本 ≥ 3 个独立 change**）时，方可将该规则
从 ``warning`` 升为 ``error``。反之，若某规则在观察窗口内长期零触发，按
既有的减法纪律删除它。

本脚本刻意不 import 任何 ``npc`` 模块、不 emit telemetry、不读取
``.npc/config.toml``、不扫描 ``openspec/specs/``——它是仓库本地资产，不是
npc 的一部分（见 ``CLAUDE.md``：npc 只放跨项目通用的原子操作）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ============================================================
# 规则名集合（恒定，供 rule_hits 键集合与负向断言使用）
# ============================================================

RULE_DEFERRED_DECISION = "deferred_decision_outside_open_questions"
RULE_SCENARIO_MISSING_WHEN_THEN = "scenario_missing_when_then"
RULE_VAGUE_ADVERB = "vague_adverb"
RULE_PROPOSAL_MISSING_NON_GOALS = "proposal_missing_non_goals"
RULE_TOUCHPOINT_LIST_MISSING_SEARCH_COMMAND = "touchpoint_list_missing_search_command"

ALL_RULE_NAMES = (
    RULE_DEFERRED_DECISION,
    RULE_SCENARIO_MISSING_WHEN_THEN,
    RULE_VAGUE_ADVERB,
    RULE_PROPOSAL_MISSING_NON_GOALS,
    RULE_TOUCHPOINT_LIST_MISSING_SEARCH_COMMAND,
)

# 多落点清单确定性枚举规则的常量（change: spec-writer-pattern-interrogation）。
# 反引号包裹且含 `/` 或以下列后缀结尾的 token 视为"文件路径引用"。
_TOUCHPOINT_MIN_PATHS = 3
_CODE_FILE_SUFFIXES = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".toml", ".sh",
    ".rs", ".go", ".md", ".yaml", ".yml", ".sql", ".txt",
)
_SEARCH_CMD_SUBSTRINGS = ("grep", "rg", "git grep", "git diff --name-only")
_BACKTICK_TOKEN_RE = re.compile(r"`([^`\n]+)`")

# 延迟决策词表：MUST 只含「延迟 + 决策动词」的谓语短语，MUST NOT 含裸的时间
# 副词（裸的「届时」「再定」「实现时」在本仓库真实语料中会在普通时间状语上
# 误报，dogfood 实测逼出本约束——见 design.md D9）。
DEFERRED_DECISION_PHRASES = (
    "实施时定",
    "届时决定",
    "届时再定",
    "实现时再定",
    "后续再定",
    "待定",
    "暂定",
    "后补",
    "TBD",
    "TODO",
    "to be determined",
    "decide later",
)

# 含糊副词表：Requirement/Scenario 正文命中即视为语义空洞的形容词/副词包装。
VAGUE_ADVERBS = (
    "appropriately",
    "quickly",
    "properly",
    "efficiently",
    "effectively",
    "adequately",
    "reasonably",
    "sufficiently",
    "correctly",
    "suitably",
)

_OPEN_QUESTIONS_SECTION = "Open Questions"

_FENCE_RE = re.compile(r"^\s*```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_H2_RE = re.compile(r"^##(?!#)\s*(.*?)\s*$")


# ============================================================
# 纯函数
# ============================================================


def strip_code_spans(text: str) -> str:
    """剥离 fenced code block（三反引号围栏）与 inline code span（一对反引号）。

    保持行数与行号对应关系：被剥离处以等长空白占位，换行符原样保留。
    """
    lines = text.split("\n")
    out_lines: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            # 围栏起止行本身也置空（无论进入还是退出围栏）
            out_lines.append(" " * len(line))
            in_fence = not in_fence
            continue
        if in_fence:
            out_lines.append(" " * len(line))
        else:
            out_lines.append(_INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), line))
    return "\n".join(out_lines)


def section_of_line(lines: list[str], i: int) -> str | None:
    """返回第 i 行（0-based）所属的 `##` 级段落标题；无标题时返回 None。

    只认恰好两个 `#` 的标题（`##`），忽略 `###` 及更深层级——段落定界只关心
    顶层分区（Context / Decisions / Open Questions / ...）。
    """
    current: str | None = None
    for idx in range(0, i + 1):
        m = _H2_RE.match(lines[idx])
        if m:
            current = m.group(1).strip()
    return current


# ============================================================
# 结构化 finding 构造
# ============================================================


def _finding(rule: str, file: str | None, line: int | None, detail: str) -> dict:
    return {"rule": rule, "file": file, "line": line, "detail": detail}


# ============================================================
# 规则 1：延迟决策（design.md）
# ============================================================


def _check_deferred_decision(design_path: Path) -> list[dict]:
    text = design_path.read_text(encoding="utf-8")
    original_lines = text.split("\n")
    stripped_lines = strip_code_spans(text).split("\n")
    hits: list[dict] = []
    for i, line in enumerate(stripped_lines):
        section = section_of_line(original_lines, i)
        if section == _OPEN_QUESTIONS_SECTION:
            continue
        for phrase in DEFERRED_DECISION_PHRASES:
            if phrase in line:
                hits.append(_finding(RULE_DEFERRED_DECISION, str(design_path), i + 1, phrase))
                break
    return hits


# ============================================================
# 规则 4：proposal 缺 Non-Goals（proposal.md）
# ============================================================


def _proposal_line_is_non_goals_label(line: str) -> bool:
    """判断一行是否为 Non-Goals / 非目标 段落标签。

    本仓库实际语料中该"段落"既可能是 `## Non-Goals` 这样的 markdown 标题，
    也可能是 `**非目标（Non-Goals）**：` 这样的加粗行内伪标题（三个已归档
    change 的 proposal.md 均用后者）。先去尾部冒号、再去加粗 `*`、再去标题
    `#`，剩余文本以 `non-goals`/`非目标` 开头才算命中，避免把正文里顺带
    提到 "Non-Goals" 的散文句子也算数。
    """
    candidate = line.strip()
    if not candidate:
        return False
    candidate = candidate.rstrip(":：").strip()
    candidate = candidate.strip("*").strip()
    candidate = candidate.lstrip("#").strip()
    return candidate.lower().startswith("non-goals") or candidate.startswith("非目标")


def _check_proposal_non_goals(proposal_path: Path) -> dict | None:
    text = proposal_path.read_text(encoding="utf-8")
    for line in text.split("\n"):
        if _proposal_line_is_non_goals_label(line):
            return None
    return _finding(
        RULE_PROPOSAL_MISSING_NON_GOALS,
        str(proposal_path),
        None,
        "proposal.md 缺少 Non-Goals / 非目标 段落标题",
    )


# ============================================================
# 规则 5：touchpoint_list_missing_search_command（tasks.md）
# ============================================================


def _line_file_path_tokens(line: str) -> list[str]:
    """返回一行里被反引号包裹、且是**相对文件路径**（含 `/`）的 token 列表。

    只认「看起来像相对路径」的 token：含 `/`、以常见代码文件后缀结尾、且不含
    符号限定符 `::`/空白/glob `*`/尖括号 `<>` 等非路径字符。dogfood 本规则对本仓库
    真实语料（本 change 自身的 tasks.md）跑一遍逼出这一约束——TDD 段落里大量出现
    `src/npc/x.py::func`（符号引用，非落点路径）、`pattern-interrogation.md`（裸产物
    文件名，非跨落点路径）、`round-*.spec-review.json`（glob 通配）会让朴素匹配把普通
    TDD 段误判为"多落点清单"。真正的落点清单（跨目录枚举待改文件）用的是完整相对
    路径 token（`plugins/.../spine-spec.md` 这类），据此收紧。与既有 vague_adverb /
    deferred_decision 规则同理——启发式按真实误报语料迭代。
    """
    out: list[str] = []
    for m in _BACKTICK_TOKEN_RE.finditer(line):
        tok = m.group(1).strip()
        if any(bad in tok for bad in ("::", " ", "*", "<", ">")):
            continue
        if tok.endswith("/"):
            continue
        if "/" in tok and tok.endswith(_CODE_FILE_SUFFIXES):
            out.append(tok)
    return out


def _check_touchpoint_lists(tasks_path: Path) -> list[dict]:
    """扫描 ``tasks.md``：某 ``##`` 段落若含 ≥3 条引用**不同**文件路径的列表项，
    段内 MUST 有一个含 ``grep``/``rg``/``git grep``/``git diff --name-only`` 的围栏
    代码块，否则命中 ``touchpoint_list_missing_search_command``。

    段落定界口径与 :func:`section_of_line` 一致（只认恰好两个 ``#`` 的标题）；
    围栏代码块的进出判定复用 :data:`_FENCE_RE`。以 ``warning`` 交付（shadow mode）。
    """
    text = tasks_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # 每个段落以其标题行索引为键，保证同名标题不折叠、line 号精确。
    sections: dict[int, dict] = {}
    current_key: int | None = None
    in_fence = False
    fence_buf: list[str] = []

    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            if in_fence:
                blob = "\n".join(fence_buf)
                if current_key is not None and any(s in blob for s in _SEARCH_CMD_SUBSTRINGS):
                    sections[current_key]["has_search"] = True
                fence_buf = []
            in_fence = not in_fence
            continue
        if in_fence:
            fence_buf.append(line)
            continue
        m = _H2_RE.match(line)
        if m:
            current_key = i
            sections[current_key] = {
                "title": m.group(1).strip(),
                "paths": set(),
                "has_search": False,
                "line": i + 1,
            }
            continue
        if current_key is not None and line.lstrip().startswith("- "):
            for tok in _line_file_path_tokens(line):
                sections[current_key]["paths"].add(tok)

    hits: list[dict] = []
    for key in sorted(sections):
        info = sections[key]
        if len(info["paths"]) >= _TOUCHPOINT_MIN_PATHS and not info["has_search"]:
            hits.append(
                _finding(
                    RULE_TOUCHPOINT_LIST_MISSING_SEARCH_COMMAND,
                    str(tasks_path),
                    info["line"],
                    f"多落点清单缺确定性搜索命令依据：## {info['title']}",
                )
            )
    return hits


# ============================================================
# 规则 2 & 3：scenario_missing_when_then / vague_adverb（消费 openspec 解析产物）
# ============================================================


def _contains_vague_adverb(text: str) -> str | None:
    for adverb in VAGUE_ADVERBS:
        if re.search(rf"\b{re.escape(adverb)}\b", text, re.IGNORECASE):
            return adverb
    return None


def _check_scenarios_and_adverbs(deltas_payload: dict, file_label: str) -> tuple[list[dict], list[dict]]:
    scenario_hits: list[dict] = []
    adverb_hits: list[dict] = []
    for delta in deltas_payload.get("deltas", []) or []:
        requirement = delta.get("requirement") or {}
        req_text = requirement.get("text") or ""
        # vague_adverb 匹配前先剥离 inline code span：dogfood 本脚本对
        # repo-spec-lint 自身的 spec.md 跑一遍时发现，本 Requirement/Scenario
        # 用反引号引用「含糊副词」示例文本（例如 `...appropriately and
        # quickly.`）来描述这条规则本身，朴素子串匹配会把这段自我描述的
        # 示例误判为该文档自己写得含糊。与 deferred_decision 规则同理，
        # 反引号内的文本视为「引用/讨论」而非「陈述」。
        adverb = _contains_vague_adverb(strip_code_spans(req_text))
        if adverb:
            adverb_hits.append(_finding(RULE_VAGUE_ADVERB, file_label, None, adverb))
        for scenario in requirement.get("scenarios", []) or []:
            raw = scenario.get("rawText") or ""
            if not ("WHEN" in raw and "THEN" in raw):
                detail = raw.strip().splitlines()[0][:120] if raw.strip() else "(empty scenario)"
                scenario_hits.append(_finding(RULE_SCENARIO_MISSING_WHEN_THEN, file_label, None, detail))
            scenario_adverb = _contains_vague_adverb(strip_code_spans(raw))
            if scenario_adverb:
                adverb_hits.append(_finding(RULE_VAGUE_ADVERB, file_label, None, scenario_adverb))
    return scenario_hits, adverb_hits


class OpenspecMissingError(Exception):
    """openspec 二进制不在 PATH 中。"""


def _load_openspec_deltas(change_id: str) -> dict:
    openspec_bin = shutil.which("openspec")
    if openspec_bin is None:
        raise OpenspecMissingError("openspec not found in PATH")
    proc = subprocess.run(
        [openspec_bin, "show", change_id, "--json", "--deltas-only"],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        # openspec 输出异常（非本脚本的错误场景），降级为空 deltas，不崩溃。
        return {"deltas": []}


# ============================================================
# --change 校验
# ============================================================


def validate_change_id(value: str) -> str | None:
    """校验 `--change` 取值。合法返回 None，非法返回错误 detail 字符串。"""
    if not value or "/" in value or ".." in value:
        return f"invalid change id: {value!r} (must be a single path segment, no '/' or '..')"
    return None


# ============================================================
# 核心 lint 入口
# ============================================================


def lint_change(change_id: str | None, dir_path: str | None, changes_root: Path | None = None) -> dict:
    """对一个 change 目录（`--change` 或 `--dir`）跑全部规则，返回结果 dict。"""
    errors: list[dict] = []
    warnings: list[dict] = []
    rule_hits: dict[str, int] = {name: 0 for name in ALL_RULE_NAMES}

    if change_id is not None:
        invalid_detail = validate_change_id(change_id)
        if invalid_detail is not None:
            errors.append(_finding("invalid_change_id", None, None, invalid_detail))
            return _finalize(change_id, errors, warnings, rule_hits)

        root = changes_root if changes_root is not None else (Path("openspec") / "changes")
        target_dir = root / change_id
        if not target_dir.is_dir():
            errors.append(
                _finding("change_not_found", str(target_dir), None, f"change directory not found: {target_dir}")
            )
            return _finalize(change_id, errors, warnings, rule_hits)
        dir_mode = False
        change_label = change_id
    else:
        target_dir = Path(dir_path) if dir_path is not None else None
        if target_dir is None:
            errors.append(_finding("invalid_invocation", None, None, "either --change or --dir is required"))
            return _finalize(None, errors, warnings, rule_hits)
        dir_mode = True
        change_label = str(target_dir)

    # 规则 1：deferred_decision_outside_open_questions（design.md，可选 artifact）
    design_path = target_dir / "design.md"
    if design_path.is_file():
        hits = _check_deferred_decision(design_path)
        rule_hits[RULE_DEFERRED_DECISION] = len(hits)
        warnings.extend(hits)

    # 规则 4：proposal_missing_non_goals（proposal.md）
    proposal_path = target_dir / "proposal.md"
    if proposal_path.is_file():
        finding = _check_proposal_non_goals(proposal_path)
        if finding is not None:
            rule_hits[RULE_PROPOSAL_MISSING_NON_GOALS] = 1
            warnings.append(finding)

    # 规则 5：touchpoint_list_missing_search_command（tasks.md）。只读 tasks.md，
    # 不依赖 `openspec show`，故 --change 与 --dir 两种模式下均生效、MUST NOT 跳过。
    tasks_path = target_dir / "tasks.md"
    if tasks_path.is_file():
        t_hits = _check_touchpoint_lists(tasks_path)
        rule_hits[RULE_TOUCHPOINT_LIST_MISSING_SEARCH_COMMAND] = len(t_hits)
        warnings.extend(t_hits)

    # 规则 2 & 3：依赖 `openspec show --json --deltas-only`，--dir 模式下跳过
    # （openspec show 只认 active change id，--dir 可能指向 fixture / archive）。
    if not dir_mode:
        try:
            deltas_payload = _load_openspec_deltas(change_id)
        except OpenspecMissingError as exc:
            errors.append(_finding("openspec_missing", None, None, str(exc)))
            return _finalize(change_label, errors, warnings, rule_hits)

        file_label = str(target_dir / "specs")
        scenario_hits, adverb_hits = _check_scenarios_and_adverbs(deltas_payload, file_label)
        rule_hits[RULE_SCENARIO_MISSING_WHEN_THEN] = len(scenario_hits)
        warnings.extend(scenario_hits)
        rule_hits[RULE_VAGUE_ADVERB] = len(adverb_hits)
        warnings.extend(adverb_hits)

    return _finalize(change_label, errors, warnings, rule_hits)


def _finalize(change_label: str | None, errors: list[dict], warnings: list[dict], rule_hits: dict[str, int]) -> dict:
    return {
        "ok": len(errors) == 0,
        "change": change_label,
        "errors": errors,
        "warnings": warnings,
        "rule_hits": rule_hits,
    }


# ============================================================
# CLI 入口
# ============================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_spec.py",
        description="agent-spine 仓库本地 spec 静态语义 lint（shadow mode，五条规则均为 warning）。",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--change", help="openspec/changes/<id> 下的单段 change id（拒绝 '/' 与 '..'）")
    group.add_argument("--dir", help="直接检查任意含 design.md 的目录（供 fixture / archive 使用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    result = lint_change(args.change, args.dir)
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
