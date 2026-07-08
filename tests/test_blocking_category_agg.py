"""blocking_categories 进入聚合与 hotspots 的回归。

对应 openspec change: aggregate-blocking-categories。
"""

from __future__ import annotations

from npc import telemetry as _telemetry


def _seed_events(events: list[dict]) -> None:
    for e in events:
        _telemetry.emit_event(e)


def test_aggregate_by_phase_blocking_categories(isolate_telemetry):
    _seed_events([
        {"kind": "review.round", "proj_key": "p", "phase": "review-r1",
         "round": 1, "status": "done", "verdict": "must-fix",
         "blocking_count": 2, "blocking_categories": ["correctness", "perf"]},
        {"kind": "review.round", "proj_key": "p", "phase": "review-r1",
         "round": 1, "status": "done", "verdict": "must-fix",
         "blocking_count": 1, "blocking_categories": ["correctness"]},
    ])
    out = _telemetry.aggregate(_telemetry.iter_events(), by="phase")
    # 与 top_reasons/top_verdicts 一致：list of (category, count) pairs
    tbc = dict(out["review-r1"]["top_blocking_categories"])
    assert tbc["correctness"] == 2
    assert tbc["perf"] == 1


def test_aggregate_by_change_blocking_categories(isolate_telemetry):
    _seed_events([
        {"kind": "review.round", "proj_key": "p", "change_id": "add-foo",
         "change_seq": 1, "phase": "review-r0", "round": 0, "status": "done",
         "blocking_categories": ["security"]},
    ])
    out = _telemetry.aggregate(_telemetry.iter_events(), by="change")
    key = "001-add-foo"
    assert dict(out[key]["top_blocking_categories"])["security"] == 1


def test_aggregate_missing_blocking_categories_ok(isolate_telemetry):
    _seed_events([
        {"kind": "phase.exit", "proj_key": "p", "phase": "implement",
         "status": "done", "duration_ms": 1000},
    ])
    out = _telemetry.aggregate(_telemetry.iter_events(), by="phase")
    # 无贡献但字段存在且为空
    assert out["implement"]["top_blocking_categories"] == []


def test_hotspots_surface_blocking_categories(isolate_telemetry):
    _seed_events([
        {"kind": "review.round", "proj_key": "p", "phase": "review-r2",
         "round": 2, "status": "done", "duration_ms": 40000, "retry_count": 1,
         "blocking_count": 3, "blocking_categories": ["correctness", "correctness", "perf"]},
    ])
    hs = _telemetry.hotspots(_telemetry.iter_events(), top=5)
    r2 = next(h for h in hs if h["phase"] == "review-r2")
    assert dict(r2["top_blocking_categories"])["correctness"] >= 1
