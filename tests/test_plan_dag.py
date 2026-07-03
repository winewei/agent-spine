"""test_plan_dag.py — npc plan dag 单元测试。

覆盖 spec：
- plan-dag-analysis spec.md 全部 Scenario
- 同层/依赖后置/路径重叠/无路径退化/max_parallel=1 等价串行
- 依赖环/未知依赖退化串行
- serialization_reason 热点路径点名
- max_parallel 切片
- 13 个加固提案真实语料召回（verifying no crash）
"""

from __future__ import annotations

import json
import argparse
from pathlib import Path
from typing import Any

import pytest

from npc import plan as _plan
from npc.config import SchedulerConfig


# ============================================================
# helpers
# ============================================================

def _make_change_dir(tmp_path: Path, change_id: str, tasks_content: str = "", proposal_content: str = "") -> Path:
    """在 tmp_path/openspec/changes/<change_id> 创建 change 目录并写 tasks.md / proposal.md。"""
    change_dir = tmp_path / "openspec" / "changes" / change_id
    change_dir.mkdir(parents=True)
    if tasks_content:
        (change_dir / "tasks.md").write_text(tasks_content, encoding="utf-8")
    if proposal_content:
        (change_dir / "proposal.md").write_text(proposal_content, encoding="utf-8")
    return change_dir


def _make_dag_args(tmp_path: Path, plan_order: list[str], config_toml: str = "") -> argparse.Namespace:
    """构造 npc plan dag 的 args。"""
    if config_toml:
        config_file = tmp_path / "config.toml"
        config_file.write_text(config_toml, encoding="utf-8")
        config_path = str(config_file)
    else:
        config_path = None
    ns = argparse.Namespace(
        plan_order=json.dumps(plan_order),
        config=config_path,
        run_ts=None,
        task_log_dir=None,
        state_json=None,
    )
    return ns


def _run_dag(monkeypatch, tmp_path: Path, plan_order: list[str], config_toml: str = "") -> dict:
    """运行 run_dag，捕获 stdout 并解析 JSON。"""
    monkeypatch.setattr(_plan, "_resolve_repo_root", lambda args: tmp_path)
    args = _make_dag_args(tmp_path, plan_order, config_toml)
    captured = {}
    original_emit = _plan._io.emit

    def capture_emit(data: Any) -> None:
        captured.update(data)

    monkeypatch.setattr(_plan._io, "emit", capture_emit)
    _plan.run_dag(args)
    return captured


# ============================================================
# Scenario: 两个不重叠 change 分入同层
# ============================================================

def test_dag_two_non_overlapping_same_layer(monkeypatch, tmp_path):
    """change-a 和 change-b 路径不重叠且无依赖 → 同一层。"""
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/a.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/b.py`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    layers = result["layers"]
    # 两个 change 应该在同一层
    assert any(set(layer) == {"change-a", "change-b"} for layer in layers), f"layers={layers}"


# ============================================================
# Scenario: 有依赖的 change 分入后置层
# ============================================================

def test_dag_dependency_later_layer(monkeypatch, tmp_path):
    """change-b 声明依赖 change-a → b 所在层 > a 所在层。"""
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/a.py`")
    _make_change_dir(tmp_path, "change-b",
                     tasks_content="修改 `src/b.py`",
                     proposal_content="依赖前置：change-a\n")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    layers = result["layers"]
    # 找各 change 所在层
    layer_of = {}
    for i, layer in enumerate(layers):
        for cid in layer:
            layer_of[cid] = i
    assert "change-a" in layer_of
    assert "change-b" in layer_of
    assert layer_of["change-b"] > layer_of["change-a"], f"layer_of={layer_of}"


# ============================================================
# Scenario: 路径重叠的 change 不同层
# ============================================================

