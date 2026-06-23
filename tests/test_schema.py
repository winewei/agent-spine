"""schema 模块测试。"""

from __future__ import annotations

import json

from npc import schema as _schema


def test_ensure_schema_creates_when_missing(tmp_path):
    target = tmp_path / "schema.json"
    created = _schema.ensure_schema(target)
    assert created is True
    assert target.exists()
    data = json.loads(target.read_text())
    assert "findings" in data["properties"]
    # 关键 enum 存在
    assert "approve" in data["properties"]["verdict"]["enum"]
    assert "critical" in data["properties"]["findings"]["items"]["properties"]["severity"]["enum"]


def test_ensure_schema_idempotent(tmp_path):
    target = tmp_path / "schema.json"
    _schema.ensure_schema(target)
    target.write_text("MODIFIED")  # 模拟用户改过
    created = _schema.ensure_schema(target)
    assert created is False
    assert target.read_text() == "MODIFIED"  # 不覆盖


def test_review_schema_structure_is_strict():
    s = _schema.REVIEW_SCHEMA
    finding = s["properties"]["findings"]["items"]
    assert finding["additionalProperties"] is False
    for key in (
        "id",
        "severity",
        "category",
        "title",
        "file",
        "line_range",
        "detail",
        "recommendation",
        "in_scope",
    ):
        assert key in finding["required"]
