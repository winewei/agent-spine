"""telemetry 模块测试。

覆盖：
- 单元：estimate_tokens / emit_event / iter_events / aggregate / hotspots / _parse_since
- CLI handler：cli_emit / cli_tail / cli_agg / cli_hotspots / cli_estimate_tokens
- 集成：events.phase_exit / pipeline._do_phase_exit 自动 emit
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from npc import events as _events, state as _state, telemetry as _telemetry


# ============================================================
# 单元：估算 + emit + iter
# ============================================================


def test_estimate_tokens_bytes():
    assert _telemetry.estimate_tokens_bytes(0) == 0
    assert _telemetry.estimate_tokens_bytes(4) == 1
    assert _telemetry.estimate_tokens_bytes(100) == 25
    # 负数视为 0
    assert _telemetry.estimate_tokens_bytes(-5) == 0


def test_estimate_tokens_text():
    assert _telemetry.estimate_tokens_text("hello world") == 11 // 4
    # 中文：UTF-8 每字 3 bytes
    assert _telemetry.estimate_tokens_text("测试") == 6 // 4


def test_estimate_tokens_file(tmp_path: Path):
    f = tmp_path / "x.md"
    f.write_text("a" * 400, encoding="utf-8")
    size, tokens = _telemetry.estimate_tokens_file(f)
    assert size == 400
    assert tokens == 100
    # 不存在文件
    size, tokens = _telemetry.estimate_tokens_file(tmp_path / "missing.md")
    assert size is None and tokens is None


def test_emit_event_writes_line(isolate_telemetry: Path):
    ok = _telemetry.emit_event({"kind": "phase.exit", "proj_key": "demo"})
    assert ok is True
    ep = _telemetry.events_path()
    assert ep.is_file()
    lines = ep.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "phase.exit"
    assert rec["schema_version"] == 1
    assert rec["proj_key"] == "demo"
    assert "ts" in rec
    # schema-v1.json 被拷贝
    assert _telemetry.schema_path().is_file()


def test_emit_event_drops_invalid_record():
    assert _telemetry.emit_event(None) is False  # type: ignore[arg-type]
    assert _telemetry.emit_event({}) is False
    assert _telemetry.emit_event({"kind": ""}) is False


def test_emit_event_swallow_write_failure(monkeypatch, isolate_telemetry: Path):
    """events.ndjson 父目录不可写时，emit 返回 False 不抛异常。"""

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(_telemetry, "_ensure_dirs", boom)
    assert _telemetry.emit_event({"kind": "phase.exit", "proj_key": "x"}) is False


def test_iter_events_skips_malformed(isolate_telemetry: Path):
    ep = _telemetry.events_path()
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(
        '{"kind":"phase.exit","proj_key":"a"}\n'
        "not-json\n"
        "\n"
        '{"kind":"review.round","proj_key":"a"}\n',
        encoding="utf-8",
    )
    evs = list(_telemetry.iter_events())
    assert [e["kind"] for e in evs] == ["phase.exit", "review.round"]


# ============================================================
# 单元：since 解析
# ============================================================


def test_parse_since_units():
    now = datetime.now(timezone.utc)
    d7 = _telemetry._parse_since("7d")
    assert d7 is not None and (now - d7) >= timedelta(days=6, hours=23)
    h24 = _telemetry._parse_since("24h")
    assert h24 is not None and (now - h24) >= timedelta(hours=23, minutes=59)
    m30 = _telemetry._parse_since("30m")
    assert m30 is not None and (now - m30) >= timedelta(minutes=29)
    assert _telemetry._parse_since(None) is None
    assert _telemetry._parse_since("") is None


def test_parse_since_iso():
    dt = _telemetry._parse_since("2026-05-01T00:00:00+00:00")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 5


def test_parse_since_invalid():
    with pytest.raises(ValueError):
        _telemetry._parse_since("garbage")


# ============================================================
# 单元：aggregate + hotspots
# ============================================================


def _seed_events(events: list[dict]) -> None:
    for e in events:
        _telemetry.emit_event(e)


def test_aggregate_by_phase(isolate_telemetry: Path):
    _seed_events([
        {"kind": "phase.exit", "proj_key": "p", "phase": "implement",
         "status": "done", "duration_ms": 1000, "retry_count": 0},
        {"kind": "phase.exit", "proj_key": "p", "phase": "implement",
         "status": "failed", "duration_ms": 2000, "outcome_reason": "implementer"},
        {"kind": "review.round", "proj_key": "p", "phase": "review-r0",
         "round": 0, "status": "done", "duration_ms": 30000,
         "verdict": "must-fix", "blocking_count": 4, "retry_count": 1},
    ])
    out = _telemetry.aggregate(_telemetry.iter_events(), by="phase")
    assert "implement" in out
    impl = out["implement"]
    assert impl["count"] == 2
    assert impl["done"] == 1
    assert impl["failed"] == 1
    assert impl["failure_rate"] == 0.5
    assert impl["duration_ms"]["p50"] in (1000, 2000)
    assert impl["duration_ms"]["max"] == 2000
    assert impl["reasons"] == {"implementer": 1}

    rv = out["review-r0"]
    assert rv["review_rounds"] == 1
    assert rv["verdicts"] == {"must-fix": 1}
    assert rv["blocking_total"] == 4
    assert rv["retry_count_sum"] == 1


def test_aggregate_by_change(isolate_telemetry: Path):
    _seed_events([
        {"kind": "phase.exit", "proj_key": "p", "change_seq": 1,
         "change_id": "add-foo", "phase": "implement", "status": "done", "duration_ms": 100},
        {"kind": "phase.exit", "proj_key": "p", "change_seq": 1,
         "change_id": "add-foo", "phase": "fix-r0", "status": "done", "duration_ms": 200},
        {"kind": "phase.exit", "proj_key": "p", "change_seq": 2,
         "change_id": "add-bar", "phase": "implement", "status": "failed", "duration_ms": 50},
    ])
    out = _telemetry.aggregate(_telemetry.iter_events(), by="change")
    assert "001-add-foo" in out
    assert out["001-add-foo"]["count"] == 2
    assert out["002-add-bar"]["count"] == 1
    assert out["002-add-bar"]["failure_rate"] == 1.0


def test_aggregate_by_week(isolate_telemetry: Path):
    _seed_events([
        {"kind": "phase.exit", "proj_key": "p", "phase": "implement",
         "status": "done", "duration_ms": 10,
         "ts": "2026-01-05T10:00:00+00:00"},
        {"kind": "phase.exit", "proj_key": "p", "phase": "implement",
         "status": "done", "duration_ms": 20,
         "ts": "2026-01-12T10:00:00+00:00"},
    ])
    out = _telemetry.aggregate(_telemetry.iter_events(), by="week")
    keys = list(out.keys())
    assert any(k.startswith("2026-W") for k in keys)
    assert sum(b["count"] for b in out.values()) == 2


def test_aggregate_invalid_by():
    with pytest.raises(ValueError):
        _telemetry.aggregate([], by="bogus")


def test_hotspots_orders_by_score(isolate_telemetry: Path):
    _seed_events([
        # phase A：失败率高 + 耗时长
        {"kind": "phase.exit", "proj_key": "p", "phase": "fix-r0",
         "status": "failed", "duration_ms": 50000, "retry_count": 2,
         "outcome_reason": "fixer"},
        {"kind": "phase.exit", "proj_key": "p", "phase": "fix-r0",
         "status": "failed", "duration_ms": 60000, "retry_count": 1},
        # phase B：完全成功
        {"kind": "phase.exit", "proj_key": "p", "phase": "implement",
         "status": "done", "duration_ms": 1000},
    ])
    hs = _telemetry.hotspots(_telemetry.iter_events(), top=5)
    assert hs[0]["phase"] == "fix-r0"
    assert hs[0]["score"] > hs[1]["score"]
    assert hs[0]["failure_rate"] == 1.0
    assert hs[0]["retry_count_sum"] == 3


# ============================================================
# CLI handler 测试
# ============================================================


def _read_emit(capsys) -> dict:
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


def test_cli_emit_records_line(isolate_telemetry: Path, capsys, fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    args = argparse.Namespace(
        kind="phase.exit",
        seq=1,
        change_id="foo",
        phase="implement",
        status="done",
        duration_ms=1234,
        proj_key=None,
        run_ts=None,
        extra='{"engine":"codex"}',
    )
    _telemetry.cli_emit(args)
    payload = _read_emit(capsys)
    assert payload["ok"] is True
    rec = json.loads(_telemetry.events_path().read_text().splitlines()[-1])
    assert rec["kind"] == "phase.exit"
    assert rec["change_seq"] == 1
    assert rec["change_id"] == "foo"
    assert rec["status"] == "done"
    assert rec["duration_ms"] == 1234
    assert rec["engine"] == "codex"
    assert rec["proj_key"].startswith("-")  # mangled path


def test_cli_emit_rejects_bad_extra(isolate_telemetry: Path, capsys, fake_repo, monkeypatch):
    monkeypatch.chdir(fake_repo)
    args = argparse.Namespace(
        kind="phase.exit", seq=None, change_id=None, phase=None, status=None,
        duration_ms=None, proj_key="x", run_ts=None, extra="not-json",
    )
    with pytest.raises(SystemExit):
        _telemetry.cli_emit(args)
    payload = _read_emit(capsys)
    assert payload["error"] == "invalid_extra"


def test_cli_tail_filter_and_limit(isolate_telemetry: Path, capsys):
    for i in range(5):
        _telemetry.emit_event({"kind": "phase.exit", "proj_key": "p", "duration_ms": i})
    _telemetry.emit_event({"kind": "review.round", "proj_key": "p"})
    args = argparse.Namespace(kind="phase.exit", last=2)
    _telemetry.cli_tail(args)
    payload = _read_emit(capsys)
    assert payload["count"] == 2
    assert payload["total"] == 5  # filter 后 total 是过滤后总数
    assert all(e["kind"] == "phase.exit" for e in payload["events"])


def test_cli_agg_writes_snapshot(isolate_telemetry: Path, capsys):
    _telemetry.emit_event({"kind": "phase.exit", "proj_key": "p", "phase": "implement",
                           "status": "done", "duration_ms": 10})
    args = argparse.Namespace(by="phase", since=None, no_write=False)
    _telemetry.cli_agg(args)
    payload = _read_emit(capsys)
    assert payload["ok"] is True
    assert "implement" in payload["by"]["phase"]
    snap = _telemetry.aggregates_dir() / "by-phase.json"
    assert snap.is_file()
    data = json.loads(snap.read_text())
    assert "data" in data and "implement" in data["data"]


def test_cli_agg_no_write(isolate_telemetry: Path, capsys):
    _telemetry.emit_event({"kind": "phase.exit", "proj_key": "p", "phase": "implement",
                           "status": "done", "duration_ms": 10})
    args = argparse.Namespace(by="phase", since=None, no_write=True)
    _telemetry.cli_agg(args)
    snap = _telemetry.aggregates_dir() / "by-phase.json"
    assert not snap.exists()


def test_cli_hotspots(isolate_telemetry: Path, capsys):
    _telemetry.emit_event({"kind": "phase.exit", "proj_key": "p", "phase": "fix-r0",
                           "status": "failed", "duration_ms": 1000, "retry_count": 1})
    args = argparse.Namespace(top=3, since=None)
    _telemetry.cli_hotspots(args)
    payload = _read_emit(capsys)
    assert payload["ok"] is True
    assert payload["events_considered"] == 1
    assert payload["hotspots"][0]["phase"] == "fix-r0"


def test_cli_estimate_tokens(tmp_path: Path, capsys):
    f = tmp_path / "p.md"
    f.write_text("x" * 800, encoding="utf-8")
    args = argparse.Namespace(file=str(f))
    _telemetry.cli_estimate_tokens(args)
    payload = _read_emit(capsys)
    assert payload["bytes"] == 800
    assert payload["est_tokens"] == 200


def test_cli_estimate_tokens_missing(capsys, tmp_path: Path):
    args = argparse.Namespace(file=str(tmp_path / "nope.md"))
    with pytest.raises(SystemExit):
        _telemetry.cli_estimate_tokens(args)
    payload = _read_emit(capsys)
    assert payload["error"] == "file_not_found"


# ============================================================
# 集成：events.phase_exit 自动 emit
# ============================================================


def _bootstrap(env_setup, capsys, make_args, *change_ids: str) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids))))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def test_events_phase_exit_emits_telemetry(env_setup, capsys, make_args, isolate_telemetry):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _events.phase_enter(make_args(seq=1, phase="implement"))
    capsys.readouterr()
    _events.phase_exit(make_args(seq=1, phase="implement", status="done", extra='{"commit":"abc"}'))
    capsys.readouterr()

    ep = _telemetry.events_path()
    assert ep.is_file()
    records = [json.loads(l) for l in ep.read_text().splitlines()]
    assert len(records) == 1
    r = records[0]
    assert r["kind"] == "phase.exit"
    assert r["phase"] == "implement"
    assert r["status"] == "done"
    assert r["change_id"] == "add-foo"
    assert r["proj_key"] == env_setup.proj_key
    assert r["run_ts"] == env_setup.run_ts
    assert "pointer" in r


def test_events_phase_exit_review_skipped(env_setup, capsys, make_args, isolate_telemetry):
    """review-rN 的 phase.exit 不应被低层 emit（由 review.round 接管）。"""
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _events.phase_enter(make_args(seq=1, phase="review-r0"))
    capsys.readouterr()
    _events.phase_exit(make_args(seq=1, phase="review-r0", status="done", extra="{}"))
    capsys.readouterr()
    ep = _telemetry.events_path()
    assert not ep.exists() or ep.read_text().strip() == ""


def test_events_phase_rotate_emits_telemetry(env_setup, capsys, make_args, isolate_telemetry):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _events.phase_enter(make_args(seq=1, phase="implement"))
    capsys.readouterr()
    _events.phase_rotate(make_args(
        seq=1, to_phase="fix-r0", prev_status="done", prev_extra='{"commit":"abc"}',
    ))
    capsys.readouterr()
    ep = _telemetry.events_path()
    records = [json.loads(l) for l in ep.read_text().splitlines()]
    # rotate 关闭 implement → 1 条 phase.exit；新 phase fix-r0 不在 rotate 里发 telemetry
    assert len(records) == 1
    assert records[0]["phase"] == "implement"
    assert records[0]["status"] == "done"
