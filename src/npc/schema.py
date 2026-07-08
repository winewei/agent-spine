"""Codex review output schema 文件自举。

schema 跨项目共享，落 ~/task_log/.new-plan-review-schema.json，避免污染工程目录。
``ensure_schema`` 在磁盘内容与 ``REVIEW_SCHEMA`` 语义不一致时重写该文件，
使 ``REVIEW_SCHEMA`` 的代码修改能够真正传达给 codex（而非仅在文件缺失时生效）。
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
                    "spec_attribution",
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
                    "spec_attribution": {
                        "type": "string",
                        "enum": [
                            "spec-silent",
                            "spec-ambiguous",
                            "spec-contradicted",
                            "impl-deviation",
                        ],
                        "description": (
                            "spec-silent = spec 未规定该行为；"
                            "spec-ambiguous = spec 有规定但存在多种合理解读；"
                            "spec-contradicted = 实现与 spec 明文相悖；"
                            "impl-deviation = spec 明确无歧义，实现未照做。"
                        ),
                    },
                },
            },
        },
    },
}


SPEC_REVIEW_SCHEMA = {
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
                "changes-requested = 至少 1 个 blocking。"
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
                ],
                "properties": {
                    "id": {"type": "string", "description": "本轮唯一 id，建议格式 F1/F2..."},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                    },
                    "category": {
                        "type": "string",
                        "enum": [
                            "ambiguity",
                            "missing-scenario",
                            "implementation-leak",
                            "untestable",
                            "deferred-decision",
                            "contradiction",
                            "scope-creep",
                        ],
                    },
                    "title": {"type": "string", "maxLength": 80},
                    "file": {"type": "string", "description": "相对仓库根路径；通用问题可填 -"},
                    "line_range": {
                        "type": "string",
                        "description": "如 42-58 或单行 42；不适用时填 -",
                    },
                    "detail": {"type": "string"},
                    "recommendation": {"type": "string"},
                },
            },
        },
    },
}


def ensure_schema(schema_path: Path, schema: dict = REVIEW_SCHEMA) -> bool:
    """schema 文件内容与 ``schema``（默认 ``REVIEW_SCHEMA``）不一致时重写（含文件缺失时新建）。

    判定基于解析后的 JSON 对象语义相等，而非字节相等，避免缩进/键序差异
    导致无谓重写。解析失败（损坏的 JSON）视为不等，同样触发重写。

    ``schema`` 参数使本函数可被 ``SPEC_REVIEW_SCHEMA`` 等其它 schema 复用
    （见 change ``spine-spec-writer``），不必另写一份 write-once 逻辑。

    返回 True 表示发生了写入（新建或重写），False 表示内容已一致、未写入。
    """
    if schema_path.exists():
        try:
            existing = json.loads(schema_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = None
        if existing == schema:
            return False
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True
