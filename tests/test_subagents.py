"""宿主 sub-agent 转录存活观测（npc.subagents）测试。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from npc import hosts, subagents


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat()


def _touch(path: Path, when: datetime) -> None:
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _claude_agent(
    session_dir: Path,
    agent_id: str,
    *,
    description: str,
    worktree: Path,
    last_lines: list[dict],
    mtime: datetime,
) -> Path:
    sub = session_dir / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / f"agent-{agent_id}.meta.json").write_text(
        json.dumps(
            {
                "agentType": "spine-coder",
                "description": description,
                "name": f"impl-{agent_id}",
                "worktreePath": str(worktree),
                "worktreeBranch": f"worktree-agent-{agent_id}",
            }
        ),
        encoding="utf-8",
    )
    transcript = sub / f"agent-{agent_id}.jsonl"
    transcript.write_text("\n".join(json.dumps(line) for line in last_lines) + "\n", encoding="utf-8")
    _touch(transcript, mtime)
    return transcript


def _assistant(ts: datetime, *, stop_reason: str | None, tool: str | None = None, cwd: str = "/tmp/x") -> dict:
    content = [{"type": "tool_use", "name": tool, "input": {}}] if tool else [{"type": "text", "text": "done"}]
    return {
        "type": "assistant",
        "timestamp": _iso(ts),
        "cwd": cwd,
        "message": {"role": "assistant", "stop_reason": stop_reason, "content": content},
    }


def _tool_result(ts: datetime, cwd: str = "/tmp/x") -> dict:
    return {"type": "user", "timestamp": _iso(ts), "cwd": cwd, "message": {"role": "user", "content": [{"type": "tool_result"}]}}


def _claude_host(home: Path) -> hosts.ResolvedHost:
    return hosts.resolve_host(env={"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": str(home / ".claude")})


def test_claude_running_stale_finished_and_repo_filter(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    proj_key = "-repo"
    sdir = home / ".claude" / "projects" / proj_key / "sess-1"
    now = datetime.now().astimezone()
    wt = repo / ".claude" / "worktrees" / "agent-a1"
    wt.mkdir(parents=True)

    # running: 末行 tool_result，30s 前；上一条 assistant 是 Bash tool_use
    _claude_agent(
        sdir, "a1", description="Implement add-hive-worker", worktree=wt,
        last_lines=[_assistant(now - timedelta(seconds=40), stop_reason="tool_use", tool="Bash"),
                    _tool_result(now - timedelta(seconds=30))],
        mtime=now - timedelta(seconds=30),
    )
    # stale: 20 分钟没动，末行 thinking 行 stop_reason None
    _claude_agent(
        sdir, "a2", description="Implement add-web-pages", worktree=wt,
        last_lines=[_assistant(now - timedelta(minutes=20), stop_reason=None)],
        mtime=now - timedelta(minutes=20),
    )
    # finished: end_turn
    _claude_agent(
        sdir, "a3", description="Implement refactor-lib", worktree=wt,
        last_lines=[_assistant(now - timedelta(minutes=5), stop_reason="end_turn")],
        mtime=now - timedelta(minutes=5),
    )
    # abandoned: 5 小时
    _claude_agent(
        sdir, "a4", description="Implement old-thing", worktree=wt,
        last_lines=[_assistant(now - timedelta(hours=5), stop_reason="tool_use", tool="Read")],
        mtime=now - timedelta(hours=5),
    )
    # 别的项目：worktree 与 cwd 都不在 repo 下 → 过滤
    _claude_agent(
        sdir, "a5", description="Implement add-hive-worker", worktree=other / "wt",
        last_lines=[_tool_result(now, cwd=str(other))],
        mtime=now,
    )
    # 超过 max_age：不扫
    _claude_agent(
        sdir, "a6", description="Implement ancient", worktree=wt,
        last_lines=[_assistant(now - timedelta(days=3), stop_reason="tool_use", tool="Read")],
        mtime=now - timedelta(days=3),
    )

    rows = subagents.scan(
        _claude_host(home), home=home, proj_key=proj_key, repo_root=repo,
        change_ids=["add-hive-worker", "add-web-pages", "refactor-lib"], now=now,
    )
    by_id = {r["agent_id"]: r for r in rows}
    assert set(by_id) == {"a1", "a2", "a3", "a4"}
    assert by_id["a1"]["observed_status"] == "running"
    assert by_id["a1"]["last_tool"] == "Bash"
    assert by_id["a1"]["change_id"] == "add-hive-worker"
    assert by_id["a1"]["host"] == "claude"
    assert by_id["a1"]["session_id"] == "sess-1"
    assert by_id["a2"]["observed_status"] == "stale"
    assert by_id["a3"]["observed_status"] == "finished"
    assert by_id["a4"]["observed_status"] == "abandoned"
    assert by_id["a4"]["change_id"] is None
    # 排序：stale 在前，finished 在后
    assert [r["agent_id"] for r in rows] == ["a2", "a1", "a4", "a3"]
    assert subagents.summarize(rows) == {
        "total": 4,
        "by_status": {"stale": 1, "running": 1, "abandoned": 1, "finished": 1},
    }


def test_claude_session_filter_env_and_arg(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    now = datetime.now().astimezone()
    root = home / ".claude" / "projects" / "-repo"
    for sess in ("s1", "s2"):
        _claude_agent(
            root / sess, f"agent-{sess}", description="Implement x", worktree=repo / "wt",
            last_lines=[_tool_result(now, cwd=str(repo))], mtime=now,
        )
    host = _claude_host(home)
    kw = dict(home=home, proj_key="-repo", repo_root=repo, change_ids=["x"], now=now)
    assert {r["session_id"] for r in subagents.scan(host, **kw)} == {"s1", "s2"}
    assert {r["session_id"] for r in subagents.scan(host, session_id="s2", **kw)} == {"s2"}
    monkeypatch.setenv(subagents.SESSION_ID_ENV, "s1")
    assert {r["session_id"] for r in subagents.scan(host, **kw)} == {"s1"}
    monkeypatch.setenv(subagents.SESSION_ID_ENV, "-")
    assert {r["session_id"] for r in subagents.scan(host, **kw)} == {"s1", "s2"}


def _codex_rollout(day_dir: Path, thread_id: str, *, cwd: Path, subagent: bool, lines: list[dict], mtime: datetime) -> Path:
    day_dir.mkdir(parents=True, exist_ok=True)
    payload = {"id": thread_id, "cwd": str(cwd), "cli_version": "0.155.1"}
    if subagent:
        payload.update({"thread_source": "subagent", "parent_thread_id": "parent-1", "agent_nickname": "worker"})
    meta = {"type": "session_meta", "timestamp": _iso(mtime - timedelta(minutes=10)), "payload": payload}
    path = day_dir / f"rollout-{thread_id}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in [meta, *lines]) + "\n", encoding="utf-8")
    _touch(path, mtime)
    return path


def test_codex_layout(tmp_path: Path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    now = datetime.now().astimezone()
    day = home / ".codex" / "sessions" / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
    call = {"type": "response_item", "timestamp": _iso(now - timedelta(seconds=20)), "payload": {"type": "function_call", "name": "shell"}}
    done = {"type": "event_msg", "timestamp": _iso(now - timedelta(minutes=3)), "payload": {"type": "task_complete"}}
    _codex_rollout(day, "t-running", cwd=repo / "wt-add-hive-worker", subagent=True, lines=[call], mtime=now - timedelta(seconds=20))
    _codex_rollout(day, "t-done", cwd=repo, subagent=True, lines=[call, done], mtime=now - timedelta(minutes=3))
    _codex_rollout(day, "t-main", cwd=repo, subagent=False, lines=[call], mtime=now)  # 主线程：不是 subagent
    _codex_rollout(day, "t-elsewhere", cwd=tmp_path / "other", subagent=True, lines=[call], mtime=now)

    host = hosts.resolve_host("codex", env={})
    assert host.subagent_layout == hosts.LAYOUT_CODEX
    rows = subagents.scan(host, home=home, proj_key="-repo", repo_root=repo, change_ids=["add-hive-worker"], now=now)
    by_id = {r["agent_id"]: r for r in rows}
    assert set(by_id) == {"t-running", "t-done"}
    assert by_id["t-running"]["observed_status"] == "running"
    assert by_id["t-running"]["last_tool"] == "shell"
    assert by_id["t-running"]["change_id"] == "add-hive-worker"
    assert by_id["t-running"]["session_id"] == "parent-1"
    assert by_id["t-done"]["observed_status"] == "finished"
    assert by_id["t-done"]["host"] == "codex"
    # parent 过滤
    assert subagents.scan(host, home=home, proj_key="-repo", repo_root=repo, change_ids=[], now=now, session_id="parent-2") == []


def test_generic_host_has_no_layout(tmp_path: Path):
    host = hosts.resolve_host(env={})
    assert host.subagent_root(tmp_path, "-k") is None
    assert subagents.scan(host, home=tmp_path, proj_key="-k", repo_root=tmp_path, change_ids=[]) == []


def test_match_change_id_prefers_longest():
    assert subagents.match_change_id(["Implement add-web-pages-v2"], ["add-web", "add-web-pages-v2"]) == "add-web-pages-v2"
    assert subagents.match_change_id([None, ""], ["x"]) is None


def test_known_external_worktree_still_requires_session_scope(tmp_path):
    home, repo, external = tmp_path / 'home', tmp_path / 'repo', tmp_path / 'task_log/wt'
    now = datetime.now().astimezone()
    sdir = home / '.claude/projects/-repo/sess-1'
    _claude_agent(sdir, 'external', description='review', worktree=external,
                  last_lines=[_tool_result(now, cwd=str(external))], mtime=now)
    host = _claude_host(home)
    kw = dict(home=home, proj_key='-repo', repo_root=repo, change_ids=[], now=now)
    assert subagents.scan(host, **kw) == []
    assert len(subagents.scan(host, worktree_roots=[external], **kw)) == 1
    assert subagents.scan(host, session_id='another-session', worktree_roots=[external], **kw) == []
    day = home / '.codex/sessions' / now.strftime('%Y/%m/%d')
    _codex_rollout(day, 'external', cwd=external, subagent=True, lines=[], mtime=now)
    host = hosts.resolve_host('codex', env={})
    assert subagents.scan(host, **kw) == []
    assert len(subagents.scan(host, worktree_roots=[external], **kw)) == 1
    assert subagents.scan(host, session_id='another-session', worktree_roots=[external], **kw) == []
