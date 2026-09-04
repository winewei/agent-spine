"""waves.py 测试：拓扑分层 / 文件着色 / 破环 / compute 纯函数与 CLI 入口。"""

from __future__ import annotations

import argparse
import json
import io

import pytest

from npc import waves as _waves


# ============================================================
# topological_layers
# ============================================================


def test_layers_linear_chain():
    layers, cycle = _waves.topological_layers(
        ["a", "b", "c"], [["a", "b"], ["b", "c"]], {}
    )
    assert layers == [["a"], ["b"], ["c"]]
    assert cycle == []


def test_layers_independent_same_layer():
    layers, cycle = _waves.topological_layers(["b", "a"], [], {})
    assert layers == [["a", "b"]]  # 无 tie_break 时按 id 排序，确定性
    assert cycle == []


def test_layers_tie_break_order():
    tb = {"b": [0, 1], "a": [1, 1]}
    layers, _ = _waves.topological_layers(["a", "b"], [], tb)
    assert layers == [["b", "a"]]  # tier 小者在前


def test_layers_cycle_broken_and_reported():
    layers, cycle = _waves.topological_layers(
        ["a", "b"], [["a", "b"], ["b", "a"]], {}
    )
    assert cycle == ["a"]  # 强制释放 tie-break 序最小者
    flat = [n for layer in layers for n in layer]
    assert sorted(flat) == ["a", "b"]


def test_layers_ignores_bad_edges():
    layers, cycle = _waves.topological_layers(
        ["a", "b"], [["a", "ghost"], ["a", "a"], ["only-one"]], {}
    )
    assert layers == [["a", "b"]]
    assert cycle == []


# ============================================================
# split_by_files
# ============================================================


def test_split_no_conflict_single_subwave():
    subs, conflicts = _waves.split_by_files(
        ["a", "b"], {"a": ["x.py"], "b": ["y.py"]}, {}
    )
    assert subs == [["a", "b"]]
    assert conflicts == []


def test_split_conflict_serializes_pair():
    subs, conflicts = _waves.split_by_files(
        ["a", "b"], {"a": ["x.py"], "b": ["x.py"]}, {}
    )
    assert subs == [["a"], ["b"]]
    assert conflicts == [["a", "b"]]


def test_split_empty_fileset_never_conflicts():
    subs, _ = _waves.split_by_files(["a", "b", "c"], {"a": ["x.py"], "b": ["x.py"]}, {})
    # c 无文件集，跟谁都能并行
    assert ["a", "c"] in subs or any("c" in s and "a" in s for s in subs)


def test_split_dir_entry_conflicts_with_contained_file():
    # LLM 抽取的目录级保守标识必须命中其下具体文件
    subs, conflicts = _waves.split_by_files(
        ["a", "b"], {"a": ["app/services/"], "b": ["app/services/foo.py"]}, {}
    )
    assert subs == [["a"], ["b"]]
    assert conflicts == [["a", "b"]]


def test_split_sibling_files_still_parallel():
    subs, _ = _waves.split_by_files(
        ["a", "b"], {"a": ["app/services/foo.py"], "b": ["app/services/bar.py"]}, {}
    )
    assert subs == [["a", "b"]]


def test_split_path_normalization_matches():
    # "./src/x.py" 与 "src/x.py" 是同一路径
    subs, conflicts = _waves.split_by_files(
        ["a", "b"], {"a": ["./src/x.py"], "b": ["src/x.py"]}, {}
    )
    assert subs == [["a"], ["b"]]
    assert conflicts == [["a", "b"]]


# ============================================================
# compute（端到端纯函数）
# ============================================================


def test_compute_full_contract():
    out = _waves.compute(
        {
            "nodes": ["a", "b", "c"],
            "edges": [["a", "c"]],
            "files": {"a": ["x.py"], "b": ["x.py"], "c": ["y.py"]},
            "tie_break": {"a": [1, 1], "b": [1, 1], "c": [2, 1]},
        }
    )
    assert out["waves"] == [["a"], ["b"], ["c"]]
    assert out["layers"] == [["a", "b"], ["c"]]
    assert out["cycle"] == []
    assert len(out["split_reasons"]) == 1
    assert out["split_reasons"][0]["shared_files"] == ["x.py"]