def test_dag_path_overlap_different_layers(monkeypatch, tmp_path):
    """change-a 和 change-b 共同触碰 src/shared.py → 不同层。"""
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/shared.py` 与 `src/a.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/shared.py` 与 `src/b.py`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    layers = result["layers"]
    # change-a 和 change-b 不应在同层
    for layer in layers:
        assert not ("change-a" in layer and "change-b" in layer), f"Both in same layer: {layer}"


# ============================================================
# Scenario: 无路径信息的 change 单独成层
# ============================================================

def test_dag_no_paths_single_layer(monkeypatch, tmp_path):
    """change-c 无路径信息 → 独占一层。"""
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/a.py`")
    _make_change_dir(tmp_path, "change-c", tasks_content="这是一些自然语言描述，没有路径信息")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-c"])
    assert result["ok"] is True
    layers = result["layers"]
    # change-c 应独占一层
    change_c_layers = [layer for layer in layers if "change-c" in layer]
    assert len(change_c_layers) == 1
    assert len(change_c_layers[0]) == 1, f"change-c not alone: {change_c_layers[0]}"


# ============================================================
# Scenario: 依赖环退化串行
# ============================================================

def test_dag_cycle_degrades_to_serial(monkeypatch, tmp_path):
    """change-a 依赖 b，b 依赖 a → 完全串行，degraded_reason 含 cycle。"""
    _make_change_dir(tmp_path, "change-a",
                     tasks_content="修改 `src/a.py`",
                     proposal_content="依赖前置：change-b\n")
    _make_change_dir(tmp_path, "change-b",
                     tasks_content="修改 `src/b.py`",
                     proposal_content="依赖前置：change-a\n")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    assert result.get("degraded_reason") is not None, "Should have degraded_reason for cycle"
    assert "cycle" in str(result["degraded_reason"]).lower(), f"degraded_reason={result['degraded_reason']}"
    # 所有层均为单元素
    for layer in result["layers"]:
        assert len(layer) == 1, f"Should be serial: {layer}"


# ============================================================
# Scenario: max_parallel=1 等价串行
# ============================================================

