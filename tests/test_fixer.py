"""fixer 模块测试。"""

from __future__ import annotations

import json

import pytest

from npc import fixer as _fixer


def test_render_findings_empty():
    out = _fixer.render_findings([])
    assert "无 in_scope blocking" in out


def test_render_findings_full():
    findings = [
        {
            "id": "F1",
            "severity": "critical",
            "category": "validation",
            "title": "缺校验",
            "file": "src/a.go",
            "line_range": "42-58",
            "detail": "未检查 username 长度",
            "recommendation": "len(username) > 256 时报错",
            "in_scope": True,
        },
        {
            "id": "F2",
            "severity": "high",
            "category": "concurrency",
            "title": "锁未释放",
            "file": "src/b.go",
            "line_range": "10",
            "detail": "panic 路径下未解锁",
            "recommendation": "用 defer Unlock",
            "in_scope": True,
        },
    ]
    out = _fixer.render_findings(findings)
    assert "## F1 — [critical][validation] 缺校验" in out
    assert "File: src/a.go:42-58" in out
    assert "Detail: 未检查" in out
    assert "## F2 — [high][concurrency]" in out


def test_findings_handler(tmp_path, capsys, make_args):
    review = tmp_path / "review.json"
    review.write_text(
        json.dumps(
            {
                "verdict": "changes-requested",
                "findings": [
                    {
                        "id": "F1",
                        "severity": "critical",
                        "category": "validation",
                        "title": "x",
                        "file": "a.go",
                        "line_range": "1",
                        "detail": "d",
                        "recommendation": "r",
                        "in_scope": True,
                    },
                    {
                        "id": "F2",
                        "severity": "medium",
                        "category": "style",
                        "title": "skip",
                        "file": "b.go",
                        "line_range": "1",
                        "detail": "d",
                        "recommendation": "r",
                        "in_scope": True,
                    },
                ],
            }
        )
    )
    output = tmp_path / "frag.md"
    _fixer.findings(make_args(review=str(review), output_fragment=str(output)))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["count"] == 1
    assert "validation" in payload["categories"]
    content = output.read_text()
    assert "F1" in content and "F2" not in content


def test_findings_handler_missing_review(tmp_path, capsys, make_args):
    out = tmp_path / "f.md"
    with pytest.raises(SystemExit):
        _fixer.findings(make_args(review=str(tmp_path / "x.json"), output_fragment=str(out)))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "file_not_found"
