"""npc plan waves —— 并行波次候选划分（v3 编排的机械基线）。

两段式划分：

1. Kahn 拓扑分层：依赖 DAG（边 A→B 表示 A 必须先于 B 完成）逐层剥离
   in-degree=0 的节点；遇环时强制释放 tie-break 序最小的节点并记录破环点。
2. 层内文件着色：同一子波次内任意两个 change 的 affected-code 路径集无重叠
   （贪心图着色）。重叠按**路径前缀**判定而非字符串相等——files 由 LLM 从
   proposal 抽取，常含目录级保守标识（如 ``app/services/``），目录级条目
   必须与其下所有具体文件视为冲突。路径无重叠的子波次才能安全地并行
   worktree implement + 串行 cherry-pick 整合。

本命令输出的是**候选**划分——语义层耦合（共享状态/时序/不变量）机械层看不到，
最终波次必须经架构师 sub-agent 裁定（见 /new-plan-changes-v3 §4.9）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict

from . import _io


def _key(node: str, tie_break: dict) -> tuple:
    tb = tie_break.get(node)
    if isinstance(tb, list) and tb:
        tier = tb[0] if len(tb) > 0 else 9_999
        scope = tb[1] if len(tb) > 1 else 9_999
        return (tier, scope, node)
    return (9_999, 9_999, node)


def topological_layers(
    nodes: list[str], edges: list[list[str]], tie_break: dict
) -> tuple[list[list[str]], list[str]]:
    """Kahn 分层。返回 (layers, cycle_broken)。

    每层 = 同一步 in-degree 归零的节点集，按 tie_break 序排序保证确定性。
    存在环时，强制释放剩余节点中 tie-break 序最小者并记入 cycle_broken。
    """
    nodeset = set(nodes)
    indeg = {n: 0 for n in nodes}
    adj = defaultdict(list)
    for e in edges:
        if len(e) != 2:
            continue
        u, v = e[0], e[1]
        if u not in nodeset or v not in nodeset or u == v:
            continue
        adj[u].append(v)
        indeg[v] += 1

    layers: list[list[str]] = []
    cycle_broken: list[str] = []
    remaining = dict(indeg)
    placed: set[str] = set()

    while len(placed) < len(nodes):
        ready = sorted(
            [n for n in nodes if n not in placed and remaining[n] == 0],
            key=lambda n: _key(n, tie_break),
        )
        if not ready:
            stuck = sorted(
                [n for n in nodes if n not in placed],
                key=lambda n: _key(n, tie_break),
            )
            forced = stuck[0]
            cycle_broken.append(forced)
            remaining[forced] = 0
            ready = [forced]
        layers.append(ready)
        for n in ready:
            placed.add(n)
        for n in ready:
            for m in adj[n]:
                if m not in placed:
                    remaining[m] -= 1
    return layers, cycle_broken


def _parts(path) -> tuple:
    """路径归一化为组件元组；空段与 ``.`` 段剔除，尾部 ``/`` 自然消失。"""
    return tuple(seg for seg in str(path).strip().split("/") if seg not in ("", "."))


def _overlaps(a: tuple, b: tuple) -> bool:
    """路径重叠：归一化后相等，或一方是另一方的组件前缀（目录覆盖其下文件）。"""
    if not a or not b:
        return False
    k = min(len(a), len(b))
    return a[:k] == b[:k]


def _conflict(fa: set, fb: set) -> bool:
    return any(_overlaps(a, b) for a in fa for b in fb)


def split_by_files(
    layer: list[str], files: dict, tie_break: dict
) -> tuple[list[list[str]], list[list[str]]]:
    """层内贪心着色：两 change 的路径集存在重叠即冲突，须落到不同子波次。

    重叠按 ``_overlaps`` 的前缀语义判定：files 是 LLM 抽取的影响集，
    目录级条目（``app/services/``）是保守冲突标识，必须命中其下具体文件。
    确定性：按 tie_break 序处理节点，分配到首个无冲突的颜色。
    返回 (sub_waves, conflict_pairs)。
    """
    ordered = sorted(layer, key=lambda n: _key(n, tie_break))
    fileset = {n: {_parts(f) for f in (files.get(n) or []) if _parts(f)} for n in ordered}
    colors: list[list[str]] = []
    color_files: list[set] = []
    conflicts: list[list[str]] = []

    for n in ordered:
        fn = fileset[n]
        assigned = None
        for i, used in enumerate(color_files):
            if not _conflict(fn, used):
                assigned = i
                break
            for m in colors[i]:
                if _conflict(fileset[m], fn):
                    conflicts.append(sorted([m, n]))
        if assigned is None:
            colors.append([])
            color_files.append(set())
            assigned = len(colors) - 1
        colors[assigned].append(n)
        color_files[assigned] |= fn

    seen: set[tuple] = set()
    uniq_conflicts: list[list[str]] = []
    for pair in conflicts:
        key = tuple(pair)
        if key not in seen:
            seen.add(key)
            uniq_conflicts.append(pair)
    return colors, uniq_conflicts


def compute(data: dict) -> dict:
    """纯函数：{nodes, edges?, files?, tie_break?} → {waves, layers, split_reasons, cycle}。

    输入不合法抛 ValueError。
    """
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("missing or empty 'nodes'")
    if not all(isinstance(n, str) and n for n in nodes):
        raise ValueError("'nodes' entries must be non-empty strings")
    dupes = sorted({n for n in nodes if nodes.count(n) > 1})
    if dupes:
        raise ValueError(f"duplicate nodes: {dupes}")
    edges = data.get("edges") or []
    files = data.get("files") or {}
    tie_break = data.get("tie_break") or {}

    layers, cycle = topological_layers(nodes, edges, tie_break)

    waves: list[list[str]] = []
    split_reasons: list[dict] = []
    for idx, layer in enumerate(layers):
        sub_waves, conflicts = split_by_files(layer, files, tie_break)
        waves.extend(sub_waves)
        if len(sub_waves) > 1:
            shared_set: set[str] = set()
            for pair in conflicts:
                for n in pair:
                    others = [
                        _parts(g) for m in pair if m != n for g in (files.get(m) or [])
                    ]
                    for f in files.get(n) or []:
                        if any(_overlaps(_parts(f), g) for g in others):
                            shared_set.add(f)
            shared = sorted(shared_set)
            split_reasons.append(
                {
                    "layer": idx,
                    "members": layer,
                    "sub_waves": sub_waves,
                    "serialized_pairs": conflicts,
                    "shared_files": shared,
                }
            )

    return {
        "waves": waves,
        "layers": layers,
        "split_reasons": split_reasons,
        "cycle": cycle,
    }


def run(args: argparse.Namespace) -> None:
    """``npc plan waves [--input FILE]``（默认读 stdin）。

    退出码：0 成功；2 输入不合法（缺 nodes / 非 JSON / 文件不可读）。
    """
    try:
        if args.input:
            with open(args.input, "r", encoding="utf-8") as fh:
                raw = fh.read()
        else:
            raw = sys.stdin.read()
    except OSError as e:
        _io.emit_error("input_unreadable", f"无法读取输入：{e}", exit_code=2)
        return

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _io.emit_error("invalid_json", f"输入不是合法 JSON：{e}", exit_code=2)
        return

    try:
        out = compute(data)
    except ValueError as e:
        _io.emit_error("invalid_input", str(e), exit_code=2)
        return

    _io.emit(out)
    _io.info(
        f"waves: {len(data['nodes'])} changes -> {len(out['layers'])} layers -> "
        f"{len(out['waves'])} sub-waves (sizes: {[len(w) for w in out['waves']]})"
    )
    if out["cycle"]:
        _io.warn(f"waves: cycle broken at {out['cycle']}")