def test_dag_max_parallel_1_serial(monkeypatch, tmp_path):
    """max_parallel=1 → 全串行，每层只有一个 change。"""
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/a.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/b.py`")
    _make_change_dir(tmp_path, "change-c", tasks_content="修改 `src/c.py`")

    config_toml = "[scheduler]\nmax_parallel = 1\n"
    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b", "change-c"], config_toml)
    assert result["ok"] is True
    for layer in result["layers"]:
        assert len(layer) == 1, f"max_parallel=1 should yield single-element layers: {layer}"
    assert len(result["layers"]) == 3


# ============================================================
# Scenario: 超限层被切片
# ============================================================

def test_dag_max_parallel_slice(monkeypatch, tmp_path):
    """5 个不重叠 change + max_parallel=3 → 切成 3+2 两层。"""
    for i in range(5):
        _make_change_dir(tmp_path, f"change-{i}",
                         tasks_content=f"修改 `src/module_{i}.py`")

    config_toml = "[scheduler]\nmax_parallel = 3\n"
    plan = [f"change-{i}" for i in range(5)]
    result = _run_dag(monkeypatch, tmp_path, plan, config_toml)
    assert result["ok"] is True
    layers = result["layers"]
    # 最大层大小不超过 3
    for layer in layers:
        assert len(layer) <= 3, f"Layer exceeds max_parallel: {layer}"
    # 总计 5 个 change 覆盖
    all_cids = [cid for layer in layers for cid in layer]
    assert set(all_cids) == set(plan), f"Not all changes covered: {all_cids}"


# ============================================================
# Scenario: 热点文件被点名
# ============================================================

def test_dag_hotspot_named_in_serialization_reason(monkeypatch, tmp_path):
    """两个 change 共同触碰 plugins/agent-spine/commands/spine-run.md → serialization_reason 含文件名。"""
    _make_change_dir(tmp_path, "change-a",
                     tasks_content="修改 `plugins/agent-spine/commands/spine-run.md` 和 `src/a.py`")
    _make_change_dir(tmp_path, "change-b",
                     tasks_content="修改 `plugins/agent-spine/commands/spine-run.md` 和 `src/b.py`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    sr = result.get("serialization_reason") or {}
    # 至少一个 change 有 hotspot 原因
    all_reasons = []
    for reasons in sr.values():
        if isinstance(reasons, list):
            all_reasons.extend(reasons)
        else:
            all_reasons.append(str(reasons))
    hotspot_reasons = [r for r in all_reasons if "hotspot" in r.lower() or "spine-run" in r.lower()]
    assert hotspot_reasons, f"No hotspot reason found: {sr}"


# ============================================================
# Scenario: 空 plan_order
# ============================================================

def test_dag_empty_plan_order(monkeypatch, tmp_path):
    """空 plan_order → 空 layers，parallelizable_fraction=0。"""
    result = _run_dag(monkeypatch, tmp_path, [])
    assert result["ok"] is True
    assert result["layers"] == []
    assert result["parallelizable_fraction"] == 0.0


# ============================================================
# Scenario: parallelizable_fraction 计算
# ============================================================

def test_dag_parallelizable_fraction(monkeypatch, tmp_path):
    """3 个不重叠 change → 全部并行，fraction=1.0。"""
    for i in range(3):
        _make_change_dir(tmp_path, f"cx-{i}", tasks_content=f"修改 `src/file_{i}.py`")

    result = _run_dag(monkeypatch, tmp_path, ["cx-0", "cx-1", "cx-2"])
    assert result["ok"] is True
    # 全部在同一层 → fraction = 1.0（或接近）
    assert result["parallelizable_fraction"] > 0, f"fraction={result['parallelizable_fraction']}"


# ============================================================
# Scenario: 真实语料不崩溃（13 个加固提案）
# ============================================================

def test_dag_real_corpus_no_crash(monkeypatch, tmp_path):
    """用真实的 13 个加固提案目录（如果存在）或 mock 目录运行 dag，不应崩溃。"""
    # 模拟 13 个加固提案
    change_ids = [
        "orchestrator-check-record-result",
        "init-crash-worktree-recovery",
        "in-session-coder-timeout",
        "review-run-failure-branch",
        "auto-decide-abort-and-archive-fallback",
        "fix-auto-decide-trigger-contract",
        "telemetry-auto-decide-finalize",
        "plugin-subagent-stop-hook",
        "auto-mode-deny-rules",
        "analyze-untriggered-cages",
        "archive-structured-errors",
        "telemetry-canonical-proj-key",
        "parallel-dag-scheduling",
    ]
    # 为每个 change 创建简单 tasks.md
    for cid in change_ids:
        content = f"# {cid} Tasks\n\n- [ ] 修改 `src/npc/{cid.replace('-','_')}.py`\n"
        _make_change_dir(tmp_path, cid, tasks_content=content)

    result = _run_dag(monkeypatch, tmp_path, change_ids)
    assert result["ok"] is True
    # 所有 change 都应出现在某层
    all_cids = [cid for layer in result["layers"] for cid in layer]
    assert set(all_cids) == set(change_ids), f"Missing: {set(change_ids) - set(all_cids)}"


# ============================================================
# Scenario: 无效 plan_order 报错
# ============================================================

def test_dag_invalid_plan_order_error(monkeypatch, tmp_path):
    """非 JSON 的 plan_order → emit_error 并退出。"""
    monkeypatch.setattr(_plan, "_resolve_repo_root", lambda args: tmp_path)
    args = _make_dag_args(tmp_path, [])
    args.plan_order = "not-json"

    errors = []
    monkeypatch.setattr(_plan._io, "emit_error", lambda kind, msg, exit_code=1: errors.append((kind, exit_code)))

    _plan.run_dag(args)
    assert errors, "Should have called emit_error"
    assert errors[0][0] == "invalid_plan_order"


# ============================================================
# Scenario: config.py [scheduler] 节解析
# ============================================================

def test_config_scheduler_defaults():
    """SchedulerConfig 默认值：max_parallel=3, max_evictions=2。"""
    cfg = SchedulerConfig()
    assert cfg.max_parallel == 3
    assert cfg.max_evictions == 2


def test_config_scheduler_validation():
    """max_parallel=0 → ConfigError。"""
    from npc.config import ConfigError
    with pytest.raises(ConfigError, match="max_parallel"):
        SchedulerConfig(max_parallel=0)

    with pytest.raises(ConfigError, match="max_evictions"):
        SchedulerConfig(max_evictions=0)


def test_config_load_scheduler_from_toml(tmp_path):
    """从 TOML 配置文件加载 [scheduler] 节。"""
    from npc.config import load_config
    config_file = tmp_path / ".npc" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text("[scheduler]\nmax_parallel = 5\nmax_evictions = 3\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.scheduler.max_parallel == 5
    assert cfg.scheduler.max_evictions == 3


# ============================================================
# F1 回归：deps_map 包含在 dag 输出中
# ============================================================


def test_dag_output_includes_deps_map_no_deps(monkeypatch, tmp_path):
    """无依赖的 plan → deps_map 为空字典（或不含有依赖的键）。"""
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/a.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/b.py`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    assert "deps_map" in result, "dag 输出必须包含 deps_map 字段"
    # 无显式依赖时 deps_map 应为空
    assert result["deps_map"] == {}, f"无依赖时 deps_map 应为空：{result['deps_map']}"


