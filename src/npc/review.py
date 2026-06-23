"""Review JSON 解析：从 codex --output-schema 输出派生关键指标。

输入：codex review 写出的 round-N.review.json（已通过 schema 校验）
输出：JSON 单行，含 verdict / blocking / advisory / categories / blocking_findings
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import _io


BLOCKING_SEVERITIES = {"critical", "high"}


def parse_review(review_json: dict) -> dict:
    """从 review JSON 派生指标。纯函数。"""
    findings = review_json.get("findings") or []
    if not isinstance(findings, list):
        raise ValueError("review.findings 必须是数组")

    blocking_list: list[dict] = []
    advisory_count = 0
    categories: list[str] = []
    seen_cats: set[str] = set()

    for f in findings:
        sev = f.get("severity")
        in_scope = bool(f.get("in_scope"))
        cat = f.get("category")
        if cat and cat not in seen_cats:
            seen_cats.add(cat)
            categories.append(cat)
        if sev in BLOCKING_SEVERITIES and in_scope:
            blocking_list.append(f)
        else:
            advisory_count += 1

    blocking_list.sort(key=lambda x: x.get("id", ""))
    return {
        "verdict": review_json.get("verdict"),
        "blocking": len(blocking_list),
        "advisory": advisory_count,
        "categories": categories,
        "blocking_findings": blocking_list,
    }


def parse(args: argparse.Namespace) -> None:
    """review parse <review.json>。"""
    path = Path(args.review_json)
    if not path.exists():
        _io.emit_error("file_not_found", f"review JSON 不存在：{path}", exit_code=3)
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _io.emit_error("invalid_json", f"review JSON 解析失败：{e}", exit_code=1)
        return

    try:
        result = parse_review(data)
    except ValueError as e:
        _io.emit_error("invalid_schema", str(e), exit_code=1)
        return

    _io.emit(result)
