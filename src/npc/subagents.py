"""宿主 spawn 的 sub-agent 存活观测（只读，零上报契约）。

``npc task`` 契约要求任务方显式上报心跳；宿主 spawn 的 implementer（Claude Code
``Agent`` / Codex ``spawn_agent``）不会主动调 npc，但宿主本身会为每个 sub-agent
维护一份转录文件，每次模型回合与工具调用都 append 一行。本模块把这份文件当作
心跳源：

- **只读三样东西**：首行/伴随元数据（agent 类型、描述、worktree）、文件 mtime、
  末行时间戳与终止标记。不解析正文，不依赖宿主内部 schema 稳定。
- **布局按宿主分派**（:mod:`npc.hosts` ``subagent_layout``）：

  * ``claude``：``<session_dir>/<session_id>/subagents/agent-<id>.jsonl`` +
    同名 ``.meta.json``（agentType / description / name / worktreePath）。
  * ``codex``：``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``，首行
    ``session_meta`` 带 ``thread_source="subagent"`` / ``parent_thread_id`` / ``cwd``。

- **归属过滤**：只保留 worktree 或 cwd 位于当前 repo_root 之下、且 mtime 在
  ``max_age_seconds`` 内的转录，避免把历史 run 或别的项目扫进来。
- **状态派生**：末行带终止标记 → ``finished``；否则按空闲秒数与 ``stale_seconds``
  判 ``running`` / ``stale``。空闲秒数取 mtime 与末行时间戳中较新者。

派生结果只暴露每 agent 一行摘要，供 ``npc watch``（--once / --follow）与
playbook 恢复闸门消费；主 session 不读转录本身。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import hosts as _hosts

DEFAULT_STALE_SECONDS = 900
DEFAULT_ABANDONED_SECONDS = 4 * 3600
DEFAULT_MAX_AGE_SECONDS = 12 * 3600
_TAIL_BYTES = 64 * 1024
_TAIL_LINES = 40
SESSION_ID_ENV = "NPC_SESSION_ID"

STATUS_RUNNING = "running"
STATUS_STALE = "stale"
STATUS_ABANDONED = "abandoned"
STATUS_FINISHED = "finished"
STATUS_UNKNOWN = "unknown"
LIVE_STATUSES = frozenset({STATUS_RUNNING, STATUS_STALE, STATUS_UNKNOWN})


# ------------------------------------------------------------------
# 通用工具
# ------------------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _read_first_json_line(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    return json.loads(line)
                return None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _read_tail_json_lines(path: Path, limit: int = _TAIL_LINES) -> list[dict]:
    """读文件末尾一段字节，返回最后 ``limit`` 条可解析的 JSON 行（新→旧）。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - _TAIL_BYTES))
            chunk = fh.read()
    except OSError:
        return []
    out: list[dict] = []
    for raw in reversed(chunk.splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw.decode("utf-8", errors="replace")))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out


def _under(path_str: str | None, root: Path) -> bool:
    if not path_str:
        return False
    try:
        Path(path_str).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _mtime_dt(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except OSError:
        return None


def match_change_id(text_fields: list[str | None], change_ids: list[str]) -> str | None:
    """从描述/名称/分支名里匹配已知 change id（最长匹配优先，避免前缀误配）。"""
    haystack = " ".join(t for t in text_fields if isinstance(t, str) and t)
    if not haystack:
        return None
    for cid in sorted(change_ids, key=len, reverse=True):
        if cid and cid in haystack:
            return cid
    return None


def _finalize(
    row: dict,
    *,
    transcript: Path,
    last_line: dict | None,
    finished: bool,
    now: datetime,
    stale_seconds: int,
    abandoned_seconds: int,
) -> dict:
    mtime = _mtime_dt(transcript)
    last_ts = _parse_iso((last_line or {}).get("timestamp"))
    candidates = [d for d in (mtime, last_ts) if d is not None]
    last_activity = max(candidates) if candidates else None
    idle = int(max(0.0, (now - last_activity).total_seconds())) if last_activity else None

    if finished:
        status = STATUS_FINISHED
    elif idle is None:
        status = STATUS_UNKNOWN
    elif idle > abandoned_seconds:
        status = STATUS_ABANDONED
    elif idle > stale_seconds:
        status = STATUS_STALE
    else:
        status = STATUS_RUNNING

    row["transcript"] = str(transcript)
    row["last_activity_at"] = last_activity.isoformat() if last_activity else None
    row["idle_seconds"] = idle
    row["stale_seconds"] = stale_seconds
    row["observed_status"] = status
    return row


# ------------------------------------------------------------------
# Claude Code 布局
# ------------------------------------------------------------------


def _claude_tool_of(line: dict | None) -> str | None:
    content = ((line or {}).get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_use":
            return part.get("name")
    return None


def _claude_last_tool(tail: list[dict]) -> str | None:
    """最近一次 tool_use 的工具名（末行常是 tool_result，需往回找）。"""
    for line in tail:
        if line.get("type") == "assistant":
            name = _claude_tool_of(line)
            if name:
                return name
    return None


_CLAUDE_END_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})


