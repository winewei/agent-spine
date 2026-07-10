"""``npc spec-report render`` 测试。

覆盖：
- 派生纯函数：commit chain / 收敛 / 返工 / 耗时 / 资源 / one_shot / 自报核验
- md 渲染：固定标题段、行数上限、不含 phase summary 原文
- CLI handler：三产物落盘、telemetry emit、common_metrics 三视图一致
- 边界：非法 seq、非 archived 终态、base 缺失走兜底、产物目录不可写不阻塞
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from npc import spec_report as _sr, state as _state, telemetry as _telemetry


# ============================================================
# 测试用 state 构造
# ============================================================


def _base_entry(**overrides) -> dict:
    entry = {
        "seq": 1,
        "change_id": "add-thing",
        "status": "archived",
        "implement_commit": "c-impl",
        "archive_commit": "c-arc",
        "blocking_trend": [],
        "categories_seen": [],
        "phases": {
            "implement": {"status": "done", "commit": "c-impl", "duration_ms": 60000},
            "review-r0": {
                "status": "done",
                "duration_ms": 30000,
                "blocking": 0,
                "categories": [],
            },
            "archive": {"status": "done", "duration_ms": 2000},
        },
    }
    entry.update(overrides)
    return entry


def _stuffed_state(progress: list[dict], run_ts="2026-07-08-1000") -> dict:
    return {
        "schema_version": 2,
        "run_ts": run_ts,
        "proj_key": "-repo",
        "progress": progress,
    }


# ============================================================
# 纯函数：派生维度
# ============================================================


def test_one_shot_true_when_blocking_zero_no_fix():
    entry = _base_entry()
    assert _sr._one_shot(entry) is True


def test_one_shot_false_when_fix_round_present():
    entry = _base_entry(
        blocking_trend=[3, 0],
        phases={
            "implement": {"status": "done", "commit": "c-impl", "duration_ms": 1},
            "review-r0": {"status": "done", "duration_ms": 1, "blocking": 3, "categories": ["validation"]},
            "fix-r1": {"status": "done", "commit": "c-f1", "duration_ms": 1,
                       "categories_scanned": "validation", "regressions_added": "-"},
            "review-r1": {"status": "done", "duration_ms": 1, "blocking": 0, "categories": []},
            "archive": {"status": "done", "duration_ms": 1},
        },
    )
    assert _sr._one_shot(entry) is False


def test_one_shot_null_when_review_data_missing():
    entry = _base_entry(phases={"implement": {"status": "done", "commit": "c-impl"}})
    assert _sr._one_shot(entry) is None


def test_commit_chain_includes_implement_fix_archive():
    entry = _base_entry(
        phases={
            "implement": {"status": "done", "commit": "c-impl"},
            "fix-r1": {"status": "done", "commit": "c-f1"},
            "fix-r2": {"status": "done", "commit": "c-f2"},
        },
        archive_commit="c-arc",
    )
    chain = _sr._commit_chain(entry)
    assert chain == [
        {"phase": "implement", "commit": "c-impl"},
        {"phase": "fix-r1", "commit": "c-f1"},
        {"phase": "fix-r2", "commit": "c-f2"},
        {"phase": "archive", "commit": "c-arc"},
    ]


def test_category_distribution_counts_review_round_occurrences():
    entry = _base_entry(
        phases={
            "review-r0": {"status": "done", "blocking": 2, "categories": ["validation", "concurrency"]},
            "review-r1": {"status": "done", "blocking": 1, "categories": ["validation"]},
        }
    )
    dist = _sr._category_distribution(entry)
    assert dist == {"validation": 2, "concurrency": 1}


def test_total_duration_ms_sums_all_phases():
    entry = _base_entry()
    assert _sr._total_duration_ms(entry) == 60000 + 30000 + 2000


# ============================================================
# 自报核验（C）
# ============================================================


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _commit_file(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_verify_regressions_warn_when_claim_but_no_test_file(fake_repo: Path):
    impl_commit = _commit_file(fake_repo, "src/foo.py", "x=1", "impl")
    fix_commit = _commit_file(fake_repo, "src/foo.py", "x=2", "fix")
    entry = _base_entry(
        implement_commit=impl_commit,
        phases={
            "fix-r1": {"status": "done", "commit": fix_commit, "regressions_added": "some-regression"},
        },
    )
    out = _sr._verify_regressions(fake_repo, entry)
    assert out[0]["verdict"] == "warn"


def test_verify_regressions_ok_when_claim_matches_test_file(fake_repo: Path):
    impl_commit = _commit_file(fake_repo, "src/foo.py", "x=1", "impl")
    fix_commit = _commit_file(fake_repo, "tests/test_foo.py", "def test(): pass", "fix")
    entry = _base_entry(
        implement_commit=impl_commit,
        phases={
            "fix-r1": {"status": "done", "commit": fix_commit, "regressions_added": "some-regression"},
        },
    )
    out = _sr._verify_regressions(fake_repo, entry)
    assert out[0]["verdict"] == "ok"


def test_verify_regressions_unverifiable_when_missing_commit(fake_repo: Path):
    entry = _base_entry(
        implement_commit="c-impl",
        phases={
            "fix-r1": {"status": "done", "commit": None, "regressions_added": "some-regression"},
        },
    )
    out = _sr._verify_regressions(fake_repo, entry)
    assert out[0]["verdict"] == "unverifiable"


def test_verify_regressions_unverifiable_when_field_missing(fake_repo: Path):
    entry = _base_entry(
        implement_commit="c-impl",
        phases={"fix-r1": {"status": "done", "commit": "c-f1"}},
    )
    out = _sr._verify_regressions(fake_repo, entry)
    assert out[0]["verdict"] == "unverifiable"


def test_verify_regressions_ok_when_no_claim(fake_repo: Path):
    entry = _base_entry(
        implement_commit="c-impl",
        phases={"fix-r1": {"status": "done", "commit": "c-f1", "regressions_added": "-"}},
    )
    out = _sr._verify_regressions(fake_repo, entry)
    assert out[0]["verdict"] == "ok"
    assert out[0]["claimed"] == []


def test_verify_categories_scanned_ok_when_covers_seen():
    entry = _base_entry(
        categories_seen=["validation", "concurrency"],
        phases={
            "fix-r1": {"status": "done", "categories_scanned": "validation,concurrency"},
        },
    )
    out = _sr._verify_categories_scanned(entry)
    assert out["verdict"] == "ok"
    assert out["missing"] == []


def test_verify_categories_scanned_warn_when_missing_some():
    entry = _base_entry(
        categories_seen=["validation", "concurrency"],
        phases={"fix-r1": {"status": "done", "categories_scanned": "validation"}},
    )
    out = _sr._verify_categories_scanned(entry)
    assert out["verdict"] == "warn"
    assert out["missing"] == ["concurrency"]


def test_verify_categories_scanned_unverifiable_when_no_self_report():
    entry = _base_entry(
        categories_seen=["validation"],
        phases={"fix-r1": {"status": "done"}},
    )
    out = _sr._verify_categories_scanned(entry)
    assert out["verdict"] == "unverifiable"


def test_verify_categories_scanned_ok_when_nothing_seen():
    entry = _base_entry(categories_seen=[], phases={})
    out = _sr._verify_categories_scanned(entry)
    assert out["verdict"] == "ok"


def test_aggregate_self_report_verdict_precedence():
    assert _sr._aggregate_self_report_verdict(
        [{"verdict": "ok"}, {"verdict": "warn"}], {"verdict": "unverifiable"}
    ) == "warn"
    assert _sr._aggregate_self_report_verdict(
        [{"verdict": "ok"}], {"verdict": "unverifiable"}
    ) == "unverifiable"
    assert _sr._aggregate_self_report_verdict(
        [{"verdict": "ok"}], {"verdict": "ok"}
    ) == "ok"


# ============================================================
# change fix-prompt-exhaustive-sweep：unsubstantiated verdict
# ============================================================


def test_verify_categories_scanned_unsubstantiated_on_recurrence():
    # fix-r1 自报 error-handling，review-r1 再次 blocking → 复现 → unsubstantiated
    entry = _base_entry(
        categories_seen=["error-handling"],
        phases={
            "review-r0": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
            "fix-r1": {"status": "done", "categories_scanned": "error-handling"},
            "review-r1": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
        },
    )
    out = _sr._verify_categories_scanned(entry)
    assert out["verdict"] == "unsubstantiated"
    assert out["recurred"] == [
        {"category": "error-handling", "claimed_at_round": 1, "recurred_at_round": 1}
    ]


def test_verify_categories_scanned_unsubstantiated_beats_ok_coverage():
    # 集合覆盖层面本应 ok（自报覆盖全部 seen），但存在复现 → unsubstantiated 优先
    entry = _base_entry(
        categories_seen=["error-handling"],
        phases={
            "review-r0": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
            "fix-r1": {"status": "done", "categories_scanned": "error-handling"},
            "review-r1": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
        },
    )
    out = _sr._verify_categories_scanned(entry)
    assert out["verdict"] == "unsubstantiated"
    assert out["missing"] == []  # 集合覆盖度本身是完整的


def test_verify_categories_scanned_no_recurrence_keeps_ok():
    # fix-r1 自报后无更晚 review 再现 → 维持 ok，无 recurred 字段
    entry = _base_entry(
        categories_seen=["error-handling"],
        phases={
            "review-r0": {"status": "done", "blocking": 1, "categories": ["error-handling"]},
            "fix-r1": {"status": "done", "categories_scanned": "error-handling"},
            "review-r1": {"status": "done", "blocking": 0, "categories": []},
        },
    )
    out = _sr._verify_categories_scanned(entry)
    assert out["verdict"] == "ok"
    assert "recurred" not in out


def test_aggregate_self_report_verdict_unsubstantiated_highest():
    assert _sr._aggregate_self_report_verdict(
        [{"verdict": "warn"}], {"verdict": "unsubstantiated"}
    ) == "unsubstantiated"
    assert _sr._aggregate_self_report_verdict(
        [{"verdict": "ok"}], {"verdict": "unsubstantiated"}
    ) == "unsubstantiated"


# ============================================================
# derive_report + render_md
# ============================================================


def test_derive_report_json_fields_complete(fake_repo: Path, tmp_path: Path):
    impl_commit = _commit_file(fake_repo, "src/foo.py", "x=1", "impl")
    f1 = _commit_file(fake_repo, "tests/test_foo.py", "def test(): pass", "fix1")
    f2 = _commit_file(fake_repo, "src/foo.py", "x=3", "fix2")
    entry = _base_entry(
        implement_commit=impl_commit,
        archive_commit="c-arc",
        blocking_trend=[3, 2, 0],
        categories_seen=["validation", "concurrency"],
        phases={
            "implement": {"status": "done", "commit": impl_commit, "duration_ms": 1000},
            "review-r0": {"status": "done", "duration_ms": 500, "blocking": 3, "categories": ["validation"]},
            "fix-r1": {
                "status": "done", "commit": f1, "duration_ms": 2000,
                "categories_scanned": "validation", "regressions_added": "regr-a",
            },
            "review-r1": {"status": "done", "duration_ms": 400, "blocking": 2, "categories": ["concurrency"]},
            "fix-r2": {
                "status": "done", "commit": f2, "duration_ms": 1500,
                "categories_scanned": "concurrency", "regressions_added": "-",
            },
            "review-r2": {"status": "done", "duration_ms": 300, "blocking": 0, "categories": []},
            "archive": {"status": "done", "duration_ms": 100},
        },
    )
    state = _stuffed_state([entry])
    report = _sr.derive_report(state, 1, fake_repo, base=tmp_path, telemetry_events=[])

    chain = report["delivery"]["commit_chain"]
    assert [c["phase"] for c in chain] == ["implement", "fix-r1", "fix-r2", "archive"]
    assert report["convergence"]["review_rounds"] == 3
    assert report["convergence"]["fix_rounds"] == 2
    assert report["convergence"]["blocking_trend"] == [3, 2, 0]
    assert report["convergence"]["one_shot"] is False
    assert report["rework"]["category_distribution"] == {"validation": 1, "concurrency": 1}
    assert report["duration"]["total_duration_ms"] == 1000 + 500 + 2000 + 400 + 1500 + 300 + 100
    assert "estimated_tokens_by_backend" in report["resources"]
    srv = report["self_report_verification"]
    assert srv["regressions_added"][0]["verdict"] == "ok"  # fix-r1 claim touches test file
    assert srv["regressions_added"][1]["verdict"] == "ok"  # fix-r2 explicit "-"
    assert srv["categories_scanned"]["verdict"] == "ok"


def test_derive_report_one_shot_change():
    entry = _base_entry()
    state = _stuffed_state([entry])
    report = _sr.derive_report(state, 1, Path("/nonexistent"), telemetry_events=[])
    assert report["convergence"]["one_shot"] is True
    assert report["convergence"]["fix_rounds"] == 0
    assert report["rework"]["category_distribution"] == {}
    assert report["convergence"]["blocking_trend"] == []


def test_render_md_has_fixed_headers_and_line_limit(tmp_path: Path):
    entry = _base_entry()
    state = _stuffed_state([entry])
    report = _sr.derive_report(state, 1, Path("/nonexistent"), base=tmp_path, telemetry_events=[])
    md = _sr.render_md(report)
    for header in ("## 终态", "## 收敛", "## 返工", "## 耗时", "## 资源", "## 自报核验", "## 叙事"):
        assert header in md
    assert len(md.splitlines()) <= _sr.MD_LINE_LIMIT


def test_render_md_does_not_include_phase_summary_raw_text(tmp_path: Path):
    (tmp_path / "implement.summary.md").write_text(
        "# Implement Summary — x\n\nCOMMIT_SECRET_MARKER_XYZ raw phase log line\n"
    )
    entry = _base_entry()
    state = _stuffed_state([entry])
    report = _sr.derive_report(state, 1, Path("/nonexistent"), base=tmp_path, telemetry_events=[])
    md = _sr.render_md(report)
    assert "COMMIT_SECRET_MARKER_XYZ" not in md


def test_common_metrics_consistent_across_json_and_md(tmp_path: Path):
    entry = _base_entry()
    state = _stuffed_state([entry])
    report = _sr.derive_report(state, 1, Path("/nonexistent"), base=tmp_path, telemetry_events=[])
    common = _sr.common_metrics(report)
    md = _sr.render_md(report)
    assert str(common["review_rounds"]) in md
    assert str(common["fix_rounds"]) in md
    assert common["self_report_summary_verdict"] in md


# ============================================================
# CLI handler：render()
# ============================================================


def test_cli_render_writes_three_artifacts(env_setup, capsys, make_args):
    entry = _base_entry()
    state = _stuffed_state([entry], run_ts=env_setup.run_ts)
    state["proj_key"] = env_setup.proj_key
    _state.write_state(env_setup.state_json, env_setup.state_md, state)

    args = make_args(seq=1)
    _sr.render(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["telemetry_emitted"] is True

    base = _sr._paths.base_for(env_setup, 1, "add-thing")
    assert (base / "spec-report.json").is_file()
    assert (base / "spec-report.md").is_file()

    events = list(_telemetry.iter_events())
    kinds = [e.get("kind") for e in events]
    assert "spec.report" in kinds
    ev = [e for e in events if e.get("kind") == "spec.report"][0]
    report_json = json.loads((base / "spec-report.json").read_text())
    assert ev["review_rounds"] == report_json["convergence"]["review_rounds"]
    assert ev["fix_rounds"] == report_json["convergence"]["fix_rounds"]
    assert ev["final_status"] == report_json["status"]
    assert ev["pointer"]["report_json"] == str(base / "spec-report.json")


def test_cli_render_invalid_seq_returns_ok_false(env_setup, capsys, make_args):
    state = _stuffed_state([_base_entry()], run_ts=env_setup.run_ts)
    _state.write_state(env_setup.state_json, env_setup.state_md, state)

    args = make_args(seq=99)
    with pytest.raises(SystemExit) as ei:
        _sr.render(args)
    assert ei.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "seq_out_of_range"


def test_cli_render_non_archived_status_rejected(env_setup, capsys, make_args):
    entry = _base_entry(status="failed")
    state = _stuffed_state([entry], run_ts=env_setup.run_ts)
    _state.write_state(env_setup.state_json, env_setup.state_md, state)

    args = make_args(seq=1)
    with pytest.raises(SystemExit) as ei:
        _sr.render(args)
    assert ei.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "not_archived"
    base = _sr._paths.base_for(env_setup, 1, "add-thing")
    assert not (base / "spec-report.json").exists()
    assert not (base / "spec-report.md").exists()


def test_cli_render_skipped_auto_status_rejected(env_setup, capsys, make_args):
    entry = _base_entry(status="skipped-auto")
    state = _stuffed_state([entry], run_ts=env_setup.run_ts)
    _state.write_state(env_setup.state_json, env_setup.state_md, state)

    args = make_args(seq=1)
    with pytest.raises(SystemExit):
        _sr.render(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"] == "not_archived"


def test_cli_render_base_missing_falls_back_to_base_for(env_setup, capsys, make_args):
    entry = _base_entry()
    del entry["seq"]
    entry["seq"] = 1
    entry.pop("base", None)
    state = _stuffed_state([entry], run_ts=env_setup.run_ts)
    _state.write_state(env_setup.state_json, env_setup.state_md, state)

    args = make_args(seq=1)
    _sr.render(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    base = _sr._paths.base_for(env_setup, 1, "add-thing")
    assert (base / "spec-report.json").is_file()


def test_cli_render_artifact_dir_unwritable_does_not_raise(env_setup, capsys, make_args, monkeypatch):
    entry = _base_entry()
    state = _stuffed_state([entry], run_ts=env_setup.run_ts)
    _state.write_state(env_setup.state_json, env_setup.state_md, state)

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _boom)

    args = make_args(seq=1)
    _sr.render(args)  # 不应抛栈
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["errors"]


def test_cli_render_two_changes_do_not_cross_write(env_setup, capsys, make_args):
    """不同 seq 的产物落到各自 base 目录，互不覆盖串写。"""
    entry_a = _base_entry(seq=1, change_id="change-a")
    entry_b = _base_entry(seq=2, change_id="change-b", blocking_trend=[1, 0])
    state = _stuffed_state([entry_a, entry_b], run_ts=env_setup.run_ts)
    _state.write_state(env_setup.state_json, env_setup.state_md, state)

    _sr.render(make_args(seq=1))
    capsys.readouterr()
    _sr.render(make_args(seq=2))
    capsys.readouterr()

    base_a = _sr._paths.base_for(env_setup, 1, "change-a")
    base_b = _sr._paths.base_for(env_setup, 2, "change-b")
    report_a = json.loads((base_a / "spec-report.json").read_text())
    report_b = json.loads((base_b / "spec-report.json").read_text())
    assert report_a["change_id"] == "change-a"
    assert report_b["change_id"] == "change-b"
    assert report_a["change_seq"] == 1
    assert report_b["change_seq"] == 2


def test_cli_render_idempotent_rerun_overwrites(env_setup, capsys, make_args):
    entry = _base_entry()
    state = _stuffed_state([entry], run_ts=env_setup.run_ts)
    _state.write_state(env_setup.state_json, env_setup.state_md, state)

    args = make_args(seq=1)
    _sr.render(args)
    capsys.readouterr()
    _sr.render(args)
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    events = [e for e in _telemetry.iter_events() if e.get("kind") == "spec.report"]
    assert len(events) == 2  # 每次调用 append 一条，允许重复（不强制去重）