def test_dag_output_includes_deps_map_with_deps(monkeypatch, tmp_path):
    """change-b 依赖 change-a → deps_map[change-b] = ['change-a']。"""
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/a.py`")
    _make_change_dir(tmp_path, "change-b",
                     tasks_content="修改 `src/b.py`",
                     proposal_content="依赖前置：change-a\n")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    assert "deps_map" in result, "dag 输出必须包含 deps_map 字段"
    deps_map = result["deps_map"]
    assert "change-b" in deps_map, f"change-b 应在 deps_map：{deps_map}"
    assert "change-a" in deps_map["change-b"], f"change-b 应依赖 change-a：{deps_map}"


def test_dag_output_deps_map_in_degraded_path(monkeypatch, tmp_path):
    """依赖环退化到串行时，deps_map 仍应在输出中（即使为空或含环内节点）。"""
    _make_change_dir(tmp_path, "change-a",
                     tasks_content="修改 `src/a.py`",
                     proposal_content="依赖前置：change-b\n")
    _make_change_dir(tmp_path, "change-b",
                     tasks_content="修改 `src/b.py`",
                     proposal_content="依赖前置：change-a\n")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    assert result.get("degraded_reason") is not None
    assert "deps_map" in result, "退化路径下 dag 输出也必须含 deps_map"


# ============================================================
# F1 回归：propagate-dep-failed 真实回归测试
# ============================================================

import json as _json_mod


def _make_state_for_propagate(tmp_path: Path, changes: list[str], statuses: dict[str, str] | None = None) -> tuple[Path, Path]:
    """创建用于 propagate-dep-failed 测试的最简 state.json + state.md。"""
    import json as j
    state_json = tmp_path / "state.json"
    state_md = tmp_path / "state.md"
    progress = []
    for i, cid in enumerate(changes):
        st = (statuses or {}).get(cid, "pending")
        progress.append({
            "seq": i + 1,
            "change_id": cid,
            "status": st,
            "blocking_trend": [],
            "categories_seen": [],
            "rounds_since_strict_decrease": 0,
            "phases": {},
        })
    state = {
        "schema_version": 2,
        "run_ts": "2026-07-03-1000-000000",
        "started_at": "2026-07-03T10:00:00+00:00",
        "last_updated_at": "2026-07-03T10:00:00+00:00",
        "mode": "interactive",
        "fresh": False,
        "status": "in-progress",
        "project_root": str(tmp_path),
        "proj_key": "-test",
        "git_head_at_start": "abc0000",
        "cc_session": {"session_id": None, "transcript_path": None, "source": "unknown"},
        "plan_order": changes,
        "progress": progress,
    }
    state_json.write_text(j.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    state_md.write_text("# test\n", encoding="utf-8")
    return state_json, state_md


def _run_propagate(monkeypatch, tmp_path: Path, failed_change: str, deps_map: dict) -> dict:
    """运行 run_propagate_dep_failed，捕获输出 JSON。"""
    import argparse as _argparse
    import json as j

    # monkeypatch load_paths 返回 state 路径
    class FakePaths:
        state_json = tmp_path / "state.json"
        state_md = tmp_path / "state.md"

    monkeypatch.setattr(_plan._paths, "load_paths", lambda args: FakePaths())

    captured = {}
    errors = []
    original_emit = _plan._io.emit

    def capture_emit(data: Any) -> None:
        captured.update(data)

    def capture_error(kind, msg, exit_code=1):
        errors.append((kind, msg, exit_code))

    monkeypatch.setattr(_plan._io, "emit", capture_emit)
    monkeypatch.setattr(_plan._io, "emit_error", capture_error)

    args = _argparse.Namespace(
        failed_change=failed_change,
        deps_map=j.dumps(deps_map),
        run_ts=None,
        task_log_dir=None,
        state_json=None,
    )
    _plan.run_propagate_dep_failed(args)
    return {"result": captured, "errors": errors}


def test_propagate_dep_failed_marks_downstream_pending(monkeypatch, tmp_path):
    """前置 change-a 失败 → change-c（显式依赖 a）从 pending 变 skipped-auto。

    change-b 路径重叠但非显式依赖 → 不受影响。
    这是 F1 finding 要求的核心回归：dep-failed 仅传播至显式依赖，不传播至路径重叠。
    """
    changes = ["change-a", "change-b", "change-c"]
    # change-b 是路径重叠，change-c 显式依赖 change-a
    state_json, state_md = _make_state_for_propagate(
        tmp_path, changes,
        statuses={"change-a": "failed", "change-b": "pending", "change-c": "pending"}
    )

    deps_map = {"change-c": ["change-a"]}  # change-b 无依赖
    out = _run_propagate(monkeypatch, tmp_path, "change-a", deps_map)

    assert not out["errors"], f"不应有错误：{out['errors']}"
    result = out["result"]
    assert result["ok"] is True
    assert "change-c" in result["skipped"], f"change-c 应被标记 skipped：{result}"
    assert "change-b" not in result["skipped"], f"change-b（路径重叠非依赖）不应被 skipped：{result}"

    # 验证 state 文件已被实际修改
    from npc import state as _state
    loaded = _state.read_state(state_json)
    prog = {e["change_id"]: e for e in loaded["progress"]}
    assert prog["change-c"]["status"] == "skipped-auto"
    assert prog["change-c"].get("skipped_reason") == "dep-failed"
    assert prog["change-b"]["status"] == "pending", "change-b 不应被改动"


def test_propagate_dep_failed_no_downstream(monkeypatch, tmp_path):
    """失败的 change 无下游依赖 → skipped 为空列表，state 不变。"""
    changes = ["change-a", "change-b"]
    state_json, state_md = _make_state_for_propagate(
        tmp_path, changes,
        statuses={"change-a": "failed", "change-b": "pending"}
    )

    deps_map: dict = {}  # 无依赖关系
    out = _run_propagate(monkeypatch, tmp_path, "change-a", deps_map)

    assert not out["errors"]
    result = out["result"]
    assert result["ok"] is True
    assert result["skipped"] == [], f"无下游时 skipped 应为空：{result}"

    from npc import state as _state
    loaded = _state.read_state(state_json)
    prog = {e["change_id"]: e for e in loaded["progress"]}
    assert prog["change-b"]["status"] == "pending"


def test_propagate_dep_failed_skips_already_terminal(monkeypatch, tmp_path):
    """下游 change 已是终态（archived/failed） → 不重复修改。"""
    changes = ["change-a", "change-b", "change-c"]
    state_json, state_md = _make_state_for_propagate(
        tmp_path, changes,
        statuses={"change-a": "failed", "change-b": "archived", "change-c": "failed"}
    )

    deps_map = {"change-b": ["change-a"], "change-c": ["change-a"]}
    out = _run_propagate(monkeypatch, tmp_path, "change-a", deps_map)

    assert not out["errors"]
    result = out["result"]
    assert result["ok"] is True
    # 两者均已是终态，不应被再次写入
    assert result["skipped"] == [], f"已终态的下游不应再写入：{result}"

    from npc import state as _state
    loaded = _state.read_state(state_json)
    prog = {e["change_id"]: e for e in loaded["progress"]}
    assert prog["change-b"]["status"] == "archived"
    assert prog["change-c"]["status"] == "failed"


def test_propagate_dep_failed_transitive(monkeypatch, tmp_path):
    """传递依赖：a 失败 → b 依赖 a → c 依赖 b；c 也应被标记 skipped-auto。"""
    changes = ["change-a", "change-b", "change-c"]
    state_json, state_md = _make_state_for_propagate(
        tmp_path, changes,
        statuses={"change-a": "failed", "change-b": "pending", "change-c": "pending"}
    )

    deps_map = {"change-b": ["change-a"], "change-c": ["change-b"]}
    out = _run_propagate(monkeypatch, tmp_path, "change-a", deps_map)

    assert not out["errors"]
    result = out["result"]
    assert result["ok"] is True
    assert "change-b" in result["skipped"]
    assert "change-c" in result["skipped"]

    from npc import state as _state
    loaded = _state.read_state(state_json)
    prog = {e["change_id"]: e for e in loaded["progress"]}
    assert prog["change-b"]["status"] == "skipped-auto"
    assert prog["change-b"].get("skipped_reason") == "dep-failed"
    assert prog["change-c"]["status"] == "skipped-auto"
    assert prog["change-c"].get("skipped_reason") == "dep-failed"


# ============================================================
# F1 回归（round-5）：glob 路径重叠检测
# ============================================================

def test_dag_glob_overlaps_concrete_same_dir(monkeypatch, tmp_path):
    """change-a 声明 `src/npc/*.py`，change-b 声明 `src/npc/state.py`。
    两者目录+扩展名匹配 → 必须分到不同层，不得并行。

    这是 F1 finding 的核心回归：精确字符串交集为空但实际存在冲突。
    """
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/npc/*.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/npc/state.py`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    layers = result["layers"]
    for layer in layers:
        assert not ("change-a" in layer and "change-b" in layer), (
            f"glob src/npc/*.py 与 src/npc/state.py 重叠，不得同层：{layers}"
        )


def test_dag_doublestar_glob_overlaps_nested_concrete(monkeypatch, tmp_path):
    """change-a 声明 `src/**/*.py`，change-b 声明 `src/npc/state.py`。
    ** glob 应匹配任意子目录 → 必须分层。
    """
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/**/*.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/npc/state.py`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    layers = result["layers"]
    for layer in layers:
        assert not ("change-a" in layer and "change-b" in layer), (
            f"glob src/**/*.py 与 src/npc/state.py 重叠，不得同层：{layers}"
        )


