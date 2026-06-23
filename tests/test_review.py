"""review 模块测试。"""

from __future__ import annotations

import json

import pytest

from npc import review as _review


SAMPLE_REVIEW = {
    "verdict": "changes-requested",
    "findings": [
        {
            "id": "F1",
            "severity": "critical",
            "category": "validation",
            "title": "缺长度校验",
            "file": "src/a.go",
            "line_range": "42-58",
            "detail": "...",
            "recommendation": "...",
            "in_scope": True,
        },
        {
            "id": "F2",
            "severity": "high",
            "category": "concurrency",
            "title": "锁未释放",
            "file": "src/b.go",
            "line_range": "10",
            "detail": "...",
            "recommendation": "...",
            "in_scope": True,
        },
        {
            "id": "F3",
            "severity": "medium",
            "category": "style",
            "title": "命名",
            "file": "src/c.go",
            "line_range": "1",
            "detail": "...",
            "recommendation": "...",
            "in_scope": True,
        },
        {
            "id": "F4",
            "severity": "critical",
            "category": "performance",
            "title": "全局问题",
            "file": "src/d.go",
            "line_range": "100",
            "detail": "...",
            "recommendation": "...",
            "in_scope": False,
        },
    ],
}


def test_parse_review_counts():
    out = _review.parse_review(SAMPLE_REVIEW)
    assert out["blocking"] == 2  # F1, F2
    assert out["advisory"] == 2  # F3 (low severity), F4 (out of scope)
    assert out["verdict"] == "changes-requested"


def test_parse_review_categories_ordered_unique():
    out = _review.parse_review(SAMPLE_REVIEW)
    assert out["categories"] == ["validation", "concurrency", "style", "performance"]


def test_parse_review_blocking_findings_sorted_by_id():
    out = _review.parse_review(SAMPLE_REVIEW)
    ids = [f["id"] for f in out["blocking_findings"]]
    assert ids == ["F1", "F2"]


def test_parse_review_empty_findings():
    out = _review.parse_review({"verdict": "approve", "findings": []})
    assert out["blocking"] == 0
    assert out["advisory"] == 0
    assert out["categories"] == []
    assert out["blocking_findings"] == []


def test_parse_review_invalid_findings_type():
    with pytest.raises(ValueError):
        _review.parse_review({"verdict": "?", "findings": "not-a-list"})


def test_parse_cli_handler(tmp_path, capsys, make_args):
    review_file = tmp_path / "r.json"
    review_file.write_text(json.dumps(SAMPLE_REVIEW))
    _review.parse(make_args(review_json=str(review_file)))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["blocking"] == 2
    assert payload["advisory"] == 2


def test_parse_cli_missing_file(tmp_path, capsys, make_args):
    with pytest.raises(SystemExit):
        _review.parse(make_args(review_json=str(tmp_path / "absent.json")))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "file_not_found"