def test_compute_waves_cover_all_nodes():
    out = _waves.compute({"nodes": ["a", "b", "c", "d"], "edges": [["a", "b"]]})
    flat = sorted(n for w in out["waves"] for n in w)
    assert flat == ["a", "b", "c", "d"]


def test_compute_missing_nodes_raises():
    with pytest.raises(ValueError):
        _waves.compute({"edges": []})
    with pytest.raises(ValueError):
        _waves.compute({"nodes": []})


def test_compute_duplicate_nodes_raises():
    with pytest.raises(ValueError, match="duplicate"):
        _waves.compute({"nodes": ["a", "b", "a"]})


def test_compute_non_string_nodes_raises():
    with pytest.raises(ValueError):
        _waves.compute({"nodes": ["a", 1]})
    with pytest.raises(ValueError):
        _waves.compute({"nodes": ["a", ""]})


def test_compute_shared_files_dir_overlap():
    out = _waves.compute(
        {"nodes": ["a", "b"], "files": {"a": ["app/services/"], "b": ["app/services/foo.py"]}}
    )
    assert out["waves"] == [["a"], ["b"]]
    assert out["split_reasons"][0]["shared_files"] == [
        "app/services/",
        "app/services/foo.py",
    ]


# ============================================================
# run（CLI handler）
# ============================================================


def _run(monkeypatch, capsys, stdin_text: str | None = None, input_file=None):
    if stdin_text is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    args = argparse.Namespace(input=str(input_file) if input_file else None)
    _waves.run(args)
    return capsys.readouterr()


def test_run_stdin_emits_single_line_json(monkeypatch, capsys):
    captured = _run(monkeypatch, capsys, stdin_text=json.dumps({"nodes": ["a"]}))
    out = json.loads(captured.out)
    assert out["waves"] == [["a"]]


def test_run_input_file(tmp_path, monkeypatch, capsys):
    f = tmp_path / "dag.json"
    f.write_text(json.dumps({"nodes": ["a", "b"], "edges": [["a", "b"]]}))
    captured = _run(monkeypatch, capsys, input_file=f)
    out = json.loads(captured.out)
    assert out["waves"] == [["a"], ["b"]]


def test_run_invalid_json_exit_2(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, capsys, stdin_text="not-json")
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_json"


def test_run_missing_nodes_exit_2(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, capsys, stdin_text="{}")
    assert ei.value.code == 2


