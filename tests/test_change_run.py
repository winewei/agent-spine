"""change run（v1.5 内环下沉）状态机测试。

全程不真实调用 coder/review/archive——通过 monkeypatch 替换 change 模块引用的
_coder.run_implement / _pipeline.run_review_round / _coder.run_fix /
_pipeline.run_archive，用脚本化的返回序列驱动状态机。
"""

from __future__ import annotations

import json

import pytest

from npc import change as _change
from npc import state as _state


# ============================================================
# Helpers
# ============================================================


def _bootstrap_run(make_args, capsys, *change_ids: str, goal: str | None = None) -> None:
    _state.init_run(make_args(plan_order=json.dumps(list(change_ids)), goal=goal))
    capsys.readouterr()
    for i, cid in enumerate(change_ids, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


class Script:
    """脚本化 fake：按调用顺序弹出预设返回值，并记录调用。"""

    def __init__(self, results: list[dict]):
        self.results = list(results)
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.results:
            raise AssertionError("fake 被调用次数超过预设结果数")
        return self.results.pop(0)


def _patch(monkeypatch, *, implement=None, review=None, fix=None, archive=None):
    if implement is not None:
        monkeypatch.setattr(_change._coder, "run_implement", implement)
    if review is not None:
        monkeypatch.setattr(_change._pipeline, "run_review_round", review)
    if fix is not None:
        monkeypatch.setattr(_change._coder, "run_fix", fix)
    if archive is not None:
        monkeypatch.setattr(_change._pipeline, "run_archive", archive)


def _set_entry(p, seq: int, **fields) -> None:
    def mut(state: dict) -> None:
        state["progress"][seq - 1].update(fields)

    _state.update_state(p.state_json, p.state_md, mut)


def _read_entry(p, seq: int) -> dict:
    return _state.read_state(p.state_json)["progress"][seq - 1]


OK_IMPL = {"ok": True, "commit": "abc", "tests": "pass"}
OK_ARCHIVE = {"ok": True, "archive_commit": "arc123", "total_rounds": 0}


def review_result(blocking: int, stale: bool = False) -> dict:
    return {"ok": True, "blocking": blocking, "stale": stale, "verdict": "x"}


# ============================================================
# happy path
# ============================================================


def test_clean_first_review_archives(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    impl = Script([OK_IMPL])
    rev = Script([review_result(0)])
    arc = Script([dict(OK_ARCHIVE)])
    _patch(monkeypatch, implement=impl, review=rev, fix=Script([]), archive=arc)

    out = _change.run_change(p, 1)
    assert out["ok"] is True
    assert out["status"] == "archived"
    assert out["archive_commit"] == "arc123"
    assert len(impl.calls) == 1 and len(rev.calls) == 1 and len(arc.calls) == 1


def test_fix_loop_until_clean(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    rev = Script([review_result(2), review_result(1), review_result(0)])
    fix = Script([{"ok": True}, {"ok": True}])
    _patch(
        monkeypatch,
        implement=Script([OK_IMPL]),
        review=rev,
        fix=fix,
        archive=Script([dict(OK_ARCHIVE)]),
    )

    out = _change.run_change(p, 1)
    assert out["status"] == "archived"
    # review r0/r1/r2；fix r1/r2（round 参数是位置参数第 4 个）
    fix_rounds = [c[0][3] for c in fix.calls]
    assert fix_rounds == [1, 2]


# ============================================================
# 决策点：交互档 needs-decision + --decision 续跑
# ============================================================


def test_stale_interactive_exits_needs_decision(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _patch(
        monkeypatch,
        implement=Script([OK_IMPL]),
        review=Script([review_result(3, stale=True)]),
        fix=Script([]),
        archive=Script([]),
    )

    out = _change.run_change(p, 1)
    assert out["ok"] is False
    assert out["status"] == "needs-decision"
    assert out["trigger"] == "stale"
    assert out["suggested"] in ("skip", "force-archive", "continue-retry")
    entry = _read_entry(p, 1)
    assert entry["pending_decision"]["trigger"] == "stale"
    assert entry["status"] == "needs-user-decision"


def test_decision_skip_consumes_pending(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _set_entry(
        p, 1,
        pending_decision={"trigger": "stale", "phase": "review", "round": 3, "suggested": "skip"},
        status="needs-user-decision",
    )
    _patch(monkeypatch, implement=Script([]), review=Script([]), fix=Script([]), archive=Script([]))

    out = _change.run_change(p, 1, decision="skip")
    assert out["status"] == "skipped"
    entry = _read_entry(p, 1)
    assert "pending_decision" not in entry
    assert entry["status"] == "skipped-auto"
    assert entry["reason"].startswith("user-skip")


def test_decision_force_archive(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _set_entry(
        p, 1,
        pending_decision={"trigger": "stale", "phase": "review", "round": 2, "suggested": "skip"},
        status="needs-user-decision",
    )
    arc = Script([dict(OK_ARCHIVE)])
    _patch(monkeypatch, implement=Script([]), review=Script([]), fix=Script([]), archive=arc)

    out = _change.run_change(p, 1, decision="force-archive")
    assert out["status"] == "archived"
    assert len(arc.calls) == 1


def test_decision_continue_retry_resumes_fix(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _set_entry(
        p, 1,
        pending_decision={"trigger": "fixer-failed", "phase": "fix", "round": 2, "suggested": "skip"},
        status="needs-user-decision",
    )
    fix = Script([{"ok": True}])
    _patch(
        monkeypatch,
        implement=Script([]),
        review=Script([review_result(0)]),
        fix=fix,
        archive=Script([dict(OK_ARCHIVE)]),
    )

    out = _change.run_change(p, 1, decision="continue-retry")
    assert out["status"] == "archived"
    assert fix.calls[0][0][3] == 2  # 从 fix-r2 重试


def test_decision_without_pending_is_usage_error(env_setup, make_args, capsys):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    with pytest.raises(_change.UsageError):
        _change.run_change(p, 1, decision="skip")


def test_unconsumed_pending_blocks_rerun(env_setup, make_args, capsys):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _set_entry(p, 1, pending_decision={"trigger": "stale", "phase": "review", "round": 1})
    with pytest.raises(_change.UsageError):
        _change.run_change(p, 1)


# ============================================================
# 决策点：auto 档
# ============================================================


def test_auto_stale_low_blocking_force_archives(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    # blocking_trend 末值 2 且长度 >=3 → auto-decide force-archive
    _set_entry(p, 1, blocking_trend=[4, 2, 2])
    arc = Script([dict(OK_ARCHIVE)])
    _patch(
        monkeypatch,
        implement=Script([OK_IMPL]),
        review=Script([review_result(2, stale=True)]),
        fix=Script([]),
        archive=arc,
    )

    out = _change.run_change(p, 1, auto=True)
    assert out["status"] == "archived"
    assert len(arc.calls) == 1


def test_auto_implementer_failed_retry_then_skip(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    impl = Script([{"ok": False, "error": "boom"}, {"ok": False, "error": "boom2"}])
    _patch(monkeypatch, implement=impl, review=Script([]), fix=Script([]), archive=Script([]))

    out = _change.run_change(p, 1, auto=True)
    assert out["ok"] is False
    assert out["status"] == "skipped"
    assert len(impl.calls) == 2  # 第一次失败 → continue-retry；第二次失败 → skip
    entry = _read_entry(p, 1)
    assert entry["status"] == "skipped-auto"


def test_auto_archive_failure_is_terminal_failed(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _patch(
        monkeypatch,
        implement=Script([OK_IMPL]),
        review=Script([review_result(0)]),
        fix=Script([]),
        archive=Script([{"ok": False, "error": "commit-chain-broken"}]),
    )

    out = _change.run_change(p, 1, auto=True)
    assert out["status"] == "failed"
    assert out["error"] == "commit-chain-broken"


def test_auto_from_state_mode(env_setup, make_args, capsys, monkeypatch):
    """state.mode=auto 时无需 --auto 标志也走 auto 档。"""
    p = env_setup
    monkeypatch.setenv("NPC_MODE", "auto")
    _bootstrap_run(make_args, capsys, "add-foo")
    impl = Script([{"ok": False, "error": "x"}, {"ok": False, "error": "x"}])
    _patch(monkeypatch, implement=impl, review=Script([]), fix=Script([]), archive=Script([]))

    out = _change.run_change(p, 1)
    assert out["status"] == "skipped"  # 而不是 needs-decision


# ============================================================
# 起点推导 / 终态保护 / max-rounds
# ============================================================


def test_derive_start_from_status():
    assert _change.derive_start({"status": "pending"}, None) == ("implement", 0)
    assert _change.derive_start(
        {"status": "reviewing", "blocking_trend": []}, None
    ) == ("review", 0)
    assert _change.derive_start(
        {"status": "in-fix-loop", "blocking_trend": [3, 2]}, None
    ) == ("review", 2)
    assert _change.derive_start({"status": "pending"}, "archive") == ("archive", 0)
    assert _change.derive_start(
        {"status": "reviewing", "blocking_trend": [2]}, "fix"
    ) == ("fix", 1)


def test_terminal_status_requires_from(env_setup, make_args, capsys):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _set_entry(p, 1, status="archived")
    with pytest.raises(_change.UsageError):
        _change.run_change(p, 1)


def test_max_rounds_triggers_decision(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _patch(
        monkeypatch,
        implement=Script([OK_IMPL]),
        review=Script([review_result(5)]),
        fix=Script([]),
        archive=Script([]),
    )

    out = _change.run_change(p, 1, max_rounds=0)
    assert out["status"] == "needs-decision"
    assert out["trigger"] == "max-rounds"


def test_review_engine_failure_interactive(env_setup, make_args, capsys, monkeypatch):
    p = env_setup
    _bootstrap_run(make_args, capsys, "add-foo")
    _patch(
        monkeypatch,
        implement=Script([OK_IMPL]),
        review=Script([{"ok": False, "error": "codex-exec-failed", "detail": "boom"}]),
        fix=Script([]),
        archive=Script([]),
    )

    out = _change.run_change(p, 1)
    assert out["status"] == "needs-decision"
    assert out["trigger"] == "codex-failed"
