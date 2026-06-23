"""输出与时间戳工具。

所有 npc 子命令通过 emit/emit_error 输出结果，保证 stdout 始终是单行 JSON，
stderr 用于人类可读的信息/警告。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone


def emit(data: dict) -> None:
    """输出单行 JSON 到 stdout（紧凑格式，不转义中文）。"""
    sys.stdout.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def emit_error(error_code: str, message: str, exit_code: int = 1) -> None:
    """输出错误 JSON 到 stdout 并退出。"""
    sys.stdout.write(
        json.dumps(
            {"ok": False, "error": error_code, "message": message},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()
    sys.exit(exit_code)


def info(msg: str) -> None:
    """信息消息到 stderr。"""
    sys.stderr.write(f"[npc] {msg}\n")


def warn(msg: str) -> None:
    """警告消息到 stderr。"""
    sys.stderr.write(f"[npc:warn] {msg}\n")


def now_iso() -> str:
    """当前时间的 ISO 8601 字符串，含本地时区偏移。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_ms() -> int:
    """当前时间的毫秒时间戳。"""
    return int(time.time() * 1000)


def now_utc_iso() -> str:
    """当前时间的 UTC ISO 8601 字符串（用于事件流，跨时区一致）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