def _claude_finished(last_line: dict | None) -> bool:
    """末行是 assistant 且 stop_reason 为回合终止 → agent 已交付。

    同一条模型消息会分多行落盘（thinking / text / tool_use 各一行），中间行的
    stop_reason 为 None，不能据"没有 tool_use"判完成。agent 交付后若被主 session
    再次唤醒，状态会从 finished 回到 running（follow 流打 RESUMED）。
    """
    if not last_line or last_line.get("type") != "assistant":
        return False
    msg = last_line.get("message") or {}
    return msg.get("stop_reason") in _CLAUDE_END_STOP_REASONS


def scan_claude(
    root: Path,
    *,
    repo_root: Path,
    change_ids: list[str],
    now: datetime,
    stale_seconds: int,
    abandoned_seconds: int,
    max_age_seconds: int,
    session_id: str | None = None,
    worktree_roots: list[Path] | None = None,
) -> list[dict]:
    """扫 ``<root>/<session>/subagents/agent-*.meta.json``。``session_id`` 给定时只看该 session。"""
    if not root.is_dir():
        return []
    cutoff = now - timedelta(seconds=max_age_seconds)
    rows: list[dict] = []
    session_dirs = [root / session_id] if session_id else [d for d in root.iterdir() if d.is_dir()]
    for sdir in session_dirs:
        sub = sdir / "subagents"
        if not sub.is_dir():
            continue
        for meta_path in sub.glob("agent-*.meta.json"):
            transcript = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".jsonl")
            if not transcript.is_file():
                continue
            mtime = _mtime_dt(transcript)
            if mtime is None or mtime < cutoff:
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
            tail = _read_tail_json_lines(transcript)
            last_line = tail[0] if tail else None
            worktree = meta.get("worktreePath")
            cwd = (last_line or {}).get("cwd")
            scopes = [repo_root, *(worktree_roots or [])]
            if not any(_under(worktree, scope) or _under(cwd, scope) for scope in scopes):
                continue
            agent_id = meta_path.name[len("agent-") : -len(".meta.json")]
            row = {
                "agent_id": agent_id,
                "host": _hosts.HOST_CLAUDE,
                "session_id": sdir.name,
                "agent_type": meta.get("agentType"),
                "name": meta.get("name"),
                "description": meta.get("description"),
                "worktree": worktree,
                "change_id": match_change_id(
                    [meta.get("description"), meta.get("name"), meta.get("worktreeBranch")],
                    change_ids,
                ),
                "last_tool": _claude_last_tool(tail),
            }
            rows.append(
                _finalize(
                    row,
                    transcript=transcript,
                    last_line=last_line,
                    finished=_claude_finished(last_line),
                    now=now,
                    stale_seconds=stale_seconds,
                    abandoned_seconds=abandoned_seconds,
                )
            )
    return rows


# ------------------------------------------------------------------
# Codex 布局
# ------------------------------------------------------------------


def _codex_is_subagent(meta: dict) -> bool:
    payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    if payload.get("thread_source") == "subagent" or payload.get("parent_thread_id"):
        return True
    src = payload.get("source")
    return isinstance(src, dict) and "subagent" in src


def _codex_finished(last_line: dict | None) -> bool:
    if not last_line or last_line.get("type") != "event_msg":
        return False
    payload = last_line.get("payload") or {}
    return payload.get("type") in {"task_complete", "shutdown_complete", "turn_aborted"}


def _codex_last_tool(tail: list[dict]) -> str | None:
    """最近一次 function_call 的工具名；没有则回退到最近事件类型。"""
    for line in tail:
        payload = line.get("payload") or {}
        if line.get("type") == "response_item" and payload.get("type") == "function_call":
            return payload.get("name")
    for line in tail:
        if line.get("type") == "event_msg":
            return (line.get("payload") or {}).get("type")
    return None


