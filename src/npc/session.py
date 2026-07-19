"""宿主 session_id 识别。

按以下顺序尝试：
1. mtime 启发：宿主 session 目录（Claude Code：~/.claude/projects/<PROJ_KEY>/）下
   最近 1 分钟内被触碰的 .jsonl（调用 npc 时宿主已 append 当前 session 的
   transcript，mtime 最新）。宿主无 session 目录（generic）→ 跳过。
2. by-cwd hook 索引最后一行 + transcript 存在性 + mtime < 6h（宿主中立：任何
   CLI 的 SessionStart hook 都可往 ~/task_log/.session-cache/by-cwd/ 追加）
3. 都不行 → ('-', '-', 'unknown')

设计为纯函数（除 mtime/文件系统访问外不写状态），由 init 调用后注入环境变量。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import hosts as _hosts


SOURCE_MTIME = "mtime-1min"
SOURCE_HOOK = "hook-by-cwd-tail"
SOURCE_UNKNOWN = "unknown"


def detect_via_mtime(
    proj_key: str,
    home: Path,
    *,
    window_seconds: int = 60,
    host: _hosts.ResolvedHost | None = None,
) -> tuple[str, str] | None:
    """路径 A：扫宿主 session 目录找最近 mtime jsonl。返回 (session_id, transcript_path) 或 None。

    ``host`` 未传时按 claude 宿主目录布局（向后兼容既有调用方）。
    """
    h = host or _hosts.resolve_host(_hosts.HOST_CLAUDE)
    cc_dir = h.session_dir(home, proj_key)
    if cc_dir is None or not cc_dir.is_dir():
        return None
    now = time.time()
    candidates: list[tuple[float, Path]] = []
    for f in cc_dir.glob("*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if now - mtime <= window_seconds:
            candidates.append((mtime, f))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    latest = candidates[0][1]
    return latest.stem, str(latest)


def detect_via_hook(proj_key: str, home: Path, *, max_age_seconds: int = 6 * 3600) -> tuple[str, str] | None:
    """路径 B：读 by-cwd hook 索引最后一行。返回 (session_id, transcript_path) 或 None。"""
    by_cwd = home / "task_log" / ".session-cache" / "by-cwd" / f"{proj_key}.jsonl"
    if not by_cwd.exists():
        return None
    try:
        # 仅读最后非空行（文件可能很大但通常 < 1MB，全读可接受）
        last_line = ""
        with by_cwd.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    last_line = line
        if not last_line:
            return None
        rec = json.loads(last_line)
    except (OSError, json.JSONDecodeError):
        return None

    sid = rec.get("session_id")
    tx = rec.get("transcript_path")
    if not sid or not tx:
        return None
    tx_path = Path(tx)
    if not tx_path.exists():
        return None
    try:
        if time.time() - tx_path.stat().st_mtime > max_age_seconds:
            return None
    except OSError:
        return None
    return sid, str(tx_path)


def detect_session(
    proj_key: str,
    home: Path | None = None,
    *,
    host: _hosts.ResolvedHost | None = None,
) -> tuple[str, str, str]:
    """返回 (session_id, transcript_path, source)；找不到返回 ('-', '-', 'unknown')。

    ``host`` 未传时按 claude 宿主（向后兼容）；generic 宿主自动跳过 mtime 启发。
    """
    h = home or Path.home()
    res = detect_via_mtime(proj_key, h, host=host)
    if res:
        return res[0], res[1], SOURCE_MTIME
    res = detect_via_hook(proj_key, h)
    if res:
        return res[0], res[1], SOURCE_HOOK
    return "-", "-", SOURCE_UNKNOWN
