"""v1.5 特性测试：state note / status --brief / verify tasks / deviation 记账 / goal coverage。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import state as _state
from npc import status as _status
from npc import summary as _summary
from npc import telemetry as _telemetry
from npc import verify as _verify
from npc import auto_decide as _auto


def _bootstrap_run(make_args, capsys, *change_ids: str, goal: str | None = None) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids)), goal=goal))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def _emit_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# ============================================================
# state note + status --brief
# ============================================================


def test_note_append_and_brief_roundtrip(env_setup, make_args, capsys):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", goal="给认证模块加限流")

    _state.note(make_args(text="wave2 前先复核 rate-limit 的依赖边", consume=False, source=None))
    out = _emit_json(capsys)
    assert out["ok"] is True
    assert Path(out["path"]).is_file()

    _status.run(make_args(brief=True))
    brief = _emit_json(capsys)
    assert brief["ok"] is True
    assert brief["goal"] == "给认证模块加限流"
    assert len(brief["notes"]) == 1
    assert "rate-limit" in brief["notes"][0]["text"]
    assert "changes" not in brief  # brief 不带全列表
    assert brief["next_action"].startswith("npc change run --seq 1")

    # 消费水位后 brief 不再带出
    _state.note(make_args(text=None, consume=True))
    _emit_json(capsys)
    _status.run(make_args(brief=True))
    brief2 = _emit_json(capsys)
    assert brief2["notes"] == []


def test_note_usage_error(env_setup, make_args, capsys):
    _bootstrap_run(make_args, capsys, "add-foo")
    with pytest.raises(SystemExit) as ei:
        _state.note(make_args(text=None, consume=False))
    assert ei.value.code == 2
    with pytest.raises(SystemExit) as ei:
        _state.note(make_args(text="x", consume=True))
    assert ei.value.code == 2


def test_brief_surfaces_pending_decision(env_setup, make_args, capsys):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", "add-bar")

    def mut(state: dict) -> None:
        state["progress"][0]["pending_decision"] = {
            "trigger": "stale",
            "round": 3,
            "suggested": "skip",
        }
        state["progress"][0]["status"] = "needs-user-decision"

    _state.update_state(p.state_json, p.state_md, mut)
    _status.run(make_args(brief=True))
    brief = _emit_json(capsys)
    pds = brief["pending_decisions"]
    assert len(pds) == 1
    assert pds[0]["trigger"] == "stale" and pds[0]["suggested"] == "skip"
    assert "--decision" in brief["next_action"]


def test_brief_next_action_finalize_when_all_terminal(env_setup, make_args, capsys):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")

    def mut(state: dict) -> None:
        state["progress"][0]["status"] = "archived"

    _state.update_state(p.state_json, p.state_md, mut)
    _status.run(make_args(brief=True))
    brief = _emit_json(capsys)
    assert "finalize" in brief["next_action"]


# ============================================================
# verify tasks
# ============================================================


def _write_tasks_md(repo: Path, change: str, text: str) -> None:
    d = repo / "openspec" / "changes" / change
    d.mkdir(parents=True)
    (d / "tasks.md").write_text(text)


TASKS_MD = """# Tasks
- [x] 1.1 写解析器
- [x] 1.2 写渲染器
- [ ] 1.3 补测试
"""


def test_verify_tasks_counts(fake_repo, make_args, capsys, monkeypatch):
    monkeypatch.chdir(fake_repo)
    _write_tasks_md(fake_repo, "add-foo", TASKS_MD)
    _verify.run_tasks_check(make_args(change="add-foo", seq=None))
    out = _emit_json(capsys)
    assert out == {
        "ok": True,
        "change": "add-foo",
        "tasks_done": 2,
        "tasks_total": 3,
        "claim": None,
        "consistent": None,
    }


def test_verify_tasks_cross_check_inconsistent(
    env_setup, fake_repo, make_args, capsys, monkeypatch
):
    p = env_setup
    monkeypatch.chdir(fake_repo)
    _bootstrap_run(make_args, capsys, "add-foo")
    _write_tasks_md(fake_repo, "add-foo", TASKS_MD)

    def mut(state: dict) -> None:
        state["progress"][0]["phases"] = {"implement": {"status": "done", "tasks": 3}}

    _state.update_state(p.state_json, p.state_md, mut)
    with pytest.raises(SystemExit) as ei:
        _verify.run_tasks_check(make_args(change="add-foo", seq=1))
    assert ei.value.code == 1
    out = _emit_json(capsys)
    assert out["claim"] == 3 and out["tasks_done"] == 2 and out["consistent"] is False


def test_verify_tasks_missing_file(fake_repo, make_args, capsys, monkeypatch):
    monkeypatch.chdir(fake_repo)
    with pytest.raises(SystemExit) as ei:
        _verify.run_tasks_check(make_args(change="ghost", seq=None))
    assert ei.value.code == 3


# ============================================================
# deviation 记账
# ============================================================


def _read_telemetry(tel_root: Path) -> list[dict]:
    f = tel_root / "events.ndjson"
    if not f.is_file():
        return []
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()]


def test_auto_decide_apply_emits_deviation(
    env_setup, make_args, capsys, isolate_telemetry
):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")

    def mut(state: dict) -> None:
        state["progress"][0]["blocking_trend"] = [5, 5, 5, 5]

    _state.update_state(p.state_json, p.state_md, mut)
    _auto.cli(make_args(seq=1, trigger="stale", apply=True))
    out = _emit_json(capsys)
    assert out["applied"] is True

    devs = [r for r in _read_telemetry(isolate_telemetry) if r["kind"] == "deviation"]
    assert len(devs) == 1
    d = devs[0]
    assert d["trigger"] == "stale" == d["outcome_reason"]
    assert d["action"] == out["action"]
    assert d["decided_by"] == "auto"
    assert d["cost_rounds"] == 4


def test_auto_decide_without_apply_no_deviation(
    env_setup, make_args, capsys, isolate_telemetry
):
    _bootstrap_run(make_args, capsys, "add-foo")
    _auto.cli(make_args(seq=1, trigger="stale", apply=False))
    capsys.readouterr()
    assert [r for r in _read_telemetry(isolate_telemetry) if r["kind"] == "deviation"] == []


def test_deviation_aggregates_by_phase(isolate_telemetry):
    _telemetry.emit_deviation(
        proj_key="k", run_ts="t", change_seq=1, change_id="c",
        trigger="cherry-pick-conflict", action="abort", phase="integrate",
        layer="decompose",
    )
    events = _read_telemetry(isolate_telemetry)
    agg = _telemetry.aggregate(events, by="phase")
    assert "integrate" in agg
    assert agg["integrate"]["reasons"] == {"cherry-pick-conflict": 1}


# ============================================================
# goal coverage（summary render）
# ============================================================


def test_summary_renders_goal_coverage(env_setup, make_args, capsys):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo", "add-bar", goal="统一鉴权中间件")

    def mut(state: dict) -> None:
        state["progress"][0]["status"] = "archived"
        state["progress"][1]["status"] = "skipped-auto"

    _state.update_state(p.state_json, p.state_md, mut)
    _summary.render(make_args())
    out = _emit_json(capsys)
    text = Path(out["output"]).read_text()
    assert "## Goal Coverage" in text
    assert "统一鉴权中间件" in text
    assert "| 1 | add-foo | archived |" in text
    assert "| 2 | add-bar | skipped-auto |" in text


def test_summary_no_goal_no_section(env_setup, make_args, capsys):
    _bootstrap_run(make_args, capsys, "add-foo")
    _summary.render(make_args())
    out = _emit_json(capsys)
    assert "## Goal Coverage" not in Path(out["output"]).read_text()
