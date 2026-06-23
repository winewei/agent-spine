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