def _codex_day_dirs(root: Path, *, now: datetime, max_age_seconds: int) -> list[Path]:
    """只扫 ``max_age_seconds`` 覆盖到的日期目录（多留一天容错时区）。"""
    days = max_age_seconds // 86400 + 2
    out: list[Path] = []
    for i in range(days):
        d = (now - timedelta(days=i))
        p = root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
        if p.is_dir():
            out.append(p)
    return out


def scan_codex(
    root: Path,
    *,
    repo_root: Path,
    change_ids: list[str],
    now: datetime,
    stale_seconds: int,
    abandoned_seconds: int,
    max_age_seconds: int,
    session_id: str | None = None,
    worktree_roots: list[Path] | None = None,
) -> list[dict]:
    """扫 ``<root>/YYYY/MM/DD/rollout-*.jsonl``，只留 subagent 且 cwd 在 repo 下的。"""
    if not root.is_dir():
        return []
    cutoff = now - timedelta(seconds=max_age_seconds)
    rows: list[dict] = []
    for day in _codex_day_dirs(root, now=now, max_age_seconds=max_age_seconds):
        for transcript in day.glob("rollout-*.jsonl"):
            mtime = _mtime_dt(transcript)
            if mtime is None or mtime < cutoff:
                continue
            meta = _read_first_json_line(transcript)
            if not meta or meta.get("type") != "session_meta" or not _codex_is_subagent(meta):
                continue
            payload = meta.get("payload") or {}
            if session_id and payload.get("parent_thread_id") not in (None, session_id):
                continue
            cwd = payload.get("cwd")
            if not any(_under(cwd, scope) for scope in [repo_root, *(worktree_roots or [])]):
                continue
            tail = _read_tail_json_lines(transcript)
            last_line = tail[0] if tail else None
            agent_id = payload.get("id") or payload.get("session_id") or transcript.stem
            row = {
                "agent_id": agent_id,
                "host": _hosts.HOST_CODEX,
                "session_id": payload.get("parent_thread_id"),
                "agent_type": payload.get("agent_type") or payload.get("agent_role"),
                "name": payload.get("agent_nickname") or payload.get("name"),
                "description": None,
                "worktree": cwd,
                "change_id": match_change_id([cwd, payload.get("agent_nickname")], change_ids),
                "last_tool": _codex_last_tool(tail),
            }
            rows.append(
                _finalize(
                    row,
                    transcript=transcript,
                    last_line=last_line,
                    finished=_codex_finished(last_line),
                    now=now,
                    stale_seconds=stale_seconds,
                    abandoned_seconds=abandoned_seconds,
                )
            )
    return rows


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------


def scan(
    host: _hosts.ResolvedHost,
    *,
    home: Path,
    proj_key: str,
    repo_root: Path,
    change_ids: list[str],
    now: datetime | None = None,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    abandoned_seconds: int = DEFAULT_ABANDONED_SECONDS,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    session_id: str | None = None,
    worktree_roots: list[Path] | None = None,
) -> list[dict]:
    """按宿主布局扫描并派生存活状态；宿主无布局返回空列表。

    ``session_id`` 未传时取 ``NPC_SESSION_ID`` 环境变量（``npc init`` 注入的当前主
    session）；两者都没有则不按 session 过滤，靠 repo_root 归属与 max_age 收敛。
    """
    root = host.subagent_root(home, proj_key)
    if root is None:
        return []
    ts = now or datetime.now().astimezone()
    sid = session_id or os.environ.get(SESSION_ID_ENV) or None
    if sid == "-":
        sid = None
    kwargs = dict(
        repo_root=repo_root,
        change_ids=change_ids,
        now=ts,
        stale_seconds=stale_seconds,
        abandoned_seconds=abandoned_seconds,
        max_age_seconds=max_age_seconds,
        session_id=sid,
        worktree_roots=worktree_roots,
    )
    if host.subagent_layout == _hosts.LAYOUT_CLAUDE:
        rows = scan_claude(root, **kwargs)
    elif host.subagent_layout == _hosts.LAYOUT_CODEX:
        rows = scan_codex(root, **kwargs)
    else:
        return []
    order = {STATUS_STALE: 0, STATUS_RUNNING: 1, STATUS_UNKNOWN: 2, STATUS_ABANDONED: 3, STATUS_FINISHED: 4}
    rows.sort(key=lambda r: (order.get(r["observed_status"], 9), r.get("change_id") or "", r["agent_id"]))
    return rows


def summarize(rows: list[dict]) -> dict:
    """派生计数，供 status/summary 类输出使用。"""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["observed_status"]] = counts.get(r["observed_status"], 0) + 1
    return {"total": len(rows), "by_status": counts}
