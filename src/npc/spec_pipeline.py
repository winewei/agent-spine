"""spec 生成 + 独立语义评审流水线（change: ``spine-spec-writer``）。

与既有 implement/fix/review 三件套结构同构，但**刻意与 STATE_JSON.progress 解耦**：
spec 生成先于 change 被纳入任何 run 的 progress 数组（先有 spec，才谈得上按它
`/spine-run`），因此本模块不依赖 ``--seq``，只依赖 ``--change <id>``，产物落在
``<run_dir>/spec-<change_id>/`` 下，全部以磁盘上 ``round-N.*`` 文件为事实源。

三个 CLI 子命令族：

- ``npc spec write run|record``：渲染 write 轮 prompt / 装订 write 轮 RESULT。
  write 轮 prompt **MUST NOT** 包含任何 spec-review 的 rubric / category 枚举 /
  findings 原文（不变量 1：生成侧不得预知本轮评判标准）。
- ``npc spec fix run|record``：渲染 fix 轮 prompt（只含**上一轮已签发**的
  blocking findings）/ 装订 fix 轮 RESULT。
- ``npc spec review run``：门顺序 ``openspec validate --strict`` →
  ``[spec_review] gate_cmd``（便宜、确定性）→ LLM 语义评审（贵）。npc 只读
  gate 命令 stdout 的 ``ok``/``rule_hits`` 两个键，不持有任何规则语义。

固定轮次上限的 fix 循环见 :func:`run_spec_fix_loop`（纯函数，不复用 code review
的「blocking 单调下降代表收敛」stale 检测——spec 的 ambiguity/scope-creep
可以在改写后反弹，blocking 单调下降不是这里的收敛前提）。

越界修改的确定性拦截见 :func:`_scope_guard_violation`：``record`` 装订前用
``git status --porcelain`` 校验变更集仅限 ``openspec/changes/<id>/``，并比对
render 时记下的基线 HEAD，防止 ``spine-spec-writer``（持有 Bash）越界改代码
或提交 commit——这是代码级硬轨，不是 prompt 文案的口头约束。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
from pathlib import Path

import jsonschema

from . import _io, paths as _paths, telemetry as _telemetry, templates
from .config import Config, ConfigError, load_config
from .engines import ClaudeEngine, CodexEngine, EngineError, ReviewRunInputs
from .fixer import render_findings
from .pipeline import (
    _emit_and_exit,
    _parse_and_validate_result_line,
    _portable_timeout_bin,
)
from .schema import SPEC_REVIEW_SCHEMA, ensure_schema
from .verify import check_routing


BLOCKING_SEVERITIES = {"critical", "high"}
SPEC_SCHEMA_FILENAME = ".new-plan-spec-review-schema.json"

# 模式盘问产物（change: spec-writer-pattern-interrogation）的三个必需 H2 标题。
# 顺序即 record / write gate 报告缺失标题时的规范顺序（二者共用同一实现，
# MUST NOT 出现"record 通过但 write gate 拒绝"的判定分歧）。
PATTERN_INTERROGATION_FILENAME = "pattern-interrogation.md"
PATTERN_INTERROGATION_REQUIRED_SECTIONS = ("## Analogs", "## Assumptions", "## Open Questions")
_USER_DECISIONS_HEADING = "## User Decisions (Interactive)"


# ============================================================
# 路径
# ============================================================


def _spec_base(p: _paths.Paths, change_id: str) -> Path:
    """spec 生成/评审产物子目录（无 seq 前缀，见模块 docstring）。"""
    return p.run_dir / f"spec-{change_id}"


def _change_dir(repo_root: Path, change_id: str) -> Path:
    return repo_root / "openspec" / "changes" / change_id


def _spec_schema_path(p: _paths.Paths) -> Path:
    return p.task_log_dir / SPEC_SCHEMA_FILENAME


def _find_openspec_bin(override: str | None = None) -> str:
    import shutil

    if override:
        return override
    b = shutil.which("openspec")
    if not b:
        raise FileNotFoundError("未在 PATH 中找到 openspec 命令")
    return b


def _find_codex_bin(override: str | None = None) -> str:
    import shutil

    if override:
        return override
    b = shutil.which("codex")
    if not b:
        raise FileNotFoundError("未在 PATH 中找到 codex 命令；请先安装")
    return b


def _find_claude_bin(override: str | None = None) -> str:
    import shutil

    if override:
        return override
    b = shutil.which("claude")
    if not b:
        raise FileNotFoundError(
            "未在 PATH 中找到 claude 命令；请安装 Claude Code CLI 或在 [spec_review] claude_bin 指定"
        )
    return b


# ============================================================
# parse_spec_review（纯函数，tasks 3.4）
# ============================================================


def parse_spec_review(review_json: dict) -> dict:
    """从 spec review JSON 派生指标。纯函数。

    与既有 ``review.parse_review`` 的差异：blocking 只看 ``severity``
    （spec finding 无 ``in_scope`` 概念，见 change design.md D2）。
    """
    findings = review_json.get("findings") or []
    if not isinstance(findings, list):
        raise ValueError("spec review findings 必须是数组")

    blocking_list: list[dict] = []
    advisory_count = 0
    categories: list[str] = []
    seen: set[str] = set()

    for f in findings:
        sev = f.get("severity")
        cat = f.get("category")
        if sev in BLOCKING_SEVERITIES:
            blocking_list.append(f)
            if cat and cat not in seen:
                seen.add(cat)
                categories.append(cat)
        else:
            advisory_count += 1

    blocking_list.sort(key=lambda x: x.get("id", ""))
    return {
        "verdict": review_json.get("verdict"),
        "blocking": len(blocking_list),
        "advisory": advisory_count,
        "blocking_categories": categories,
        "blocking_findings": blocking_list,
    }


# ============================================================
# 路由不变量前置校验（tasks 8.3c / 8.3c2 / D5c）
# ============================================================


def _spec_routing_violations(cfg: Config) -> list[dict]:
    """只取 ``check_routing`` 里以 ``spec_`` 开头的 violation（路由真相源唯一）。"""
    return [v for v in check_routing(cfg) if v.get("rule", "").startswith("spec_")]


# ============================================================
# gate_cmd（argv 数组 + shell=False；npc 只读 ok/rule_hits，tasks 2.x）
# ============================================================


def _run_gate_cmd(
    gate_cmd: tuple[str, ...] | list[str] | None,
    change_id: str,
    repo_root: Path,
    runner=subprocess.run,
) -> dict:
    """执行 ``[spec_review] gate_cmd``。返回 dict：

    ``{"skipped": bool, "ok": bool, "rule_hits": dict, "error": str|None, "argv": list|None}``

    npc 只解析 stdout JSON 的 ``ok``/``rule_hits`` 两个键，不解读任何规则语义
    （见 D3b：本函数源码 MUST NOT 出现任何规则名字符串）。
    """
    if not gate_cmd:
        return {"skipped": True, "ok": True, "rule_hits": {}, "error": None, "argv": None}

    argv = list(gate_cmd) + ["--change", change_id]
    try:
        proc = runner(argv, shell=False, cwd=str(repo_root), capture_output=True, text=True)
    except OSError as e:
        return {
            "skipped": False,
            "ok": False,
            "rule_hits": {},
            "error": f"gate_error:{e}",
            "argv": argv,
        }

    stdout = proc.stdout or ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "skipped": False,
            "ok": False,
            "rule_hits": {},
            "error": "gate_output_invalid",
            "argv": argv,
        }
    if not isinstance(data, dict) or "ok" not in data:
        return {
            "skipped": False,
            "ok": False,
            "rule_hits": {},
            "error": "gate_output_invalid",
            "argv": argv,
        }

    rule_hits = data.get("rule_hits")
    if not isinstance(rule_hits, dict):
        rule_hits = {}
    return {
        "skipped": False,
        "ok": bool(data.get("ok")),
        "rule_hits": rule_hits,
        "error": None,
        "argv": argv,
    }


# ============================================================
# 越界修改的确定性拦截（D5b，tasks 8.2.x）
# ============================================================


def _git_status_paths(repo_root: Path, runner=subprocess.run) -> list[str]:
    """``git status --porcelain`` 的变更路径列表（rename 展开为 old + new 两项）。

    显式传 ``--untracked-files=all``：默认 porcelain 模式下，若某目录**整体**未跟踪，
    git 会把它折叠成单条 ``?? <dir>/`` 而不逐个列出目录内文件——这会让越界扫描
    误判"目录路径未落在 change 目录前缀内"（即便目录内所有文件其实都在范围内），
    也可能反向漏报（该目录下混了越界文件却被折叠成一条看似无害的父目录路径）。
    """
    proc = runner(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    paths: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        rest = line[3:] if len(line) > 3 else line.lstrip()
        if " -> " in rest:
            old, new = rest.split(" -> ", 1)
            paths.append(old.strip().strip('"'))
            paths.append(new.strip().strip('"'))
        else:
            paths.append(rest.strip().strip('"'))
    return paths


def _git_head(repo_root: Path, runner=subprocess.run) -> str | None:
    proc = runner(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _out_of_scope_paths(paths: list[str], change_id: str) -> list[str]:
    prefix = f"openspec/changes/{change_id}/"
    return [pth for pth in paths if not pth.startswith(prefix)]


def _write_pre_head_marker(p: _paths.Paths, base: Path, marker_name: str) -> None:
    """render 时（write/fix run）记下基线 HEAD，供 record 时比对是否意外产生了 commit。"""
    head = _git_head(p.repo_root)
    if head is None:
        return
    base.mkdir(parents=True, exist_ok=True)
    (base / marker_name).write_text(head, encoding="utf-8")


def _scope_guard_violation(
    p: _paths.Paths, change_id: str, base: Path, marker_name: str
) -> dict | None:
    """越界扫描：返回 None 表示通过，否则返回违规 dict（含 ``error``/其它字段）。"""
    paths = _git_status_paths(p.repo_root)
    out_of_scope = _out_of_scope_paths(paths, change_id)
    if out_of_scope:
        return {"error": "out_of_scope_changes", "paths": out_of_scope}

    marker = base / marker_name
    if marker.is_file():
        pre_head = marker.read_text(encoding="utf-8").strip()
        cur_head = _git_head(p.repo_root)
        if pre_head and cur_head and pre_head != cur_head:
            return {"error": "unexpected_commit", "pre_head": pre_head, "head": cur_head}
    return None


# ============================================================
# 模式盘问 pattern-interrogation.md 的 H2 段落定界（共享辅助，D3）
# ============================================================
#
# `spec_interrogate_record` 用它统计 `## Open Questions` 的顶层 bullet 数；
# `spec_write_run` 的硬门用它检查三个必需标题是否存在——**同一实现，两处调用**，
# 不分裂为两套解析逻辑（tasks 3.7）。刻意不 import `scripts/check_spec.py`：
# 后者是仓库本地资产，`spec_pipeline.py` 属 npc，两者边界不能破，故在此独立实现
# 一份等价的最小段落定界逻辑（design D3）。


def _h2_section_bullets(text: str, heading: str) -> tuple[bool, int]:
    """给定一个完整 H2 标题（如 ``## Open Questions``），返回 ``(present, bullet_count)``：

    - ``present``：该 H2 标题是否作为独立标题行存在（精确匹配整行去空白后的文本）。
    - ``bullet_count``：该标题之下、下一个 ``## `` 标题之前，匹配 ``- `` 的顶层
      bullet 行数（缩进的子 bullet 不计；标题不存在时恒为 0）。

    只认恰好两个 ``#`` 的标题（``##``），与 ``check_spec.section_of_line`` 同款定界口径。
    """
    want = heading.strip()
    lines = text.split("\n")
    in_section = False
    present = False
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            if stripped == want:
                present = True
                in_section = True
                continue
            # 遇到任意其它 H2 标题即结束当前段落
            if in_section:
                in_section = False
            continue
        if in_section:
            # 顶层 bullet：行首（无缩进）以 "- " 开头
            if line.startswith("- "):
                count += 1
    return present, count


def _missing_pattern_sections(text: str) -> list[str]:
    """返回 ``pattern-interrogation.md`` 正文缺失的必需 H2 标题（规范顺序）。

    ``record`` 与 ``write gate`` 共用本函数，保证二者对同一 fixture 给出**完全相同**
    的缺失标题集合（无判定分歧）。
    """
    return [
        h for h in PATTERN_INTERROGATION_REQUIRED_SECTIONS if not _h2_section_bullets(text, h)[0]
    ]


def _pattern_interrogation_path(repo_root: Path, change_id: str) -> Path:
    return _change_dir(repo_root, change_id) / PATTERN_INTERROGATION_FILENAME


# ============================================================
# npc spec interrogate run|record|decide（change: spec-writer-pattern-interrogation）
# ============================================================


def spec_interrogate_run(
    p: _paths.Paths,
    change_id: str,
    *,
    config_path: Path | None = None,
    goal: str | None = None,
) -> dict:
    """渲染 interrogate 轮 prompt，恒 in-session（``deferred=true``）。

    与 :func:`spec_write_run`/:func:`spec_fix_run` 复用**同一套** routing 检查
    （``_spec_routing_violations``），不引入第二套后端白名单（D8）。

    ``goal``：来自 ``/spine-spec`` 的用户一句话原始目标，原文透传给
    ``render_spec_interrogator``；为 ``None``/空串时不渲染目标段落。
    """
    try:
        cfg = load_config(p.repo_root, override_path=config_path)
    except ConfigError as e:
        raise ValueError(str(e)) from e

    violations = _spec_routing_violations(cfg)
    if violations:
        return {"ok": False, "error": "spec_routing_violation", "violations": violations}

    base = _spec_base(p, change_id)
    base.mkdir(parents=True, exist_ok=True)
    _write_pre_head_marker(p, base, "pre_head.interrogate.txt")

    prompt_file = base / "pattern-interrogation.prompt.md"
    text = templates.render_spec_interrogator(
        change_id=change_id,
        base=str(base),
        repo_root=str(p.repo_root),
        goal=goal or None,
    )
    prompt_file.write_text(text, encoding="utf-8")

    spawn_text = templates.render_spawn_prompt(
        phase="spec_interrogate", change_id=change_id, prompt_file=str(prompt_file)
    )

    return {
        "ok": True,
        "change": change_id,
        "deferred": True,
        "spawn_prompt": spawn_text,
        "prompt_file": str(prompt_file),
    }


def spec_interrogate_record(p: _paths.Paths, change_id: str, result_line: str) -> dict:
    """装订 interrogate 轮 RESULT，并**独立**解析 ``## Open Questions`` 的 bullet 数。

    顺序（tasks 3.7a）：解析 + 校验 RESULT 必需键 → 越界/意外 commit 拦截 →
    读磁盘 ``pattern-interrogation.md`` → 三个必需标题存在性 → 统计 Open Questions。
    ``.open_questions`` 由 npc 独立数出，**不采信** writer 自报（不变量 2）。
    """
    parsed, missing = _parse_and_validate_result_line(result_line, "spec_interrogate")
    if parsed is None:
        return {"ok": False, "change": change_id, "error": "result-line-missing"}
    if missing:
        return {
            "ok": False,
            "change": change_id,
            "error": "result-missing-keys",
            "missing_keys": missing,
        }

    base = _spec_base(p, change_id)
    violation = _scope_guard_violation(p, change_id, base, "pre_head.interrogate.txt")
    if violation is not None:
        return {"ok": False, "change": change_id, **violation}

    art_path = _pattern_interrogation_path(p.repo_root, change_id)
    if not art_path.is_file():
        return {
            "ok": False,
            "change": change_id,
            "error": "pattern_interrogation_missing",
            "detail": f"{art_path} 不存在（interrogate 轮 MUST 产出 pattern-interrogation.md）",
        }

    text = art_path.read_text(encoding="utf-8")
    missing_sections = _missing_pattern_sections(text)
    if missing_sections:
        return {
            "ok": False,
            "change": change_id,
            "error": "pattern_interrogation_missing_section",
            "missing_sections": missing_sections,
        }

    _present, open_questions = _h2_section_bullets(text, "## Open Questions")
    return {
        "ok": True,
        "change": change_id,
        "artifacts": parsed.get("artifacts", "-"),
        "summary": parsed.get("summary", "-"),
        "open_questions": open_questions,
    }


def spec_interrogate_decide(p: _paths.Paths, change_id: str, decisions_md: str) -> dict:
    """纯机械文本追加：把用户对 Open Questions 的裁决原文追加进
    ``pattern-interrogation.md`` 的 ``## User Decisions (Interactive)`` 段（D4）。

    一次性、不覆盖：文件已含该段落时以 ``decisions_already_recorded`` 拒绝，逐字节
    不改动文件。npc **不解析、不改写、不做语义判断** ``decisions_md`` 的内容。
    """
    art_path = _pattern_interrogation_path(p.repo_root, change_id)
    if not art_path.is_file():
        return {
            "ok": False,
            "change": change_id,
            "error": "pattern_interrogation_missing",
            "detail": f"{art_path} 不存在",
        }

    text = art_path.read_text(encoding="utf-8")
    present, _count = _h2_section_bullets(text, _USER_DECISIONS_HEADING)
    if present:
        return {
            "ok": False,
            "change": change_id,
            "error": "decisions_already_recorded",
        }

    art_path.write_text(
        text + f"\n\n{_USER_DECISIONS_HEADING}\n\n{decisions_md}\n", encoding="utf-8"
    )
    return {"ok": True, "change": change_id}


# ============================================================
# npc spec write run|record
# ============================================================


def spec_write_run(
    p: _paths.Paths,
    change_id: str,
    *,
    config_path: Path | None = None,
    goal: str | None = None,
    lessons_path: str | None = None,
) -> dict:
    """``goal``：来自 ``/spine-spec`` 的用户一句话原始目标（原文透传给
    ``render_spec_writer``）。为 ``None``/空串时不渲染目标段落（已存在
    change-id 补全/修复分支）。

    ``lessons_path``：pilot-rewrite-gate 回写场景注入的 run 级 ``lessons.md`` 绝对路径
    （独立于 ``goal`` 的参考段落，二者并列不覆盖）。为 ``None``/空串时不渲染，prompt
    与现状逐字等价。"""
    try:
        cfg = load_config(p.repo_root, override_path=config_path)
    except ConfigError as e:
        raise ValueError(str(e)) from e

    violations = _spec_routing_violations(cfg)
    if violations:
        return {"ok": False, "error": "spec_routing_violation", "violations": violations}

    # 模式盘问硬前置门（D2，change: spec-writer-pattern-interrogation）：
    # 排在 routing 校验**之后**——routing 违规恒短路，两道门错误标识永不冲突。
    # 上游产物缺失/结构缺陷时下游拒绝渲染，MUST NOT 写出任何 write prompt 文件。
    # 分支 A（--goal 透传）与分支 B（补全既有 change）一视同仁地前置。
    art_path = _pattern_interrogation_path(p.repo_root, change_id)
    if not art_path.is_file():
        return {
            "ok": False,
            "change": change_id,
            "error": "pattern_interrogation_missing",
            "detail": f"{art_path} 不存在（write 轮前必须先完成 npc spec interrogate）",
        }
    missing_sections = _missing_pattern_sections(art_path.read_text(encoding="utf-8"))
    if missing_sections:
        return {
            "ok": False,
            "change": change_id,
            "error": "pattern_interrogation_missing_section",
            "missing_sections": missing_sections,
        }

    base = _spec_base(p, change_id)
    base.mkdir(parents=True, exist_ok=True)
    _write_pre_head_marker(p, base, "pre_head.write.txt")

    prompt_file = base / "spec-write.prompt.md"
    text = templates.render_spec_writer(
        change_id=change_id,
        base=str(base),
        repo_root=str(p.repo_root),
        goal=goal,
        lessons_path=lessons_path or None,
    )
    prompt_file.write_text(text, encoding="utf-8")

    spawn_text = templates.render_spawn_prompt(
        phase="spec_write", change_id=change_id, prompt_file=str(prompt_file)
    )

    return {
        "ok": True,
        "change": change_id,
        "deferred": True,
        "spawn_prompt": spawn_text,
        "prompt_file": str(prompt_file),
    }


def spec_write_record(p: _paths.Paths, change_id: str, result_line: str) -> dict:
    parsed, missing = _parse_and_validate_result_line(result_line, "spec_write")
    if parsed is None:
        return {"ok": False, "change": change_id, "error": "result-line-missing"}
    if missing:
        return {
            "ok": False,
            "change": change_id,
            "error": "result-missing-keys",
            "missing_keys": missing,
        }

    base = _spec_base(p, change_id)
    violation = _scope_guard_violation(p, change_id, base, "pre_head.write.txt")
    if violation is not None:
        return {"ok": False, "change": change_id, **violation}

    return {
        "ok": True,
        "change": change_id,
        "artifacts": parsed.get("artifacts", "-"),
        "validate": parsed.get("validate", "-"),
        "summary": parsed.get("summary", "-"),
    }


# ============================================================
# npc spec fix run|record
# ============================================================


def spec_fix_run(
    p: _paths.Paths, change_id: str, round_n: int, *, config_path: Path | None = None
) -> dict:
    try:
        cfg = load_config(p.repo_root, override_path=config_path)
    except ConfigError as e:
        raise ValueError(str(e)) from e

    violations = _spec_routing_violations(cfg)
    if violations:
        return {"ok": False, "error": "spec_routing_violation", "violations": violations}

    base = _spec_base(p, change_id)
    base.mkdir(parents=True, exist_ok=True)

    prev_round = round_n - 1
    prev_review_path = base / f"round-{prev_round}.spec-review.json"
    if not prev_review_path.exists():
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "error": "prev_spec_review_missing",
            "detail": f"{prev_review_path} 不存在（fix 轮 {round_n} 需要 round-{prev_round}.spec-review.json）",
        }

    try:
        prev_review = json.loads(prev_review_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "error": "invalid_json",
            "detail": str(e),
        }

    try:
        parsed = parse_spec_review(prev_review)
    except ValueError as e:
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "error": "invalid_schema",
            "detail": str(e),
        }

    findings_md = render_findings(parsed["blocking_findings"])

    marker_name = f"pre_head.fix-r{round_n}.txt"
    _write_pre_head_marker(p, base, marker_name)

    prompt_file = base / f"round-{round_n}.spec-fix.prompt.md"
    text = templates.render_spec_fixer(
        change_id=change_id,
        round_n=round_n,
        base=str(base),
        repo_root=str(p.repo_root),
        blocking_findings_md=findings_md,
    )
    prompt_file.write_text(text, encoding="utf-8")

    spawn_text = templates.render_spawn_prompt(
        phase="spec_fix", change_id=change_id, prompt_file=str(prompt_file)
    )

    return {
        "ok": True,
        "change": change_id,
        "round": round_n,
        "deferred": True,
        "spawn_prompt": spawn_text,
        "prompt_file": str(prompt_file),
        "blocking_count": len(parsed["blocking_findings"]),
    }


def spec_fix_record(p: _paths.Paths, change_id: str, round_n: int, result_line: str) -> dict:
    parsed, missing = _parse_and_validate_result_line(result_line, "spec_fix")
    if parsed is None:
        return {"ok": False, "change": change_id, "round": round_n, "error": "result-line-missing"}
    if missing:
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "error": "result-missing-keys",
            "missing_keys": missing,
        }

    base = _spec_base(p, change_id)
    marker_name = f"pre_head.fix-r{round_n}.txt"
    violation = _scope_guard_violation(p, change_id, base, marker_name)
    if violation is not None:
        return {"ok": False, "change": change_id, "round": round_n, **violation}

    fixed_str = parsed.get("fixed", "0")
    try:
        fixed = int(fixed_str)
    except ValueError:
        fixed = 0

    return {
        "ok": True,
        "change": change_id,
        "round": round_n,
        "fixed": fixed,
        "validate": parsed.get("validate", "-"),
        "summary": parsed.get("summary", "-"),
    }


# ============================================================
# npc spec review run（tasks 2.x / 3.x / 7.x）
# ============================================================


def _spec_engine_exec(
    *,
    engine_name: str,
    repo_root: Path,
    schema_path: Path,
    focus_text: str,
    review_out: Path,
    events_out: Path,
    timeout_sec: int,
    engine_bin: str,
    portable_timeout: Path,
    model: str | None = None,
) -> int:
    """单次引擎调用（codex/claude 二选一）。以函数形式暴露作为 test seam：

    测试用它断言"零调用"（monkeypatch 后 call_count==0）。
    """
    if engine_name == "codex":
        return CodexEngine(engine_bin).run(
            ReviewRunInputs(
                repo_root=repo_root,
                schema_path=schema_path,
                focus_text=focus_text,
                review_out=review_out,
                events_out=events_out,
                timeout_sec=timeout_sec,
                portable_timeout=portable_timeout,
            )
        )
    return ClaudeEngine(engine_bin, model=model).run(
        ReviewRunInputs(
            repo_root=repo_root,
            schema_path=schema_path,
            focus_text=focus_text,
            review_out=review_out,
            events_out=events_out,
            timeout_sec=timeout_sec,
            portable_timeout=portable_timeout,
        )
    )


def _render_spec_review_focus(change_id: str, change_dir: Path) -> str:
    """spec review 的 focus 文本：让引擎读 change 目录下 artifact 并给出语义判定。

    与 code review 的 focus（diff 驱动）不同，spec review 直接指向 artifact
    目录——spec 无 diff 作用域概念（D2）。
    """
    return f"""你是 OpenSpec change 语义评审专家。请评审 change `{change_id}` 的 artifact。

