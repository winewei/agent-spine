"""trend 模块测试。"""

from __future__ import annotations

import json

import pytest

from npc import state as _state, trend as _trend


def _bootstrap(env_setup, capsys, make_args, *changes):
    _state.init_run(make_args(plan_order=json.dumps(list(changes))))
    capsys.readouterr()
    for i, cid in enumerate(changes, start=1):
        _state.add_change(make_args(seq=i, change_id=cid, base=None))
        capsys.readouterr()


def _update_with(seq, blocking, categories, make_args):
    metrics = {"blocking": blocking, "categories": categories}
    _trend.update_trend(make_args(seq=seq, metrics=json.dumps(metrics)))


def test_first_round_baseline(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _update_with(1, 5, ["validation"], make_args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["blocking_trend"] == [5]
    assert payload["rounds_since_strict_decrease"] == 0
    assert payload["categories_seen"] == ["validation"]


def test_strict_decrease_resets_counter(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _update_with(1, 5, ["validation"], make_args)
    capsys.readouterr()
    _update_with(1, 3, ["concurrency"], make_args)  # 严格下降
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["blocking_trend"] == [5, 3]
    assert payload["rounds_since_strict_decrease"] == 0
    assert payload["categories_seen"] == ["validation", "concurrency"]


def test_stalemate_increments_counter(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _update_with(1, 5, [], make_args)
    capsys.readouterr()
    _update_with(1, 5, [], make_args)
    capsys.readouterr()
    _update_with(1, 5, [], make_args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["blocking_trend"] == [5, 5, 5]
    assert payload["rounds_since_strict_decrease"] == 2


def test_increase_increments_counter(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _update_with(1, 4, [], make_args)
    capsys.readouterr()
    _update_with(1, 5, [], make_args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["rounds_since_strict_decrease"] == 1


def test_categories_dedup_preserve_order(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _update_with(1, 5, ["validation", "concurrency"], make_args)
    capsys.readouterr()
    _update_with(1, 4, ["concurrency", "transaction"], make_args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["categories_seen"] == ["validation", "concurrency", "transaction"]


def test_check_stale_false_below_threshold(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _update_with(1, 5, [], make_args)
    capsys.readouterr()
    _update_with(1, 5, [], make_args)
    capsys.readouterr()
    _trend.check_stale(make_args(seq=1))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["stale"] is False
    assert payload["rounds_since_strict_decrease"] == 1


def test_check_stale_true_at_threshold(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    _update_with(1, 5, [], make_args)
    capsys.readouterr()
    _update_with(1, 5, [], make_args)
    capsys.readouterr()
    _update_with(1, 5, [], make_args)
    capsys.readouterr()
    _update_with(1, 5, [], make_args)  # rsd=3
    capsys.readouterr()
    _trend.check_stale(make_args(seq=1))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["stale"] is True
    assert payload["rounds_since_strict_decrease"] == 3
    assert payload["blocking_trend"] == [5, 5, 5, 5]


def test_update_trend_invalid_metrics(env_setup, capsys, make_args):
    _bootstrap(env_setup, capsys, make_args, "add-foo")
    with pytest.raises(SystemExit):
        _trend.update_trend(make_args(seq=1, metrics='{"blocking":"not-int"}'))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["error"] == "invalid_metrics"


# ============================================================
# change fix-prompt-exhaustive-sweep：连续计数 + 复现判定纯函数
# ============================================================


def _review(cats):
    return {"categories": list(cats)}


def _fix(scanned):
    return {"categories_scanned": scanned}


def test_category_streaks_empty_when_no_reviews():
    assert _trend.category_streaks({}) == {}
    assert _trend.category_streaks({"fix-r1": _fix("validation")}) == {}


def test_category_streaks_consecutive_two_and_three():
    phases = {
        "review-r0": _review(["error-handling"]),
        "review-r1": _review(["error-handling"]),
    }
    assert _trend.category_streaks(phases) == {"error-handling": 2}

    phases["review-r2"] = _review(["error-handling"])
    assert _trend.category_streaks(phases) == {"error-handling": 3}


def test_category_streaks_interrupt_resets_to_one():
    # r0 出现、r1 未出现、r2 又出现 → streak=1（不是 3）
    phases = {
        "review-r0": _review(["error-handling"]),
        "review-r1": _review(["validation"]),
        "review-r2": _review(["error-handling"]),
    }
    assert _trend.category_streaks(phases)["error-handling"] == 1


def test_category_streaks_only_counts_latest_round_categories():
    # error-handling 只在 r0 出现、r1 缺席 → 不在最近一轮，不计入结果
    phases = {
        "review-r0": _review(["error-handling"]),
        "review-r1": _review(["validation"]),
    }
    out = _trend.category_streaks(phases)
    assert "error-handling" not in out
    assert out["validation"] == 1


def test_recurred_categories_cross_round_hit():
    # fix-r1 自报 error-handling，review-r1（M=1 ≥ N=1）再次 blocking → 复现
    phases = {
        "review-r0": _review(["error-handling"]),
        "fix-r1": _fix("error-handling"),
        "review-r1": _review(["error-handling"]),
    }
    out = _trend.recurred_categories(phases)
    assert out == [
        {"category": "error-handling", "claimed_at_round": 1, "recurred_at_round": 1}
    ]
    assert _trend.recurred_category_names(phases) == ["error-handling"]


def test_recurred_categories_trigger_round_not_evidence():
    # review-r0 是触发 fix-r1 自报的原因（M=0 < N=1），不算复现证据；
    # 此后无 review-rM(M≥1) 再现 → 无复现判定
    phases = {
        "review-r0": _review(["error-handling"]),
        "fix-r1": _fix("error-handling"),
        "review-r1": _review(["validation"]),
    }
    assert _trend.recurred_categories(phases) == []


def test_recurred_categories_no_recur_after_claim():
    phases = {
        "review-r0": _review(["error-handling"]),
        "fix-r1": _fix("error-handling"),
        # 无更晚 review 再现该 category
    }
    assert _trend.recurred_categories(phases) == []


def test_recurred_categories_missing_self_report_no_verdict():
    phases = {
        "review-r0": _review(["error-handling"]),
        "fix-r1": _fix("-"),  # 自报缺失
        "review-r1": _review(["error-handling"]),
    }
    assert _trend.recurred_categories(phases) == []
    phases["fix-r1"] = {}  # 完全无 categories_scanned key
    assert _trend.recurred_categories(phases) == []


def test_recurred_categories_min_recur_round():
    # 多轮再现时取最小 M
    phases = {
        "review-r0": _review(["error-handling"]),
        "fix-r1": _fix("error-handling"),
        "review-r1": _review(["error-handling"]),
        "fix-r2": _fix("error-handling"),
        "review-r2": _review(["error-handling"]),
    }
    out = _trend.recurred_categories(phases)
    # fix-r1 的最早复现是 review-r1；fix-r2 的最早复现是 review-r2
    assert {
        "category": "error-handling",
        "claimed_at_round": 1,
        "recurred_at_round": 1,
    } in out
    assert {
        "category": "error-handling",
        "claimed_at_round": 2,
        "recurred_at_round": 2,
    } in out


def test_no_history_returns_empty_structures():
    assert _trend.category_streaks({}) == {}
    assert _trend.recurred_categories({}) == []
    assert _trend.recurred_category_names({}) == []
