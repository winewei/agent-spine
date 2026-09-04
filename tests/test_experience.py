"""experience.py 测试：传输 / 组装 / 闸门 / 提交 / 召回 / 污染 / 命令面。

绝不触碰真实 OpenViking server：所有网络路径都 monkeypatch
``urllib.request.urlopen``（experience.Client 唯一的出口）。
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from npc import config as _config
from npc import experience as _exp
from npc import state as _state


# ============================================================
# Helpers
# ============================================================


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def fake_urlopen(monkeypatch, handler):
    """把 urllib.request.urlopen 换成 handler(req) -> bytes | Exception。"""
    calls: list[tuple[str, str, dict | None]] = []

    def _open(req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        calls.append((req.get_method(), req.full_url, body))
        out = handler(req, body)
        if isinstance(out, Exception):
            raise out
        return FakeResponse(out if isinstance(out, bytes) else json.dumps(out).encode())

    monkeypatch.setattr("urllib.request.urlopen", _open)
    return calls


def envelope(result: dict) -> dict:
    return {"status": "ok", "result": result, "error": None}


def make_client() -> _exp.Client:
    return _exp.Client("http://127.0.0.1:1933", "k-test", 3000)


IMPLEMENT_SUMMARY = """# Implement Summary — add-thing

Commit: abc1234
Tasks Completed: 3 / 3
Tests: pass
Files Modified:
- src/a.py
- src/b.py

## Key Decisions
- 走 urllib 而非第三方 HTTP 客户端

## Issues Encountered
- 测试基线本就有 2 个失败，改判零回归

## Verification
- pytest -q
"""

FIX_SUMMARY = """# Fix Round 1 Summary — add-thing

Commit: def5678
Tests After Fix: pass

## Per-Finding Resolution
- F1 (缺长度校验): 在入口统一校验

## Locations Scanned (Root-Cause Sweep)
- category=validation:
  - src/a.py:42 (修)

## Invariant Sweep
- 不变量: 所有写入路径必须经同一校验入口
  - 落点: src/a.py:31 (修)