## 待评审目录

{change_dir}

请阅读该目录下的 proposal.md / design.md / tasks.md / specs/**/spec.md（存在的文件），
从以下角度评审 spec 的语义质量（不评审代码实现，本 change 尚未实施）：

- ambiguity：Requirement/Scenario 存在多种合理解读
- missing-scenario：应存在但缺失的 Scenario（如缺 abort/错误路径）
- implementation-leak：spec 里过早写死实现细节
- untestable：Scenario 无法转化为确定性测试断言
- deferred-decision：写了"实施时定"等延迟决策措辞
- contradiction：proposal/design/tasks/specs 之间自相矛盾
- scope-creep：Non-Goals 声明之外仍暗中扩大范围

请输出符合下方 Schema 的单一 JSON 对象，不得包含任何解释性文字。
"""


def spec_review_run(
    p: _paths.Paths,
    change_id: str,
    round_n: int,
    *,
    engine_name: str | None = None,
    config_path: Path | None = None,
    timeout_sec: int = 900,
    retries: int = 0,
    codex_bin: str | None = None,
    openspec_bin: str | None = None,
    portable_timeout: Path | None = None,
    gate_runner=subprocess.run,
    validate_runner=subprocess.run,
) -> dict:
    """跑完整一轮 spec review。失败与成功均返回 dict（不抛 SystemExit）。

    门顺序（成本递增）：``openspec validate --strict`` → ``[spec_review] gate_cmd``
    → LLM 引擎。前者失败 MUST NOT 进入后者（tasks 2.x）。
    """
    try:
        cfg = load_config(p.repo_root, override_path=config_path)
    except ConfigError as e:
        raise ValueError(str(e)) from e

    spec_review_cfg = cfg.spec_review
    if engine_name and engine_name not in ("codex", "claude"):
        raise ValueError(f"未知 spec_review engine：{engine_name!r}（仅支持 codex / claude）")
    selected_engine = (engine_name or spec_review_cfg.engine).lower()

    # 不变量 5/6 强制：路由校验必须校验实际将执行的 engine，而非配置文件原始值。
    # 若 CLI --engine 覆盖了 [spec_review].engine，需用覆盖后的值构造 effective config
    # 再喂给 check_routing，否则会出现"按旧 engine 通过校验、按新 engine 实际执行"的
    # 漏洞（与 pipeline.py:review_run 的既有处理同构）。
    if engine_name and engine_name.lower() != spec_review_cfg.engine.lower():
        effective_spec_review_cfg = dataclasses.replace(spec_review_cfg, engine=selected_engine)
        effective_cfg = dataclasses.replace(cfg, spec_review=effective_spec_review_cfg)
    else:
        effective_cfg = cfg
    violations = _spec_routing_violations(effective_cfg)
    if violations:
        return {"ok": False, "error": "spec_routing_violation", "violations": violations}

    base = _spec_base(p, change_id)
    base.mkdir(parents=True, exist_ok=True)
    change_dir = _change_dir(p.repo_root, change_id)

    review_path = base / f"round-{round_n}.spec-review.json"
    events_path = base / f"round-{round_n}.spec-review.events.jsonl"

    started_ms = _io.now_ms()

    def _duration_ms() -> int:
        return max(0, _io.now_ms() - started_ms)

    def _emit_failure(*, gate_failed: str | None, outcome_reason: str, engine: str | None = None) -> None:
        _telemetry.emit_spec_review_round(
            proj_key=p.proj_key,
            canonical_proj_key=p.canonical_proj_key,
            run_ts=p.run_ts,
            change_seq=None,
            change_id=change_id,
            round_n=round_n,
            base=base,
            ok=False,
            engine=engine,
            verdict=None,
            blocking_count=None,
            blocking_categories=None,
            duration_ms=_duration_ms(),
            retry_count=0,
            outcome_reason=outcome_reason,
            gate_failed=gate_failed,
            gate_skipped=False,
            gate_rule_hits=None,
            state_json=p.state_json,
            run_events=p.run_events,
        )

    # 门 1：openspec validate --strict
    try:
        osp = _find_openspec_bin(openspec_bin)
    except FileNotFoundError as e:
        _emit_failure(gate_failed="openspec_validate", outcome_reason="openspec_missing")
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "gate_failed": "openspec_validate",
            "gate_skipped": False,
            "error": "openspec_missing",
            "detail": str(e),
        }

    val = validate_runner(
        [osp, "validate", change_id, "--type", "change", "--strict"],
        cwd=str(p.repo_root),
        capture_output=True,
        text=True,
    )
    if val.returncode != 0:
        _emit_failure(gate_failed="openspec_validate", outcome_reason="openspec_validate_failed")
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "gate_failed": "openspec_validate",
            "gate_skipped": False,
            "detail": (val.stderr or val.stdout or "").strip()[-1000:],
        }

    # 门 2：[spec_review] gate_cmd（argv + shell=False；npc 不解读规则语义）
    gate_result = _run_gate_cmd(
        spec_review_cfg.gate_cmd, change_id, p.repo_root, runner=gate_runner
    )
    if not gate_result["skipped"] and not gate_result["ok"]:
        outcome = gate_result["error"] or "gate_cmd_failed"
        _telemetry.emit_spec_review_round(
            proj_key=p.proj_key,
            canonical_proj_key=p.canonical_proj_key,
            run_ts=p.run_ts,
            change_seq=None,
            change_id=change_id,
            round_n=round_n,
            base=base,
            ok=False,
            engine=None,
            verdict=None,
            blocking_count=None,
            blocking_categories=None,
            duration_ms=_duration_ms(),
            retry_count=0,
            outcome_reason=outcome,
            gate_failed="gate_cmd",
            gate_skipped=False,
            gate_rule_hits=gate_result["rule_hits"],
            state_json=p.state_json,
            run_events=p.run_events,
        )
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "gate_failed": "gate_cmd",
            "gate_skipped": False,
            "gate_error": outcome,
        }

    # 门 3：LLM 语义评审（贵）
    focus_text = _render_spec_review_focus(change_id, change_dir)
    schema_path = _spec_schema_path(p)
    ensure_schema(schema_path, SPEC_REVIEW_SCHEMA)

    try:
        pt = _portable_timeout_bin(portable_timeout)
    except FileNotFoundError as e:
        _emit_failure(gate_failed=None, outcome_reason="portable_timeout_missing", engine=selected_engine)
        return {"ok": False, "change": change_id, "round": round_n, "error": "dependency_missing", "detail": str(e)}

    try:
        if selected_engine == "codex":
            engine_bin = _find_codex_bin(codex_bin)
        else:
            engine_bin = _find_claude_bin(spec_review_cfg.claude_bin)
    except FileNotFoundError as e:
        _emit_failure(gate_failed=None, outcome_reason="dependency_missing", engine=selected_engine)
        return {"ok": False, "change": change_id, "round": round_n, "error": "dependency_missing", "detail": str(e)}

    attempts = retries + 1
    review_data: dict | None = None
    last_error: str | None = None
    for _attempt in range(attempts):
        if review_path.exists():
            review_path.unlink()
        if events_path.exists():
            events_path.unlink()
        rc = _spec_engine_exec(
            engine_name=selected_engine,
            repo_root=p.repo_root,
            schema_path=schema_path,
            focus_text=focus_text,
            review_out=review_path,
            events_out=events_path,
            timeout_sec=timeout_sec,
            engine_bin=engine_bin,
            portable_timeout=pt,
            model=spec_review_cfg.claude_model,
        )
        if rc == 0 and review_path.is_file():
            raw = review_path.read_text(encoding="utf-8")
            try:
                review_data = json.loads(raw)
                break
            except json.JSONDecodeError as e:
                last_error = f"invalid_json:{e}"
                review_data = None
        else:
            last_error = (
                f"exit_code={rc}" if rc != 0 else "spec_review_json_missing_after_engine_exit_0"
            )

    if review_data is None:
        error_code = f"{selected_engine}-exec-failed"
        _telemetry.emit_spec_review_round(
            proj_key=p.proj_key,
            canonical_proj_key=p.canonical_proj_key,
            run_ts=p.run_ts,
            change_seq=None,
            change_id=change_id,
            round_n=round_n,
            base=base,
            ok=False,
            engine=selected_engine,
            verdict=None,
            blocking_count=None,
            blocking_categories=None,
            duration_ms=_duration_ms(),
            retry_count=max(0, attempts - 1),
            outcome_reason=error_code,
            gate_failed=None,
            gate_skipped=gate_result["skipped"],
            gate_rule_hits=gate_result["rule_hits"],
            state_json=p.state_json,
            run_events=p.run_events,
        )
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "gate_failed": None,
            "gate_skipped": gate_result["skipped"],
            "error": error_code,
            "detail": last_error,
        }

    try:
        jsonschema.validate(review_data, SPEC_REVIEW_SCHEMA)
    except jsonschema.ValidationError as e:
        _telemetry.emit_spec_review_round(
            proj_key=p.proj_key,
            canonical_proj_key=p.canonical_proj_key,
            run_ts=p.run_ts,
            change_seq=None,
            change_id=change_id,
            round_n=round_n,
            base=base,
            ok=False,
            engine=selected_engine,
            verdict=None,
            blocking_count=None,
            blocking_categories=None,
            duration_ms=_duration_ms(),
            retry_count=max(0, attempts - 1),
            outcome_reason="invalid_spec_review_schema",
            gate_failed=None,
            gate_skipped=gate_result["skipped"],
            gate_rule_hits=gate_result["rule_hits"],
            state_json=p.state_json,
            run_events=p.run_events,
        )
        return {
            "ok": False,
            "change": change_id,
            "round": round_n,
            "error": "invalid_spec_review_schema",
            "detail": str(e),
        }

    metrics = parse_spec_review(review_data)
    duration_ms = _duration_ms()

    _telemetry.emit_spec_review_round(
        proj_key=p.proj_key,
        canonical_proj_key=p.canonical_proj_key,
        run_ts=p.run_ts,
        change_seq=None,
        change_id=change_id,
        round_n=round_n,
        base=base,
        ok=True,
        engine=selected_engine,
        verdict=metrics["verdict"],
        blocking_count=metrics["blocking"],
        blocking_categories=sorted(metrics["blocking_categories"]),
        duration_ms=duration_ms,
        retry_count=max(0, attempts - 1),
        outcome_reason=None,
        gate_failed=None,
        gate_skipped=gate_result["skipped"],
        gate_rule_hits=gate_result["rule_hits"],
        state_json=p.state_json,
        run_events=p.run_events,
    )

    return {
        "ok": True,
        "change": change_id,
        "round": round_n,
        "verdict": metrics["verdict"],
        "blocking": metrics["blocking"],
        "advisory": metrics["advisory"],
        "blocking_categories": sorted(metrics["blocking_categories"]),
        "gate_failed": None,
        "gate_skipped": gate_result["skipped"],
        "gate_rule_hits": gate_result["rule_hits"],
        "pointer": {"spec_review_json": str(review_path)},
        # F3 修复：`/spine-spec` 的 fix 循环上限判定需要一个确定性的、
        # 已经在被调用的命令里就能拿到的 [spec_review].max_rounds 真相源——
        # 而不是从 `npc verify routing`（只 emit 路由字段，从不含 max_rounds）
        # 猜一个默认值。此处把已加载的 config 值原样透传，round_n>=1 的每次
        # `npc spec review run` 调用都会带上它，供命令层直接读取。
        "max_rounds": spec_review_cfg.max_rounds,
    }


# ============================================================
# 固定轮次上限的 fix 循环（纯函数，tasks 4.x；D4：拒绝移植 stale 检测）
# ============================================================


def run_spec_fix_loop(review_fn, fix_fn, max_rounds: int) -> dict:
    """驱动「review → (blocking>0 且未达上限 → fix) → review → ...」的固定上限循环。

    ``review_fn(round_n) -> {"blocking": int, ...}``：跑一轮 review，返回其结果 dict。
    ``fix_fn(round_n) -> None``：跑一次 fix（渲染第 round_n 轮 fix 所需的一切；
    实际执行是 in-session subagent，本函数只负责驱动调用次数与终止条件）。

    语义：``max_rounds=N`` 表示「最多执行 N 次 fix」，review 轮次索引 0..N
    （共 N+1 次 review）。不复用 code review 的「blocking 单调下降代表收敛」判据
    ——spec 的 blocking 计数可以在改写后反弹，不代表卡死（D4）。

    round 5 F1 修复：``spec_review_run`` 的每一条 ``ok=False`` 返回路径（gate 失败、
    ``spec_routing_violation``、``dependency_missing``、``<engine>-exec-failed``、
    ``invalid_spec_review_schema`` 等）都**不带 ``blocking`` 键**——只有 ``ok=True``
    的成功评审结果才会写入 ``blocking``（见 spec_review_run 各 return 分支）。因此
    以「``blocking`` 键是否存在」而非「``.get("blocking", 0) == 0``」来判定评审是否
    真正跑完：前者对非门失败会显式落到 ``review-failed`` 分支并立刻终止循环（不再调用
    fix_fn，不推进 round_n），避免把「评审没跑成」误判为「评审跑完且 clean」。
    """
    fix_calls = 0
    review_results: list[dict] = []
    round_n = 0
    while True:
        result = review_fn(round_n)
        review_results.append(result)
        if "blocking" not in result:
            return {
                "status": "review-failed",
                "rounds": round_n + 1,
                "fix_calls": fix_calls,
                "review_results": review_results,
                "error": result.get("error"),
                "gate_failed": result.get("gate_failed"),
            }
        if result.get("blocking", 0) == 0:
            return {
                "status": "clean",
                "rounds": round_n + 1,
                "fix_calls": fix_calls,
                "review_results": review_results,
            }
        if fix_calls >= max_rounds:
            return {
                "status": "needs-user-decision",
                "rounds": round_n + 1,
                "fix_calls": fix_calls,
                "review_results": review_results,
            }
        fix_fn(round_n + 1)
        fix_calls += 1
        round_n += 1


# ============================================================
# CLI handlers
# ============================================================


def cli_spec_write_run(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = spec_write_run(
            p,
            args.change_id,
            config_path=Path(args.config) if getattr(args, "config", None) else None,
            goal=getattr(args, "goal", None),
            lessons_path=getattr(args, "lessons_path", None),
        )
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)


def cli_spec_write_record(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    result = spec_write_record(p, args.change_id, args.result)
    _emit_and_exit(result)


def cli_spec_interrogate_run(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = spec_interrogate_run(
            p,
            args.change_id,
            config_path=Path(args.config) if getattr(args, "config", None) else None,
            goal=getattr(args, "goal", None),
        )
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)


def cli_spec_interrogate_record(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    result = spec_interrogate_record(p, args.change_id, args.result)
    _emit_and_exit(result)


def cli_spec_interrogate_decide(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    result = spec_interrogate_decide(p, args.change_id, args.decisions_md)
    _emit_and_exit(result)


def cli_spec_fix_run(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = spec_fix_run(
            p,
            args.change_id,
            args.round_n,
            config_path=Path(args.config) if getattr(args, "config", None) else None,
        )
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)


def cli_spec_fix_record(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    result = spec_fix_record(p, args.change_id, args.round_n, args.result)
    _emit_and_exit(result)


def cli_spec_review_run(args: argparse.Namespace) -> None:
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    try:
        result = spec_review_run(
            p,
            args.change_id,
            args.round_n,
            engine_name=getattr(args, "engine", None),
            config_path=Path(args.config) if getattr(args, "config", None) else None,
            timeout_sec=getattr(args, "timeout", None) or 900,
            retries=getattr(args, "retries", None) or 0,
            codex_bin=getattr(args, "codex_bin", None),
        )
    except FileNotFoundError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return
    except EngineError as e:
        _io.emit_error("dependency_missing", str(e), exit_code=4)
        return
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    _emit_and_exit(result)
