"""Codex review output schema 文件自举。

schema 跨项目共享，落 ~/task_log/.new-plan-review-schema.json，避免污染工程目录。
schema 内容稳定，仅在文件缺失时写入。
"""

from __future__ import annotations

import json
from pathlib import Path


REVIEW_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approve", "passed-with-advisory", "changes-requested"],
            "description": (
                "approve = 无 blocking 且无 advisory；"
                "passed-with-advisory = 无 blocking 但有 advisory；"
                "changes-requested = 至少 1 个 in_scope blocking。"
            ),
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "severity",
                    "category",
                    "title",
                    "file",
                    "line_range",
                    "detail",
                    "recommendation",
                    "in_scope",
                ],
                "properties": {
                    "id": {"type": "string", "description": "本轮唯一 id，建议格式 F1/F2..."},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "validation/error-handling/test-coverage/edge-case/type-safety/"
                            "performance/security/style/concurrency/transaction/locking/retry/"
                            "race-condition/partial-failure 中选一个，必要时可新增"
                        ),
                    },
                    "title": {"type": "string", "maxLength": 80},
                    "file": {"type": "string", "description": "相对仓库根路径；通用问题可填 -"},
                    "line_range": {
                        "type": "string",
                        "description": "如 42-58 或单行 42；不适用时填 -",
                    },
                    "detail": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "in_scope": {
                        "type": "boolean",
                        "description": (
                            "true = 与本次 change diff 直接相关；"
                            "false = diff 之外的既有问题或越界建议（不计入 blocking）"
                        ),
                    },
                },
            },
        },
    },
}


def ensure_schema(schema_path: Path) -> bool:
    """schema 文件不存在时写入。返回 True 表示新建。"""
    if schema_path.exists():
        return False
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(REVIEW_SCHEMA, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True