## Real Regressions
- None
"""


def review_json(*findings: dict) -> dict:
    return {"verdict": "changes-requested", "findings": list(findings)}


def finding(fid="F1", sev="critical", cat="validation", title="缺长度校验", file="src/a.py", in_scope=True):
    return {
        "id": fid,
        "severity": sev,
        "category": cat,
        "title": title,
        "file": file,
        "line_range": "1",
        "detail": "长正文，不应被提交",
        "recommendation": "补校验",
        "in_scope": in_scope,
    }


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    """一个内容齐全的 <base> 产物目录。"""
    base = tmp_path / "001-add-thing"
    base.mkdir()
    (base / "implement.summary.md").write_text(IMPLEMENT_SUMMARY, encoding="utf-8")
    (base / "round-0.review.json").write_text(
        json.dumps(
            review_json(
                finding(),
                finding(fid="F2", sev="high", cat="test-coverage", title="缺并发回归", file="tests/t.py"),
                finding(fid="F3", cat="style", title="越界建议", in_scope=False),
            )
        ),
        encoding="utf-8",
    )
    (base / "round-1.fix.summary.md").write_text(FIX_SUMMARY, encoding="utf-8")
    (base / "round-1.review.json").write_text(
        json.dumps(review_json()), encoding="utf-8"
    )
    # 白名单外的文件：断言组装器绝不读它们
    (base / "events.jsonl").write_text('{"secret":"must-not-leak"}\n', encoding="utf-8")
    (base / "implement.prompt.md").write_text("PROMPT-SECRET", encoding="utf-8")
    return base


ARCHIVED_ENTRY = {
    "seq": 1,
    "change_id": "add-thing",
    "status": "archived",
    "blocking_trend": [2, 0],
    "total_rounds": 2,
    "phases": {"implement": {"tests": "pass"}, "fix-r1": {"tests": "pass"}},
}


class FakePaths:
    def __init__(self, repo_root: Path, run_dir: Path):
        self.repo_root = repo_root
        self.proj_key = "-tmp-repo"
        self.run_ts = "2026-09-05-0100"
        self.run_dir = run_dir


# ============================================================
# (a) 传输层
# ============================================================


def test_parse_env_file_handles_export_comments_and_quotes(tmp_path: Path):
    p = tmp_path / "x.env"
    p.write_text(
        "# comment\n\nexport OPENVIKING_API_KEY='sk-abc'\n"
        'OPENVIKING_BASE_URL="http://127.0.0.1:9999"\nBROKEN\n',
        encoding="utf-8",
    )
    env = _exp.parse_env_file(p)
    assert env == {
        "OPENVIKING_API_KEY": "sk-abc",
        "OPENVIKING_BASE_URL": "http://127.0.0.1:9999",
    }


def test_parse_env_file_missing_returns_empty(tmp_path: Path):
    assert _exp.parse_env_file(tmp_path / "nope.env") == {}


def test_from_config_returns_none_without_key(tmp_path: Path):
    cfg = _config.ExperienceConfig(env_file=str(tmp_path / "nope.env"))
    assert _exp.from_config(cfg) is None


def test_from_config_base_url_falls_back_to_env_file(tmp_path: Path):
    envf = tmp_path / "c.env"
    envf.write_text(
        "OPENVIKING_API_KEY=k\nOPENVIKING_BASE_URL=http://127.0.0.1:2000\n",
        encoding="utf-8",
    )
    client = _exp.from_config(_config.ExperienceConfig(env_file=str(envf)))
    assert client is not None
    assert client.base_url == "http://127.0.0.1:2000"
    # 显式 base_url 优先于 env 文件
    explicit = _exp.from_config(
        _config.ExperienceConfig(env_file=str(envf), base_url="http://127.0.0.1:3000")
    )
    assert explicit.base_url == "http://127.0.0.1:3000"


def test_client_maps_http_error(monkeypatch):
    fake_urlopen(
        monkeypatch,
        lambda req, body: urllib.error.HTTPError(req.full_url, 503, "boom", {}, None),
    )
    with pytest.raises(_exp.ExperienceError) as ei:
        make_client().get("/health")
    assert ei.value.code == "http_503"


def test_client_maps_urlerror_to_unreachable(monkeypatch):
    fake_urlopen(
        monkeypatch,
        lambda req, body: urllib.error.URLError(ConnectionRefusedError("refused")),
    )
    with pytest.raises(_exp.ExperienceError) as ei:
        make_client().post("/api/v1/sessions", {})
    assert ei.value.code == "unreachable"


def test_client_maps_timeout(monkeypatch):
    fake_urlopen(monkeypatch, lambda req, body: TimeoutError("timed out"))
    with pytest.raises(_exp.ExperienceError) as ei:
        make_client().get("/health")
    assert ei.value.code == "timeout"


def test_client_maps_urlerror_wrapping_timeout(monkeypatch):
    fake_urlopen(
        monkeypatch, lambda req, body: urllib.error.URLError(TimeoutError("timed out"))
    )
    with pytest.raises(_exp.ExperienceError) as ei:
        make_client().get("/health")
    assert ei.value.code == "timeout"


def test_client_maps_bad_json(monkeypatch):
    fake_urlopen(monkeypatch, lambda req, body: b"<html>not json</html>")
    with pytest.raises(_exp.ExperienceError) as ei:
        make_client().get("/health")
    assert ei.value.code == "bad_json"


def test_client_sends_api_key_header(monkeypatch):
    seen: dict = {}

    def handler(req, body):
        seen["key"] = req.get_header("X-api-key")
        seen["ct"] = req.get_header("Content-type")
        return envelope({})

    fake_urlopen(monkeypatch, handler)
    make_client().post("/api/v1/sessions", {"a": 1})
    assert seen["key"] == "k-test"
    assert seen["ct"] == "application/json"


def test_probe_health(monkeypatch):
    fake_urlopen(
        monkeypatch,
        lambda req, body: {"status": "ok", "healthy": True, "version": "0.4.17.1"},
    )
    assert _exp.probe_health(make_client())["version"] == "0.4.17.1"


# ============================================================
# (b) 写入侧组装
# ============================================================


def test_strip_injection_removes_tag_and_section():
    text = (
        "## 历史经验（外部召回，非本 change 的规格）\n\n"
        '<npc-experience uri="viking://a" score="0.72">\n正文\n</npc-experience>\n\n'
        "尾部提醒\n\n## Key Decisions\n- 保留我\n"
    )
    out = _exp.strip_injection(text)
    assert "npc-experience" not in out
    assert "历史经验" not in out
    assert "尾部提醒" not in out
    assert "## Key Decisions" in out and "- 保留我" in out


def test_strip_injection_keeps_deeper_headings_inside_block():
    text = "## 历史经验（外部召回）\n### 子标题\nx\n## 之后\ny\n"
    out = _exp.strip_injection(text)
    assert "子标题" not in out
    assert "## 之后" in out


def test_build_case_criteria_from_in_scope_findings(base_dir: Path, tmp_path: Path):
    case = _exp.build_case("add-thing", "-proj", base_dir, ARCHIVED_ENTRY)
    assert case["protocol"] == _exp.CASE_PROTOCOL
    assert case["case"]["task_signature"] == "-proj:add-thing"
    descs = [c["description"] for c in case["case"]["rubric"]["criteria"]]
    assert descs == ["validation: 缺长度校验", "test-coverage: 缺并发回归"]
    assert case["case"]["input"]["total_rounds"] == 2
    assert case["case"]["input"]["tests"] == "pass"


def test_build_case_empty_findings_falls_back_to_default_criterion(tmp_path: Path):
    base = tmp_path / "b"
    base.mkdir()
    (base / "round-0.review.json").write_text(json.dumps(review_json()), "utf-8")
    case = _exp.build_case("c", "-p", base, {"total_rounds": 1, "phases": {}})
    assert case["case"]["rubric"]["criteria"] == [
        {"description": _exp.DEFAULT_CRITERION, "weight": 1.0}
    ]


def test_build_case_proposal_summary_from_repo(base_dir: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    d = repo / "openspec" / "changes" / "add-thing"
    d.mkdir(parents=True)
    (d / "proposal.md").write_text(
        "# Proposal\n\n## Why\n因为需要 X。\n\n## What Changes\n- 加 Y\n\n## Impact\n无关段\n",
        encoding="utf-8",
    )
    case = _exp.build_case("add-thing", "-p", base_dir, ARCHIVED_ENTRY, repo_root=repo)
    summary = case["case"]["input"]["proposal_summary"]
    assert "因为需要 X" in summary and "加 Y" in summary
    assert "无关段" not in summary
    assert len(summary) <= _exp.PROPOSAL_SUMMARY_MAX_CHARS


def test_build_case_missing_proposal_is_empty_summary(base_dir: Path, tmp_path: Path):
    case = _exp.build_case("add-thing", "-p", base_dir, ARCHIVED_ENTRY, repo_root=tmp_path)
    assert case["case"]["input"]["proposal_summary"] == ""


def test_build_messages_five_sections_and_fast_path_header(base_dir: Path):
    msgs = _exp.build_messages("add-thing", "-p", base_dir, ARCHIVED_ENTRY)
    assert len(msgs) == 5
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[0]["content"].startswith(_exp.CASE_HEADER + "\n```json\n")
    assert msgs[0]["content"].endswith("\n```")

    assert "## Key Decisions" in msgs[1]["content"]
    assert "Files Modified:" in msgs[1]["content"] and "src/a.py" in msgs[1]["content"]
    assert "## Verification" not in msgs[1]["content"]

    assert msgs[2]["content"].splitlines() == [
        "- [critical|validation] 缺长度校验 @src/a.py",
        "- [high|test-coverage] 缺并发回归 @tests/t.py",
    ]
    assert "越界建议" not in msgs[2]["content"]

    assert "## Per-Finding Resolution" in msgs[3]["content"]
    assert "## Invariant Sweep" in msgs[3]["content"]
    assert "Real Regressions" not in msgs[3]["content"]

    assert msgs[4]["content"] == (
        "归档成功：共 2 轮 review，tests=pass。"
        "请把可复用的做法归纳为规则（Situation/Approach/Reflect），不要复述事件经过。"
    )


def test_build_messages_never_reads_events_or_prompt(base_dir: Path):
    blob = json.dumps(_exp.build_messages("add-thing", "-p", base_dir, ARCHIVED_ENTRY))
    assert "must-not-leak" not in blob
    assert "PROMPT-SECRET" not in blob
    assert "长正文，不应被提交" not in blob


def test_build_messages_strips_injection_from_summary(base_dir: Path):
    (base_dir / "implement.summary.md").write_text(
        IMPLEMENT_SUMMARY
        + '\n## 历史经验（外部召回）\n<npc-experience uri="viking://x" score="0.9">抄回来的</npc-experience>\n',
        encoding="utf-8",
    )
    blob = json.dumps(_exp.build_messages("add-thing", "-p", base_dir, ARCHIVED_ENTRY))
    assert "npc-experience" not in blob and "抄回来的" not in blob


def test_build_messages_skips_empty_middle_sections(tmp_path: Path):
    base = tmp_path / "empty"
    base.mkdir()
    msgs = _exp.build_messages("c", "-p", base, {"total_rounds": 0, "phases": {}})
    assert len(msgs) == 2
    assert msgs[0]["content"].startswith(_exp.CASE_HEADER)
    assert msgs[-1]["content"].startswith("归档成功：共 0 轮")


@pytest.mark.parametrize(
    "overrides,expected_reason",
    [
        ({}, "ok"),
        ({"status": "failed"}, "not-archived"),
        ({"blocking_trend": [2, 1]}, "blocking-nonzero"),
        ({"blocking_trend": []}, "no-blocking-trend"),
        ({"override": True}, "override"),
        ({"reason": "force-archive"}, "force-archive"),
        ({"experience_contaminated": True}, "contaminated"),
    ],
)
def test_write_gate_verified(overrides, expected_reason):
    entry = {**ARCHIVED_ENTRY, **overrides}
    ok, reason = _exp.write_gate_ok(entry, "verified")
    assert reason == expected_reason
    assert ok is (expected_reason == "ok")


def test_write_gate_any_only_requires_archived():
    entry = {**ARCHIVED_ENTRY, "blocking_trend": [3], "reason": "force-archive"}
    assert _exp.write_gate_ok(entry, "any") == (True, "ok")
    assert _exp.write_gate_ok({**entry, "status": "failed"}, "any")[0] is False


# ============================================================
# (c) 提交
# ============================================================


def test_commit_three_step_order(monkeypatch, base_dir: Path, tmp_path: Path):
    calls = fake_urlopen(
        monkeypatch,
        lambda req, body: envelope(
            {"task_id": "t-1", "archive_uri": "viking://user/npc/archives/a"}
        ),
    )
    p = FakePaths(tmp_path, tmp_path)
    entry = {**ARCHIVED_ENTRY, "base": str(base_dir)}
    out = _exp.commit(make_client(), _config.ExperienceConfig(), p_like=p, seq=1, entry=entry)

    assert out["ok"] is True
    assert out["task_id"] == "t-1"
    assert out["session_id"] == "npc--tmp-repo-2026-09-05-0100-1-add-thing"
    paths = [c[1].rsplit("1933", 1)[1] for c in calls]
    sid = out["session_id"]
    assert paths[0] == "/api/v1/sessions"
    assert paths[-1] == f"/api/v1/sessions/{sid}/commit"
    assert all(pp == f"/api/v1/sessions/{sid}/messages" for pp in paths[1:-1])
    assert len(paths) == 2 + out["messages"]
    # memory_policy 只允许 experiences
    assert calls[0][2]["memory_policy"] == {"memory_types": ["experiences"]}
    saved = json.loads((base_dir / "experience.commit.json").read_text())
    assert saved["task_id"] == "t-1"


def test_commit_session_conflict_is_idempotent(monkeypatch, base_dir: Path, tmp_path: Path):
    def handler(req, body):
        if req.full_url.endswith("/api/v1/sessions"):
            return urllib.error.HTTPError(req.full_url, 409, "exists", {}, None)
        return envelope({"task_id": "t-2"})

    fake_urlopen(monkeypatch, handler)
    out = _exp.commit(
        make_client(),
        _config.ExperienceConfig(),
        p_like=FakePaths(tmp_path, tmp_path),
        seq=1,
        entry={**ARCHIVED_ENTRY, "base": str(base_dir)},
    )
    assert out == {
        "ok": True,
        "session_id": out["session_id"],
        "task_id": "t-2",
        "archive_uri": None,
        "messages": 5,
    }


def test_commit_failure_records_reason_and_does_not_raise(
    monkeypatch, base_dir: Path, tmp_path: Path
):
    fake_urlopen(
        monkeypatch, lambda req, body: urllib.error.URLError(ConnectionRefusedError())
    )
    out = _exp.commit(
        make_client(),
        _config.ExperienceConfig(),
        p_like=FakePaths(tmp_path, tmp_path),
        seq=1,
        entry={**ARCHIVED_ENTRY, "base": str(base_dir)},
    )
    assert out["ok"] is False and out["reason"] == "unreachable"
    assert json.loads((base_dir / "experience.commit.json").read_text())["ok"] is False


def test_commit_dry_run_sends_nothing(monkeypatch, base_dir: Path, tmp_path: Path):
    def boom(req, timeout=None):
        raise AssertionError("dry-run 不得发起任何请求")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    out = _exp.commit(
        None,
        _config.ExperienceConfig(),
        p_like=FakePaths(tmp_path, tmp_path),
        seq=1,
        entry={**ARCHIVED_ENTRY, "base": str(base_dir)},
        dry_run=True,
    )
    assert out["dry_run"] is True
    saved = json.loads(Path(out["path"]).read_text())
    assert len(saved["messages"]) == 5


# ============================================================
# (d) 召回
# ============================================================


def _entries(*items) -> dict:
    return envelope({"entries": list(items)})


EXP_URI = "viking://user/npc/memories/experiences/a.md"
EXP_URI2 = "viking://user/npc/memories/experiences/b.md"


def test_recall_filters_non_experience_uri_and_empty_text(monkeypatch):
    fake_urlopen(
        monkeypatch,
        lambda req, body: _entries(
            {"uri": EXP_URI, "score": 0.5, "text": "规则 A"},
            {"uri": "viking://user/npc/memories/events/x.md", "score": 0.9, "text": "事件"},
            {"uri": EXP_URI2, "score": 0.8, "text": "   "},
        ),
    )
    res = _exp.recall(make_client(), _config.ExperienceConfig(), phase="fix", query="q")
    assert res.error is None
    assert res.uris == [EXP_URI]


def test_recall_sorts_by_score_desc_and_sends_params(monkeypatch):
    calls = fake_urlopen(
        monkeypatch,
        lambda req, body: _entries(
            {"uri": EXP_URI, "score": 0.4, "text": "低"},
            {"uri": EXP_URI2, "score": 0.9, "text": "高"},
        ),
    )
    cfg = _config.ExperienceConfig(quota_experiences=2, score_threshold=0.5)
    res = _exp.recall(
        make_client(), cfg, phase="fix", query="q" * 300, exclude_uris=[EXP_URI]
    )
    assert [e["score"] for e in res.entries] == [0.9, 0.4]
    body = calls[0][2]
    assert body["mode"] == "context"
    assert body["quotas"] == {"experiences": 2}
    assert body["max_tokens"] == cfg.inject_max_tokens_fix
    assert body["score_threshold"] == 0.5
    assert body["rewrite"] is False and body["query_expansion"] == "off"
    assert body["exclude_uris"] == [EXP_URI]
    assert len(body["query"]) == _exp.QUERY_MAX_CHARS


def test_recall_soft_fails_on_transport_error(monkeypatch):
    fake_urlopen(monkeypatch, lambda req, body: TimeoutError("timed out"))
    res = _exp.recall(make_client(), _config.ExperienceConfig(), phase="fix", query="q")
    assert res.error == "timeout" and res.entries == ()


def test_recall_without_client_or_query():
    cfg = _config.ExperienceConfig()
    assert _exp.recall(None, cfg, phase="fix", query="q").error == "no_client"
    assert _exp.recall(make_client(), cfg, phase="fix", query="  ").error == "empty_query"


def test_build_query_shapes():
    impl = _exp.build_query(
        "implement", change_id="add-thing", proposal_title="加缓存", stack="python"
    )
    assert impl == "add-thing 加缓存 python"
    fix = _exp.build_query(
        "fix", change_id="add-thing", findings_titles=["validation: 缺校验", ""]
    )
    assert fix == "validation: 缺校验 add-thing"
    assert len(_exp.build_query("fix", change_id="x" * 500)) == _exp.QUERY_MAX_CHARS


def test_render_block_shell():
    res = _exp.RecallResult(
        entries=({"uri": EXP_URI, "score": 0.7234, "text": "正文"},), tokens=3
    )
    block = _exp.render_block(res)
    assert block.startswith(_exp.BLOCK_HEADING)
    assert f'<npc-experience uri="{EXP_URI}" score="0.72">' in block
    assert "</npc-experience>" in block
    assert block.rstrip().endswith("不要把本节内容抄进 summary 文件。")


def test_render_block_empty_result_is_empty_string():
    assert _exp.render_block(_exp.RecallResult()) == ""


def test_render_block_drops_lowest_score_over_budget():
    res = _exp.RecallResult(
        entries=(
            {"uri": EXP_URI, "score": 0.9, "text": "A" * 400},
            {"uri": EXP_URI2, "score": 0.4, "text": "B" * 400},
        )
    )
    block = _exp.render_block(res, max_tokens=120)
    assert EXP_URI in block and EXP_URI2 not in block
    assert _exp.render_block(res, max_tokens=1) == ""


def test_write_injection_record(tmp_path: Path):
    res = _exp.RecallResult(
        entries=({"uri": EXP_URI, "score": 0.7, "text": "正文"},), tokens=3, query="q"
    )
    block = _exp.render_block(res)
    path = _exp.write_injection_record(tmp_path, "fix", 2, res, "headsha", block=block)
    assert path.name == "fix-r2.experience.json"
    rec = json.loads(path.read_text())
    assert rec["phase"] == "fix" and rec["round"] == 2 and rec["query"] == "q"
    assert rec["uris"] == [{"uri": EXP_URI, "score": 0.7}]
    assert rec["head"] == "headsha"
    assert rec["tokens"] == _exp.estimate_tokens(block)
    assert len(rec["sha256"]) == 64

    plain = _exp.write_injection_record(tmp_path, "implement", None, res, None)
    assert plain.name == "implement.experience.json"


# ============================================================
# 污染检测
# ============================================================


def test_detect_contamination_by_tag():
    assert _exp.detect_contamination('见 <npc-experience uri="x">', []) is True


def test_detect_contamination_by_uri():
    recs = [{"phase": "fix", "uris": [{"uri": EXP_URI, "score": 0.7}], "block_text": ""}]
    assert _exp.detect_contamination(f"参考了 {EXP_URI} 的做法", recs) is True


def test_detect_contamination_by_long_span():
    body = "规则：" + "所有写入路径在持久化前必须经过同一校验入口。" * 8
    recs = [{"phase": "fix", "uris": [], "block_text": body}]
    assert _exp.detect_contamination("前言\n" + body[10:200] + "\n后记", recs) is True


def test_detect_contamination_negative():
    recs = [{"phase": "fix", "uris": [{"uri": EXP_URI}], "block_text": "X" * 500}]
    assert _exp.detect_contamination("完全无关的 summary 正文", recs) is False
    assert _exp.detect_contamination("", recs) is False


def test_read_injection_records_attaches_block_text(tmp_path: Path):
    res = _exp.RecallResult(entries=({"uri": EXP_URI, "score": 0.7, "text": "正文"},))
    block = _exp.render_block(res)
    (tmp_path / "fix-r1.experience.md").write_text(block, encoding="utf-8")
    _exp.write_injection_record(tmp_path, "fix", 1, res, None, block=block)
    (tmp_path / "experience.commit.json").write_text('{"ok":true}', encoding="utf-8")
    (tmp_path / "broken.experience.json").write_text("{oops", encoding="utf-8")

    recs = _exp.read_injection_records(tmp_path)
    assert len(recs) == 1
    assert recs[0]["block_text"] == block


# ============================================================
# (e) doctor
# ============================================================


def test_doctor_report_warns_dev_mode_and_missing_declaration(monkeypatch, tmp_path: Path):
    envf = tmp_path / "c.env"
    envf.write_text("OPENVIKING_API_KEY=k\n", encoding="utf-8")

    def handler(req, body):
        if req.full_url.endswith("/health"):
            return {"healthy": True, "version": "0.4.17.1", "auth_mode": "none"}
        return envelope({})  # fs/ls 返回非 list → 计数 None

    fake_urlopen(monkeypatch, handler)
    rep = _exp.doctor_report(_config.ExperienceConfig(env_file=str(envf)))
    assert rep["health"]["ok"] is True
    assert rep["experiences_count"] is None
    assert any("dev 模式" in w for w in rep["warnings"])
    assert any("extraction_model_declared" in w for w in rep["warnings"])


def test_doctor_report_warns_non_local_base_url(monkeypatch, tmp_path: Path):
    envf = tmp_path / "c.env"
    envf.write_text("OPENVIKING_API_KEY=k\n", encoding="utf-8")
    fake_urlopen(monkeypatch, lambda req, body: TimeoutError("timed out"))
    rep = _exp.doctor_report(
        _config.ExperienceConfig(env_file=str(envf), base_url="http://10.0.0.5:1933")
    )
    assert rep["health"]["ok"] is False and rep["health"]["error"] == "timeout"
    assert any("非本地地址" in w for w in rep["warnings"])


def test_doctor_report_without_credentials(tmp_path: Path):
    rep = _exp.doctor_report(_config.ExperienceConfig(env_file=str(tmp_path / "no.env")))
    assert rep["env_file_found"] is False and rep["health"] is None
    assert any("OPENVIKING_API_KEY" in w for w in rep["warnings"])


def test_doctor_report_counts_experiences_and_agent_evolution(monkeypatch, tmp_path: Path):
    envf = tmp_path / "c.env"
    envf.write_text("OPENVIKING_API_KEY=k\n", encoding="utf-8")
    rootf = tmp_path / "root.env"
    rootf.write_text("OPENVIKING_ROOT_API_KEY=r\n", encoding="utf-8")

    def handler(req, body):
        if req.full_url.endswith("/health"):
            return {"healthy": True, "version": "1", "auth_mode": "api_key"}
        if "/fs/ls" in req.full_url:
            return envelope([{"uri": "a.md", "isDir": False}, {"uri": "d", "isDir": True}])
        return envelope({"enabled": False})

    fake_urlopen(monkeypatch, handler)
    rep = _exp.doctor_report(
        _config.ExperienceConfig(
            env_file=str(envf), root_env_file=str(rootf), extraction_model_declared="gpt-5.4"
        )
    )
    assert rep["experiences_count"] == 1
    assert rep["agent_evolution"] is False
    assert any("agent_evolution" in w for w in rep["warnings"])
    assert not any("extraction_model_declared" in w for w in rep["warnings"])


# ============================================================
# 命令面
# ============================================================


def _bootstrap(make_args, capsys, change_id="add-thing"):
    _state.init_run(make_args(plan_order=json.dumps([change_id]), goal=None))
    capsys.readouterr()
    _state.add_change(make_args(seq=1, change_id=change_id, base=None))
    capsys.readouterr()


def _enable(repo: Path, **kv) -> None:
    (repo / ".npc").mkdir(exist_ok=True)
    body = "[experience]\nenabled = true\n" + "".join(f"{k} = {v}\n" for k, v in kv.items())
    (repo / ".npc" / "config.toml").write_text(body, encoding="utf-8")


def _out(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_commit_disabled_exits_zero(env_setup, make_args, capsys, monkeypatch):
    monkeypatch.chdir(env_setup.repo_root)
    _bootstrap(make_args, capsys)
    _exp.cli_commit(make_args(seq=1, dry_run=False, strict=False, gate=None))
    out = _out(capsys)
    assert out == {"ok": False, "skipped": True, "reason": "disabled"}


def test_cli_commit_disabled_strict_exits_one(env_setup, make_args, capsys, monkeypatch):
    monkeypatch.chdir(env_setup.repo_root)
    _bootstrap(make_args, capsys)
    with pytest.raises(SystemExit) as ei:
        _exp.cli_commit(make_args(seq=1, dry_run=False, strict=True, gate=None))
    assert ei.value.code == 1


def test_cli_commit_gate_blocks_pending_change(env_setup, make_args, capsys, monkeypatch):
    monkeypatch.chdir(env_setup.repo_root)
    _enable(env_setup.repo_root)
    _bootstrap(make_args, capsys)
    _exp.cli_commit(make_args(seq=1, dry_run=False, strict=False, gate=None))
    out = _out(capsys)
    assert out["skipped"] is True and out["reason"] == "not-archived"


def test_cli_commit_dry_run_writes_messages(env_setup, make_args, capsys, monkeypatch):
    monkeypatch.chdir(env_setup.repo_root)
    _enable(env_setup.repo_root)
    _bootstrap(make_args, capsys)

    def mut(state: dict) -> None:
        state["progress"][0].update(
            {"status": "archived", "blocking_trend": [1, 0], "total_rounds": 2}
        )

    _state.update_state(env_setup.state_json, env_setup.state_md, mut)
    base = Path(_state.read_state(env_setup.state_json)["progress"][0]["base"])
    (base / "implement.summary.md").write_text(IMPLEMENT_SUMMARY, encoding="utf-8")

    _exp.cli_commit(make_args(seq=1, dry_run=True, strict=False, gate=None))
    out = _out(capsys)
    assert out["ok"] is True and out["dry_run"] is True
    assert (base / "experience.messages.json").is_file()


def test_cli_commit_no_client_skips(env_setup, make_args, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(env_setup.repo_root)
    _enable(env_setup.repo_root, env_file=f'"{tmp_path / "absent.env"}"')
    _bootstrap(make_args, capsys)

    def mut(state: dict) -> None:
        state["progress"][0].update({"status": "archived", "blocking_trend": [0]})

    _state.update_state(env_setup.state_json, env_setup.state_md, mut)
    _exp.cli_commit(make_args(seq=1, dry_run=False, strict=False, gate=None))
    assert _out(capsys)["reason"] == "no_client"


def test_cli_commit_unknown_seq_exits_three(env_setup, make_args, capsys, monkeypatch):
    monkeypatch.chdir(env_setup.repo_root)
    _enable(env_setup.repo_root)
    _bootstrap(make_args, capsys)
    with pytest.raises(SystemExit) as ei:
        _exp.cli_commit(make_args(seq=9, dry_run=False, strict=False, gate=None))
    assert ei.value.code == 3


def test_cli_recall_disabled(env_setup, make_args, capsys, monkeypatch):
    monkeypatch.chdir(env_setup.repo_root)
    _bootstrap(make_args, capsys)
    _exp.cli_recall(
        make_args(seq=1, phase="fix", round_n=1, query=None, strict=False)
    )
    assert _out(capsys)["reason"] == "disabled"


def test_cli_recall_writes_block_and_record(env_setup, make_args, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(env_setup.repo_root)
    envf = tmp_path / "c.env"
    envf.write_text("OPENVIKING_API_KEY=k\n", encoding="utf-8")
    _enable(env_setup.repo_root, env_file=f'"{envf}"')
    _bootstrap(make_args, capsys)
    fake_urlopen(
        monkeypatch,
        lambda req, body: _entries({"uri": EXP_URI, "score": 0.81, "text": "先验规则"}),
    )
    _exp.cli_recall(make_args(seq=1, phase="fix", round_n=2, query="q", strict=False))
    out = _out(capsys)
    assert out["ok"] is True and out["entries"] == 1 and out["uris"] == [EXP_URI]
    block = Path(out["path"]).read_text()
    assert Path(out["path"]).name == "fix-r2.experience.md"
    assert "先验规则" in block and out["tokens"] == _exp.estimate_tokens(block)


def test_cli_recall_soft_fail_writes_empty_block(env_setup, make_args, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(env_setup.repo_root)
    envf = tmp_path / "c.env"
    envf.write_text("OPENVIKING_API_KEY=k\n", encoding="utf-8")
    _enable(env_setup.repo_root, env_file=f'"{envf}"')
    _bootstrap(make_args, capsys)
    fake_urlopen(monkeypatch, lambda req, body: urllib.error.URLError("down"))
    _exp.cli_recall(make_args(seq=1, phase="implement", round_n=None, query="q", strict=False))
    out = _out(capsys)
    assert out["ok"] is False and out["error"] == "unreachable"
    assert Path(out["path"]).read_text() == ""

    with pytest.raises(SystemExit) as ei:
        _exp.cli_recall(
            make_args(seq=1, phase="implement", round_n=None, query="q", strict=True)
        )
    assert ei.value.code == 1


def test_cli_status_aggregates(env_setup, make_args, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(env_setup.repo_root)
    _bootstrap(make_args, capsys)
    base = Path(_state.read_state(env_setup.state_json)["progress"][0]["base"])
    (base / "experience.commit.json").write_text(
        json.dumps({"ok": True, "task_id": "t-9"}), encoding="utf-8"
    )
    res = _exp.RecallResult(entries=({"uri": EXP_URI, "score": 0.7, "text": "x"},))
    _exp.write_injection_record(base, "fix", 1, res, None)

    _exp.cli_status(make_args(seq=None))
    out = _out(capsys)
    assert out["ok"] is True and out["enabled"] is False
    item = out["items"][0]
    assert item["committed"] is True and item["task_id"] == "t-9"
    assert item["injected_count"] == 1 and item["injected_tokens"] > 0
    # 未启用时不联网，task_status 保持 null
    assert item["task_status"] is None


def test_cli_doctor_exits_one_when_unreachable(monkeypatch, fake_repo, capsys):
    monkeypatch.chdir(fake_repo)
    _enable(fake_repo, base_url='"http://127.0.0.1:1"')
    fake_urlopen(monkeypatch, lambda req, body: urllib.error.URLError("refused"))
    with pytest.raises(SystemExit) as ei:
        _exp.cli_doctor(make_ns())
    assert ei.value.code == 1
    assert _out(capsys)["ok"] is False


def make_ns():
    import argparse

    return argparse.Namespace(state_json=None, run_ts=None, task_log_dir=None)