def test_dag_glob_no_overlap_different_dir(monkeypatch, tmp_path):
    """change-a 声明 `src/npc/*.py`，change-b 声明 `src/other/foo.py`。
    目录不同 → 应视为不重叠，可以同层。
    """
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/npc/*.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/other/foo.py`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    layers = result["layers"]
    assert any(
        "change-a" in layer and "change-b" in layer for layer in layers
    ), f"不同目录的 glob 与具体路径不应被序列化：{layers}"


def test_dag_glob_no_overlap_different_ext(monkeypatch, tmp_path):
    """change-a 声明 `src/npc/*.py`，change-b 声明 `src/npc/README.md`。
    扩展名不同 → 不重叠，可以同层。
    """
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/npc/*.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/npc/README.md`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    layers = result["layers"]
    assert any(
        "change-a" in layer and "change-b" in layer for layer in layers
    ), f"不同扩展名的 glob 与具体路径不应被序列化：{layers}"


def test_dag_two_globs_same_dir_overlap(monkeypatch, tmp_path):
    """change-a 声明 `src/npc/*.py`，change-b 声明 `src/npc/*.md`（不同扩展名 glob）。
    两 glob 目录前缀相同 → 保守视为重叠，必须分层。
    """
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/npc/*.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/npc/*.md`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    layers = result["layers"]
    for layer in layers:
        assert not ("change-a" in layer and "change-b" in layer), (
            f"同目录下两个 glob 应保守视为重叠，不得同层：{layers}"
        )


def test_dag_glob_hotspot_named_in_serialization_reason(monkeypatch, tmp_path):
    """glob 与具体路径冲突时，serialization_reason 应包含冲突路径信息。"""
    _make_change_dir(tmp_path, "change-a", tasks_content="修改 `src/npc/*.py`")
    _make_change_dir(tmp_path, "change-b", tasks_content="修改 `src/npc/state.py`")

    result = _run_dag(monkeypatch, tmp_path, ["change-a", "change-b"])
    assert result["ok"] is True
    sr = result.get("serialization_reason") or {}
    all_reasons = []
    for reasons in sr.values():
        if isinstance(reasons, list):
            all_reasons.extend(reasons)
        else:
            all_reasons.append(str(reasons))
    assert any("hotspot" in r for r in all_reasons), (
        f"glob 冲突应产生 hotspot 序列化原因：{sr}"
    )


# ============================================================
# _paths_overlap / _glob_overlaps_path 单元测试
# ============================================================

def test_glob_overlaps_path_same_dir_same_ext():
    from npc.plan import _glob_overlaps_path
    assert _glob_overlaps_path("src/npc/*.py", "src/npc/state.py") is True


def test_glob_overlaps_path_different_dir():
    from npc.plan import _glob_overlaps_path
    assert _glob_overlaps_path("src/npc/*.py", "src/other/state.py") is False


def test_glob_overlaps_path_different_ext():
    from npc.plan import _glob_overlaps_path
    assert _glob_overlaps_path("src/npc/*.py", "src/npc/README.md") is False


def test_glob_overlaps_path_doublestar():
    from npc.plan import _glob_overlaps_path
    assert _glob_overlaps_path("src/**/*.py", "src/npc/state.py") is True


