"""engines.py 测试：JSON 提取、引擎工厂、ClaudeEngine 子进程交互。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from npc import engines as _engines
from npc.config import ReviewEngineConfig


# ============================================================
# extract_json_object
# ============================================================


def test_extract_json_object_direct():
    s = '{"verdict": "approve", "findings": []}'
    assert _engines.extract_json_object(s) == s


def test_extract_json_object_with_surrounding_text():
    s = 'Here is your review:\n\n{"verdict": "approve", "findings": []}\n\nthanks!'
    out = _engines.extract_json_object(s)
    assert out == '{"verdict": "approve", "findings": []}'


def test_extract_json_object_fenced_code_block():
    s = '```json\n{"verdict": "approve", "findings": []}\n```\n'
    out = _engines.extract_json_object(s)
    assert out is not None
    assert json.loads(out)["verdict"] == "approve"


def test_extract_json_object_handles_braces_in_strings():
    payload = {"verdict": "approve", "findings": [], "note": "has } in string"}
    s = "prefix\n" + json.dumps(payload) + "\nsuffix"
    out = _engines.extract_json_object(s)
    assert out is not None
    assert json.loads(out) == payload


def test_extract_json_object_nested():
    payload = {"a": {"b": {"c": 1}}, "verdict": "approve"}
    s = "noise " + json.dumps(payload) + " more noise"
    out = _engines.extract_json_object(s)
    assert json.loads(out) == payload


def test_extract_json_object_returns_none_for_garbage():
    assert _engines.extract_json_object("nothing useful here") is None
    assert _engines.extract_json_object("") is None
    assert _engines.extract_json_object("only { without close") is None


def test_extract_json_object_handles_escaped_quotes():
    payload = {"verdict": "approve", "detail": 'he said \\"yes\\"'}
    s = json.dumps(payload)
    out = _engines.extract_json_object("garbage " + s + " trailing")
    assert json.loads(out) == json.loads(s)


# ============================================================
# get_engine 工厂
# ============================================================


def test_get_engine_codex_uses_config_bin(tmp_path: Path):
    cfg = ReviewEngineConfig(engine="codex", codex_bin="/opt/codex")
    eng = _engines.get_engine(cfg)
    assert isinstance(eng, _engines.CodexEngine)
    assert eng.codex_bin == "/opt/codex"


def test_get_engine_claude_uses_config_bin(monkeypatch):
    cfg = ReviewEngineConfig(
        engine="claude",
        claude_bin="/opt/claude",
        claude_model="claude-opus-4-7",
        claude_extra_args=("--foo",),
    )
    eng = _engines.get_engine(cfg)
    assert isinstance(eng, _engines.ClaudeEngine)
    assert eng.claude_bin == "/opt/claude"
    assert eng.model == "claude-opus-4-7"
    assert eng.extra_args == ("--foo",)


def test_get_engine_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(_engines.shutil, "which", lambda _: "/path/from/which/claude")
    cfg = ReviewEngineConfig(engine="codex", codex_bin="/opt/codex")
    eng = _engines.get_engine(cfg, name_override="claude")
    assert isinstance(eng, _engines.ClaudeEngine)
    assert eng.claude_bin == "/path/from/which/claude"


def test_get_engine_unknown_name_raises():
    cfg = ReviewEngineConfig(engine="codex", codex_bin="/opt/codex")
    with pytest.raises(_engines.EngineError, match="未知 review engine"):
        _engines.get_engine(cfg, name_override="gemini")


def test_get_engine_missing_codex_raises(monkeypatch):
    monkeypatch.setattr(_engines.shutil, "which", lambda _: None)
    cfg = ReviewEngineConfig(engine="codex")
    with pytest.raises(_engines.EngineError, match="codex"):
        _engines.get_engine(cfg)


def test_get_engine_missing_claude_raises(monkeypatch):
    monkeypatch.setattr(_engines.shutil, "which", lambda _: None)
    cfg = ReviewEngineConfig(engine="claude")
    with pytest.raises(_engines.EngineError, match="claude"):
        _engines.get_engine(cfg)


# ============================================================
# CodexEngine.run（cmd 拼装）
# ============================================================


def test_codex_engine_run_invokes_subprocess(monkeypatch, tmp_path: Path):
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["stdin"] = kwargs.get("input")
        # 模拟 codex 写好 review_out
        review_out_idx = cmd.index("-o") + 1
        Path(cmd[review_out_idx]).write_text('{"verdict": "approve", "findings": []}')
        m = MagicMock()
        m.returncode = 0
        return m

    monkeypatch.setattr(_engines.subprocess, "run", fake_run)

    review_out = tmp_path / "r.json"
    events_out = tmp_path / "e.jsonl"
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    pt = tmp_path / "portable-timeout"
    pt.write_text("#!/bin/sh\n")

    eng = _engines.CodexEngine("/fake/codex")
    rc = eng.run(
        _engines.ReviewRunInputs(
            repo_root=tmp_path,
            schema_path=schema,
            focus_text="please review",
            review_out=review_out,
            events_out=events_out,
            timeout_sec=900,
            portable_timeout=pt,
        )
    )
    assert rc == 0
    assert captured["cmd"][0] == str(pt)
    assert captured["cmd"][2] == "/fake/codex"
    assert "--output-schema" in captured["cmd"]
    # focus_text 之后会追加 codex exec loop-guard 约束，stdin 以 focus_text 开头
    assert captured["stdin"].startswith(b"please review")
    assert b"codex exec" in captured["stdin"]


# ============================================================
# ClaudeEngine.run
# ============================================================


def test_claude_engine_run_extracts_json_from_stdout(monkeypatch, tmp_path: Path):
    """claude -p 退出码 0、stdout 里有合法 JSON → review_out 写入解析后 JSON。"""
    review_json = '{"verdict": "approve", "findings": []}'

    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ("preamble noise\n" + review_json + "\ntrailing\n").encode("utf-8")
        return m

    monkeypatch.setattr(_engines.subprocess, "run", fake_run)

    review_out = tmp_path / "r.json"
    events_out = tmp_path / "e.jsonl"
    schema = tmp_path / "schema.json"
    schema.write_text('{"$schema": "x"}')
    pt = tmp_path / "portable-timeout"
    pt.write_text("#!/bin/sh\n")

    eng = _engines.ClaudeEngine("/fake/claude", model="claude-opus-4-7")
    rc = eng.run(
        _engines.ReviewRunInputs(
            repo_root=tmp_path,
            schema_path=schema,
            focus_text="please review the diff",
            review_out=review_out,
            events_out=events_out,
            timeout_sec=900,
            portable_timeout=pt,
        )
    )
    assert rc == 0
    parsed = json.loads(review_out.read_text())
    assert parsed["verdict"] == "approve"
    # events 文件保留了 cmd / prompt / stdout
    evt = events_out.read_text()
    assert "claude -p invocation" in evt
    assert "please review the diff" in evt
    assert review_json in evt


def test_claude_engine_run_propagates_nonzero_exit(monkeypatch, tmp_path: Path):
    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 124
        m.stdout = b""
        return m

    monkeypatch.setattr(_engines.subprocess, "run", fake_run)

    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    pt = tmp_path / "pt"
    pt.write_text("")

    eng = _engines.ClaudeEngine("/fake/claude")
    rc = eng.run(
        _engines.ReviewRunInputs(
            repo_root=tmp_path,
            schema_path=schema,
            focus_text="x",
            review_out=tmp_path / "r.json",
            events_out=tmp_path / "e.jsonl",
            timeout_sec=10,
            portable_timeout=pt,
        )
    )
    assert rc == 124
    assert not (tmp_path / "r.json").exists()


def test_claude_engine_run_returns_extract_failed_when_no_json(monkeypatch, tmp_path: Path):
    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = b"sorry I cannot output JSON today"
        return m

    monkeypatch.setattr(_engines.subprocess, "run", fake_run)

    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    pt = tmp_path / "pt"
    pt.write_text("")

    eng = _engines.ClaudeEngine("/fake/claude")
    rc = eng.run(
        _engines.ReviewRunInputs(
            repo_root=tmp_path,
            schema_path=schema,
            focus_text="x",
            review_out=tmp_path / "r.json",
            events_out=tmp_path / "e.jsonl",
            timeout_sec=10,
            portable_timeout=pt,
        )
    )
    assert rc == _engines.CLAUDE_JSON_EXTRACT_FAILED_RC
    assert "ERROR" in (tmp_path / "e.jsonl").read_text()


def test_claude_engine_compose_prompt_includes_schema(tmp_path: Path):
    schema = tmp_path / "schema.json"
    schema.write_text('{"some": "schema"}')
    out = _engines.ClaudeEngine._compose_prompt("focus body", schema)
    assert "focus body" in out
    assert '"some": "schema"' in out
    assert "claude -p 模式" in out
