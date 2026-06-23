"""Fixer prompt 片段抽取。

从 review.json 抽 in_scope=true 且 severity ∈ {critical, high} 的 findings，
渲染为 markdown 片段（每条 H2 段落），供主 session 拼进 Fixer prompt。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import _io
from .review import parse_review


def render_findings(blocking_findings: list[dict]) -> str:
    """渲染 blocking findings 为 markdown 片段。"""
    if not blocking_findings:
        return "（本轮无 in_scope blocking findings）\n"

    lines: list[str] = []
    for f in blocking_findings:
        fid = f.get("id", "?")
        sev = f.get("severity", "?")
        cat = f.get("category", "?")
        title = f.get("title", "")
        file = f.get("file", "-")
        line_range = f.get("line_range", "-")
        detail = f.get("detail", "")
        recommendation = f.get("recommendation", "")
        lines.append(f"## {fid} — [{sev}][{cat}] {title}")
        lines.append(f"File: {file}:{line_range}")
        lines.append(f"Detail: {detail}")
        lines.append(f"Recommendation: {recommendation}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def findings(args: argparse.Namespace) -> None:
    """fixer findings --review PATH --output-fragment PATH。"""
    review_path = Path(args.review)
    if not review_path.exists():
        _io.emit_error("file_not_found", f"review JSON 不存在：{review_path}", exit_code=3)
        return

    try:
        data = json.loads(review_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _io.emit_error("invalid_json", f"review JSON 解析失败：{e}", exit_code=1)
        return

    try:
        parsed = parse_review(data)
    except ValueError as e:
        _io.emit_error("invalid_schema", str(e), exit_code=1)
        return

    blocking_list = parsed["blocking_findings"]
    fragment = render_findings(blocking_list)

    output = Path(args.output_fragment)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(fragment, encoding="utf-8")

    _io.emit(
        {
            "ok": True,
            "output": str(output),
            "count": len(blocking_list),
            "categories": parsed["categories"],
        }
    )