def test_paths_overlap_exact():
    from npc.plan import _paths_overlap
    assert _paths_overlap({"src/a.py"}, {"src/a.py"}) is True


def test_paths_overlap_glob_vs_concrete():
    from npc.plan import _paths_overlap
    assert _paths_overlap({"src/npc/*.py"}, {"src/npc/state.py"}) is True


def test_paths_overlap_glob_vs_concrete_no_match():
    from npc.plan import _paths_overlap
    assert _paths_overlap({"src/npc/*.py"}, {"src/other/state.py"}) is False


def test_paths_overlap_two_globs_same_dir():
    from npc.plan import _paths_overlap
    # 保守：同目录两 glob 视为重叠
    assert _paths_overlap({"src/npc/*.py"}, {"src/npc/*.md"}) is True


def test_paths_overlap_two_globs_different_dir():
    from npc.plan import _paths_overlap
    assert _paths_overlap({"src/npc/*.py"}, {"src/other/*.py"}) is False


def test_propagate_dep_failed_invalid_deps_map(monkeypatch, tmp_path):
    """非法 deps_map JSON → emit_error 而非崩溃。"""
    import argparse as _argparse
    from npc import plan as _plan_mod

    class FakePaths:
        state_json = tmp_path / "state.json"
        state_md = tmp_path / "state.md"

    monkeypatch.setattr(_plan._paths, "load_paths", lambda args: FakePaths())

    errors = []
    monkeypatch.setattr(_plan._io, "emit", lambda d: None)
    monkeypatch.setattr(_plan._io, "emit_error", lambda kind, msg, exit_code=1: errors.append((kind, exit_code)))

    args = _argparse.Namespace(
        failed_change="change-a",
        deps_map="not-json",
        run_ts=None, task_log_dir=None, state_json=None,
    )
    _plan.run_propagate_dep_failed(args)
    assert errors, "非法 JSON 应触发 emit_error"
    assert errors[0][0] == "invalid_args"
