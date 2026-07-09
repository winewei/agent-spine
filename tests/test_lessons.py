"""``npc lessons record`` / ``npc lessons gate`` 测试。

覆盖 run-lessons-feedforward 的三 capability：

- run-lessons-extraction：多轮 fix 拼接、无 fix 轮不追加、幂等、字段降级、events 缺失错误契约、
  不读 reviewer 产出。
- run-lessons-injection：lessons.md 不存在/空文件不注入、非空注入指针 bullet 且不内联内容。
- pilot-rewrite-gate：候选集判定（层号/status 过滤）、游标去重、apply 落盘、targets 校验拒绝、
  spec write --lessons-path 段落存在性、省略时逐字等价。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from npc import lessons as _lessons, paths as _paths, state as _state, templates


# ============================================================
# fixtures / helpers
# ============================================================


def _write_state(p: _paths.Paths, progress: list[dict], *, lessons_node: dict | None = None) -> None:
    state = {
        "schema_version": 2,
        "run_ts": p.run_ts,
        "proj_key": p.proj_key,
        "plan_order": [e["change_id"] for e in progress],
        "progress": progress,
    }
    if lessons_node is not None:
        state["lessons"] = lessons_node
    _state.write_state(p.state_json, p.state_md, state)


def _write_events(base: Path, events: list[dict]) -> None:
    base.mkdir(parents=True, exist_ok=True)
    with (base / "events.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _fix_done(round_n: int, *, cats="", regs="", notes="") -> dict:
    return {
        "event": "fix.done",
        "ts": "2026-07-09T00:00:00Z",
        "phase": f"fix-r{round_n}",
        "duration_ms": 1000,
        "categories_scanned": cats,
        "regressions_added": regs,
        "notes": notes,
    }


# ============================================================
# run-lessons-extraction
# ============================================================


def test_multi_round_fix_produces_full_entry(env_setup):
    p = env_setup
    base = _paths.base_for(p, 1, "change-a")
    _write_state(p, [{"seq": 1, "change_id": "change-a", "status": "archived",
                      "archive_commit": "deadbeef1234", "base": str(base), "phases": {}}])
    _write_events(base, [
        _fix_done(1, cats="validation", regs="tests/test_a.py::t1", notes="fixed missing check"),
        _fix_done(2, cats="error-handling,validation", regs="tests/test_b.py::t2", notes="broadened sweep"),
    ])

    res = _lessons.extract_and_append(p, 1)
    assert res["ok"] is True
    assert res["appended"] is True

    text = (p.run_dir / "lessons.md").read_text(encoding="utf-8")
    assert "## change-a (archived deadbeef, 2 fix rounds)" in text
    # 类目去重并集
    assert "categories_scanned: error-handling, validation" in text
    # regressions 并集
    assert "tests/test_a.py::t1" in text and "tests/test_b.py::t2" in text
    # notes 按 round 顺序
    assert "- r1: fixed missing check" in text
    assert "- r2: broadened sweep" in text
    assert text.index("r1:") < text.index("r2:")


def test_extraction_reads_real_phase_exit_emitter(env_setup, make_args):
    """回归：提炼源锚定 npc 真实落盘形态，防实现相对真实 schema 静默漂移。

    不手搓事件 dict，而是通过真实 ``events.phase_exit``（等价 ``npc phase exit fix-rN
    --status done``）生成 ``<base>/events.jsonl``，证明 ``extract_and_append`` 读取的正是
    npc 实际 emit 的 ``event == "fix.done"`` 行——该行**不含** ``kind`` / ``status``
    （spec/design/tasks 曾误写为 ``kind==phase.exit && status==done``，那是另一条 telemetry
    派生流的形态，见 run-lessons-extraction spec 事件契约说明）。
    """
    from npc import events as _events

    p = env_setup
    base = _paths.base_for(p, 1, "change-real")
    base.mkdir(parents=True, exist_ok=True)
    # 先建 fix-r1 in-progress，再走真实 phase exit（模拟 fixer 收敛后 rotate/exit）
    _write_state(p, [{"seq": 1, "change_id": "change-real", "status": "archived",
                      "archive_commit": "cafebabe0001", "base": str(base),
                      "phases": {"fix-r1": {"status": "in-progress",
                                            "started_at": "2026-05-22T00:00:00Z",
                                            "started_ms": 0}}}])
    args = make_args(seq=1, phase="fix-r1", status="done",
                     extra=json.dumps({"categories_scanned": "validation,concurrency",
                                       "regressions_added": "tests/test_real.py::t",
                                       "notes": "real emitter path"}))
    _events.phase_exit(args)

    # 真实落盘断言：event == "fix.done"，行内既无 kind 也无 status
    lines = (base / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ev = json.loads(next(l for l in lines if "fix.done" in l))
    assert ev["event"] == "fix.done"
    assert ev["phase"] == "fix-r1"
    assert "kind" not in ev and "status" not in ev  # 关键：不是 spec 误写的 phase.exit 形态
    assert ev["categories_scanned"] == "validation,concurrency"

    # 提炼确实读到这条真实事件并追加条目
    res = _lessons.extract_and_append(p, 1)
    assert res["ok"] is True and res["appended"] is True
    text = (p.run_dir / "lessons.md").read_text(encoding="utf-8")
    assert "## change-real (archived cafebabe, 1 fix rounds)" in text
    assert "categories_scanned: concurrency, validation" in text
    assert "tests/test_real.py::t" in text
    assert "- r1: real emitter path" in text


def test_telemetry_shaped_phase_exit_not_mistaken_for_fix_round(env_setup):
    """契约边界：``kind==phase.exit && status==done`` 是 telemetry 派生流形态（且不携带
    自报字段），即便误入 ``<base>/events.jsonl`` 也 MUST NOT 被当作 fix 轮——提炼只认
    ``event == "fix.done"``。"""
    p = env_setup
    base = _paths.base_for(p, 1, "change-x")
    _write_state(p, [{"seq": 1, "change_id": "change-x", "status": "archived",
                      "base": str(base), "phases": {}}])
    _write_events(base, [{"kind": "phase.exit", "phase": "fix-r1", "status": "done"}])
    res = _lessons.extract_and_append(p, 1)
    assert res["ok"] is True
    assert res["appended"] is False
    assert res["reason"] == "no-fix-rounds"


def test_no_fix_round_not_appended(env_setup):
    p = env_setup
    base = _paths.base_for(p, 1, "change-b")
    _write_state(p, [{"seq": 1, "change_id": "change-b", "status": "archived",
                      "base": str(base), "phases": {}}])
    # 只有 review 事件，没有 fix.done
    _write_events(base, [{"event": "review.done", "phase": "review-r0", "blocking": 0}])

    res = _lessons.extract_and_append(p, 1)
    assert res["ok"] is True
    assert res["appended"] is False
    assert res["reason"] == "no-fix-rounds"
    assert not (p.run_dir / "lessons.md").exists()


def test_idempotent_repeat_call(env_setup):
    p = env_setup
    base = _paths.base_for(p, 1, "change-a")
    _write_state(p, [{"seq": 1, "change_id": "change-a", "status": "archived",
                      "archive_commit": "abc", "base": str(base), "phases": {}}])
    _write_events(base, [_fix_done(1, cats="validation", notes="n1")])

    r1 = _lessons.extract_and_append(p, 1)
    r2 = _lessons.extract_and_append(p, 1)
    assert r1["appended"] is True
    assert r2["appended"] is False
    assert r2["reason"] == "already-recorded"
    text = (p.run_dir / "lessons.md").read_text(encoding="utf-8")
    assert text.count("## change-a") == 1


def test_all_fields_empty_still_records_rounds(env_setup):
    p = env_setup
    base = _paths.base_for(p, 1, "change-c")
    _write_state(p, [{"seq": 1, "change_id": "change-c", "status": "archived",
                      "base": str(base), "phases": {}}])
    _write_events(base, [_fix_done(1, cats="-", regs="-", notes="-")])

    res = _lessons.extract_and_append(p, 1)
    assert res["appended"] is True
    text = (p.run_dir / "lessons.md").read_text(encoding="utf-8")
    assert "## change-c" in text
    assert "- rounds: 1" in text
    # 空字段子项被省略
    assert "categories_scanned:" not in text
    assert "regressions_added:" not in text
    assert "- notes:" not in text


def test_events_missing_graceful_error(env_setup):
    p = env_setup
    base = _paths.base_for(p, 1, "change-a")
    _write_state(p, [{"seq": 1, "change_id": "change-a", "status": "archived",
                      "base": str(base), "phases": {}}])
    # 不写 events.jsonl
    res = _lessons.extract_and_append(p, 1)
    assert res["ok"] is False
    assert res["error"] == "events-missing"


def test_does_not_read_reviewer_artifacts(env_setup):
    """提炼路径不打开 review.json；lessons.md 不含 finding 原文。"""
    p = env_setup
    base = _paths.base_for(p, 1, "change-a")
    _write_state(p, [{"seq": 1, "change_id": "change-a", "status": "archived",
                      "base": str(base), "phases": {}}])
    _write_events(base, [_fix_done(1, cats="validation", notes="self-reported note")])
    # 埋一份含 finding 原文的 review.json，验证不被读入
    secret = "SECRET_REVIEWER_FINDING_TEXT"
    (base / "round-1.review.json").write_text(
        json.dumps({"findings": [{"title": secret}]}), encoding="utf-8"
    )

    res = _lessons.extract_and_append(p, 1)
    assert res["appended"] is True
    text = (p.run_dir / "lessons.md").read_text(encoding="utf-8")
    assert secret not in text


def test_corrupt_line_skipped_no_throw(env_setup):
    p = env_setup
    base = _paths.base_for(p, 1, "change-a")
    _write_state(p, [{"seq": 1, "change_id": "change-a", "status": "archived",
                      "base": str(base), "phases": {}}])
    base.mkdir(parents=True, exist_ok=True)
    with (base / "events.jsonl").open("w", encoding="utf-8") as f:
        f.write("{ this is not valid json\n")
        f.write(json.dumps(_fix_done(1, cats="validation", notes="ok")) + "\n")

    res = _lessons.extract_and_append(p, 1)
    assert res["ok"] is True
    assert res["appended"] is True
    text = (p.run_dir / "lessons.md").read_text(encoding="utf-8")
    assert "validation" in text


def test_entries_appended_recorded_in_state(env_setup):
    p = env_setup
    base = _paths.base_for(p, 1, "change-a")
    _write_state(p, [{"seq": 1, "change_id": "change-a", "status": "archived",
                      "base": str(base), "phases": {}}])
    _write_events(base, [_fix_done(1, cats="validation")])
    _lessons.extract_and_append(p, 1)
    state = _state.read_state(p.state_json)
    assert "change-a" in state["lessons"]["entries_appended"]


# ============================================================
# run-lessons-injection (render_implementer)
# ============================================================


def test_implementer_no_lessons_byte_equivalent():
    without = templates.render_implementer("cid", "/base", "/repo")
    explicit_none = templates.render_implementer("cid", "/base", "/repo", lessons_path=None)
    assert without == explicit_none
    assert "lessons.md" not in without


def test_implementer_with_lessons_has_pointer_only():
    text = templates.render_implementer(
        "cid", "/base", "/repo", lessons_path="/run/lessons.md"
    )
    assert "/run/lessons.md" in text
    assert "仅供参考" in text
    # 指针出现在必读输入段
    assert "## 必读输入" in text


def test_resolve_lessons_path_empty_file_treated_absent(env_setup):
    from npc import coder as _coder
    p = env_setup
    lp = p.run_dir / "lessons.md"
    lp.parent.mkdir(parents=True, exist_ok=True)
    # 不存在
    assert _coder._resolve_lessons_path(p) is None
    # 空文件视同不存在
    lp.write_text("", encoding="utf-8")
    assert _coder._resolve_lessons_path(p) is None
    # 非空
    lp.write_text("## x\n", encoding="utf-8")
    assert _coder._resolve_lessons_path(p) == str(lp.resolve())


# ============================================================
# pilot-rewrite-gate
# ============================================================


def _gate_state(p, *, lessons_entries=0, cursor=0):
    progress = [
        {"seq": 1, "change_id": "c0", "status": "archived", "dag_layer": 0, "phases": {}},
        {"seq": 2, "change_id": "c1", "status": "pending", "dag_layer": 1, "phases": {}},
        {"seq": 3, "change_id": "c2", "status": "pending", "dag_layer": 2, "phases": {}},
        {"seq": 4, "change_id": "c3", "status": "in-fix-loop", "dag_layer": 1, "phases": {}},
    ]
    _write_state(p, progress, lessons_node={"entries_appended": [], "gate_processed_cursor": cursor,
                                            "gate_decisions": []})
    if lessons_entries:
        lp = p.run_dir / "lessons.md"
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text("\n".join(f"## e{i} (1 fix rounds)\n- rounds: 1" for i in range(lessons_entries)) + "\n",
                      encoding="utf-8")


def test_gate_candidates_layer_and_status_filter(env_setup):
    p = env_setup
    _gate_state(p, lessons_entries=2, cursor=0)
    res = _lessons.gate_candidates(p, 0)
    assert res["has_candidates"] is True
    # 层号 > 0 且 pending：c1, c2；c3 是 in-fix-loop 被排除
    assert res["candidates"] == ["c1", "c2"]


def test_gate_short_circuits_without_new_entries(env_setup):
    p = env_setup
    # 2 条 lessons，但游标已到 2 → 无新增
    _gate_state(p, lessons_entries=2, cursor=2)
    res = _lessons.gate_candidates(p, 0)
    assert res["has_candidates"] is False


def test_gate_started_downstream_excluded(env_setup):
    p = env_setup
    progress = [
        {"seq": 1, "change_id": "c0", "status": "archived", "dag_layer": 0, "phases": {}},
        {"seq": 2, "change_id": "c-d", "status": "implementing", "dag_layer": 1,
         "implement_commit": "x", "phases": {}},
    ]
    _write_state(p, progress, lessons_node={"entries_appended": [], "gate_processed_cursor": 0,
                                            "gate_decisions": []})
    lp = p.run_dir / "lessons.md"
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text("## e0 (1 fix rounds)\n- rounds: 1\n", encoding="utf-8")
    res = _lessons.gate_candidates(p, 0)
    assert "c-d" not in res["candidates"]
    assert res["has_candidates"] is False  # 无候选


def test_gate_apply_advances_cursor(env_setup):
    p = env_setup
    _gate_state(p, lessons_entries=2, cursor=0)
    r = _lessons.apply_gate_decision(p, 0, [], "skip-rewrite")
    assert r["ok"] is True
    assert r["cursor"] == 2
    # 再判定：无新增 → false
    res = _lessons.gate_candidates(p, 0)
    assert res["has_candidates"] is False
    state = _state.read_state(p.state_json)
    decs = state["lessons"]["gate_decisions"]
    assert len(decs) == 1 and decs[0]["decision"] == "skip-rewrite"


def test_gate_apply_rewrite_valid_targets(env_setup):
    p = env_setup
    _gate_state(p, lessons_entries=1, cursor=0)
    r = _lessons.apply_gate_decision(p, 0, ["c1"], "rewrite")
    assert r["ok"] is True
    state = _state.read_state(p.state_json)
    assert state["lessons"]["gate_decisions"][0]["targets"] == ["c1"]


def test_gate_apply_rewrite_rejects_non_candidate(env_setup):
    p = env_setup
    _gate_state(p, lessons_entries=1, cursor=0)
    # c3 是 in-fix-loop（非 pending）→ 不属候选集
    r = _lessons.apply_gate_decision(p, 0, ["c3"], "rewrite")
    assert r["ok"] is False
    assert r["error"] == "target-not-candidate"
    state = _state.read_state(p.state_json)
    # 不落决策、不推进游标
    assert state["lessons"]["gate_decisions"] == []
    assert state["lessons"]["gate_processed_cursor"] == 0


def test_gate_apply_rewrite_rejects_unknown_change(env_setup):
    p = env_setup
    _gate_state(p, lessons_entries=1, cursor=0)
    r = _lessons.apply_gate_decision(p, 0, ["no-such-change"], "rewrite")
    assert r["ok"] is False
    assert r["error"] == "target-not-candidate"


# ============================================================
# spec write --lessons-path 注入
# ============================================================


def test_spec_writer_without_lessons_byte_equivalent():
    without = templates.render_spec_writer("cid", "/base", "/repo", goal="do a thing")
    explicit = templates.render_spec_writer("cid", "/base", "/repo", goal="do a thing", lessons_path=None)
    assert without == explicit
    assert "lessons.md" not in without


def test_spec_writer_with_lessons_has_independent_section():
    text = templates.render_spec_writer(
        "cid", "/base", "/repo", goal="do a thing", lessons_path="/run/lessons.md"
    )
    # goal 与 lessons 段落并列
    assert "用户原始目标" in text
    assert "同 run 前置 change 失败模式" in text
    assert "/run/lessons.md" in text


def test_spec_writer_lessons_without_goal():
    text = templates.render_spec_writer("cid", "/base", "/repo", lessons_path="/run/lessons.md")
    assert "同 run 前置 change 失败模式" in text
    assert "用户原始目标" not in text