def test_run_duplicate_nodes_exit_2(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run(monkeypatch, capsys, stdin_text=json.dumps({"nodes": ["a", "a"]}))
    assert ei.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "invalid_input"


# ============================================================
# ready（流水化增量调度）
# ============================================================


def test_ready_no_done_returns_only_roots():
    out = _waves.ready(
        {"nodes": ["a", "b", "c"], "edges": [["a", "b"], ["b", "c"]]}
    )
    assert out["ready"] == ["a"]
    assert out["blocked"] == {"b": ["dep-pending:a"], "c": ["dep-pending:b"]}
    assert out["remaining"] == 3
    assert out["warnings"] == []


def test_ready_done_releases_downstream():
    out = _waves.ready(
        {"nodes": ["a", "b", "c"], "edges": [["a", "b"], ["b", "c"]], "done": ["a"]}
    )
    assert out["ready"] == ["b"]
    assert out["blocked"] == {"c": ["dep-pending:b"]}
    assert out["remaining"] == 2


def test_ready_multi_pred_needs_all_done():
    out = _waves.ready(
        {"nodes": ["a", "b", "c"], "edges": [["a", "c"], ["b", "c"]], "done": ["a"]}
    )
    assert out["ready"] == ["b"]
    assert out["blocked"]["c"] == ["dep-pending:b"]


def test_ready_active_file_conflict_blocks():
    out = _waves.ready(
        {
            "nodes": ["a", "b"],
            "files": {"a": ["src/svc/"], "b": ["src/svc/api.py"]},
            "active": ["a"],
        }
    )
    assert out["ready"] == []
    assert out["blocked"] == {"b": ["file-conflict:a"]}
    assert out["remaining"] == 2  # active 未进终态，仍计入 remaining


def test_ready_active_without_conflict_does_not_block():
    out = _waves.ready(
        {"nodes": ["a", "b"], "files": {"a": ["src/x.py"], "b": ["src/y.py"]}, "active": ["a"]}
    )
    assert out["ready"] == ["b"]
    assert out["blocked"] == {}


def test_ready_intra_batch_conflict_keeps_tie_break_winner():
    out = _waves.ready(
        {
            "nodes": ["a", "b"],
            "files": {"a": ["src/x.py"], "b": ["src/x.py"]},
            "tie_break": {"a": [2, 1], "b": [1, 1]},
        }
    )
    assert out["ready"] == ["b"]  # tier 小者先选中，冲突方被挤出本批
    assert out["blocked"] == {"a": ["file-conflict:b"]}


def test_ready_limit_truncates():
    out = _waves.ready({"nodes": ["a", "b", "c"], "limit": 2})
    assert out["ready"] == ["a", "b"]
    assert out["blocked"] == {"c": ["limit"]}
    assert out["remaining"] == 3


def test_ready_limit_zero_blocks_all():
    out = _waves.ready({"nodes": ["a"], "limit": 0})
    assert out["ready"] == []
    assert out["blocked"] == {"a": ["limit"]}


def test_ready_unknown_cids_go_to_warnings():
    out = _waves.ready({"nodes": ["a"], "done": ["ghost"], "active": ["phantom"]})
    assert out["ready"] == ["a"]
    assert out["warnings"] == ["unknown-active:phantom", "unknown-done:ghost"]


def test_ready_finished_defaults_to_done_and_excludes():
    out = _waves.ready({"nodes": ["a", "b"], "done": ["a"], "finished": ["b"]})
    assert out["ready"] == []
    assert out["blocked"] == {}
    assert out["remaining"] == 0


def test_ready_dep_pending_short_circuits_file_conflict():
    out = _waves.ready(
        {
            "nodes": ["a", "b", "c"],
            "edges": [["a", "c"]],
            "files": {"b": ["src/x.py"], "c": ["src/x.py"]},
            "active": ["b"],
        }
    )
    assert out["ready"] == ["a"]
    assert out["blocked"]["c"] == ["dep-pending:a"]  # 依赖层先短路，不报 file-conflict


def test_ready_bad_limit_raises():
    with pytest.raises(ValueError):
        _waves.ready({"nodes": ["a"], "limit": -1})
    with pytest.raises(ValueError):
        _waves.ready({"nodes": ["a"], "limit": "2"})


def test_ready_bad_done_type_raises():
    with pytest.raises(ValueError):
        _waves.ready({"nodes": ["a"], "done": "a"})


def _run_ready(monkeypatch, capsys, stdin_text=None, input_file=None):
    if stdin_text is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    args = argparse.Namespace(input=str(input_file) if input_file else None)
    _waves.run_ready(args)
    return capsys.readouterr()


def test_run_ready_emits_single_line_json(monkeypatch, capsys):
    payload = {"nodes": ["a", "b"], "edges": [["a", "b"]], "done": ["a"]}
    captured = _run_ready(monkeypatch, capsys, stdin_text=json.dumps(payload))
    assert len(captured.out.strip().splitlines()) == 1
    out = json.loads(captured.out)
    assert out["ready"] == ["b"]


def test_run_ready_invalid_input_exit_2(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run_ready(monkeypatch, capsys, stdin_text=json.dumps({"nodes": ["a"], "limit": -1}))
    assert ei.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "invalid_input"


def test_cli_registers_plan_ready():
    from npc import cli as _cli

    parser = _cli._build_parser()
    args = parser.parse_args(["plan", "ready", "--input", "dag.json"])
    assert args._cmd_path == "plan ready"
    assert args.input == "dag.json"
    assert callable(args.handler)
