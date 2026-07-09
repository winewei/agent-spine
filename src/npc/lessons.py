"""``npc lessons record`` / ``npc lessons gate`` —— run 级失败模式前馈。

一个 run 内多个 change 互不共享经验：change A 在 fix 循环里反复踩的坑，change B
只能重新交学费。本模块补两条确定性连线：

- **record**（``run-lessons-extraction``）：某 change archive 成功后，从其
  ``<base>/events.jsonl`` 里 ``fix-rN`` 的 done 事件确定性提炼
  ``categories_scanned`` / ``regressions_added`` / ``notes`` 三个 fixer 自报字段，
  拼接成一条 markdown 段落追加到 ``<run_dir>/lessons.md``。**只读这三个字段**，
  MUST NOT 打开或引用任何 reviewer 产出（``round-N.review.json`` /
  ``round-N.focus.md`` / ``*.spec-review.json``）——守核心不变量 1「生成 ⊥ 验证」。

- **gate**（``pilot-rewrite-gate``）：DAG 层屏障之后的只读候选判定 + 决策落盘。
  ``gate_candidates`` 纯函数式算出「层号 > 当前层 且 status==pending」的下游 change，
  并报告 lessons.md 相对游标是否有新增条目；``apply_gate_decision`` 校验 targets、
  落 ``state.lessons.gate_decisions`` 历史、推进 ``gate_processed_cursor``。

设计参见 ``openspec/changes/run-lessons-feedforward/design.md``。所有动作皆确定性，
不调用任何 LLM，不做语义摘要。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from . import _io, paths as _paths, state as _state


# fix-rN phase → round 数字
_FIX_PHASE_RE = re.compile(r"^fix-r(\d+)$")
# lessons.md 段落标题：`## <change_id> (...)` —— change_id 首个非空白 token
_HEADING_RE = re.compile(r"^##\s+(\S+)")

# 单条 notes 自报文本的截断上限（防 lessons.md 无限增长；与 spec_report.MD_LINE_LIMIT
# 同类惯例——机械裁剪，不做语义摘要）。
NOTES_MAX_LEN = 200


# ============================================================
# lessons.md 读取 helper（纯函数）
# ============================================================


def _entry_change_ids(lessons_path: Path) -> list[str]:
    """按出现顺序返回 lessons.md 中所有段落的 change_id。"""
    if not lessons_path.is_file():
        return []
    try:
        text = lessons_path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


def _entry_count(lessons_path: Path) -> int:
    return len(_entry_change_ids(lessons_path))


def _heading_exists(lessons_path: Path, change_id: str) -> bool:
    return change_id in _entry_change_ids(lessons_path)


# ============================================================
# events.jsonl 提炼（纯函数，只读 fixer 自报字段）
# ============================================================


def _split_csv(raw: Any) -> list[str]:
    """把 ``categories_scanned`` / ``regressions_added`` 的 csv 自报拆成去空/去 ``-`` 列表。"""
    if raw is None:
        return []
    s = str(raw).strip()
    if not s or s == "-":
        return []
    return [c.strip() for c in s.split(",") if c.strip() and c.strip() != "-"]


def _clean_notes(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s or s == "-":
        return ""
    if len(s) > NOTES_MAX_LEN:
        s = s[: NOTES_MAX_LEN - 1].rstrip() + "…"
    return s


def _parse_fix_done_events(events_path: Path) -> list[dict]:
    """读取 ``events.jsonl``，返回按 round 升序的 fix-rN done 事件精炼列表。

    过滤契约锚定 **npc 真实落盘形态**：``npc phase exit <seq> fix-rN --status done``
    （见 ``events.py``）向 per-change ``events.jsonl`` 追加的行是
    ``{"event":"fix.done","phase":"fix-rN", <fixer 自报字段...>}``——该文件用 ``event``
    字段命名事件、以 ``.done`` 后缀编码成功退出，**行内不含 ``kind`` / ``status``**。
    因此这里筛 ``event == "fix.done" && phase`` 匹配 ``^fix-r\\d+$``，取三个 fixer
    自报字段。（``kind == "phase.exit" && status == "done"`` 是另一条 telemetry 派生流
    ``events.ndjson`` 的形态，且该流不携带 ``categories_scanned`` / ``regressions_added``
    / ``notes``，无法用作提炼源——见 design.md D1。）逐行 best-effort 解析：无法解析的
    行静默跳过（不抛栈），MUST NOT 打开任何 reviewer 产出文件。
    """
    rounds: dict[int, dict] = {}
    text = events_path.read_text(encoding="utf-8")  # OSError 由调用方兜底
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != "fix.done":
            continue
        phase = ev.get("phase") or ""
        m = _FIX_PHASE_RE.match(str(phase))
        if not m:
            continue
        rn = int(m.group(1))
        # 同一 round 若出现多条（异常重放），后写覆盖前写（确定性取最后一条）
        rounds[rn] = {
            "round": rn,
            "categories_scanned": _split_csv(ev.get("categories_scanned")),
            "regressions_added": _split_csv(ev.get("regressions_added")),
            "notes": _clean_notes(ev.get("notes")),
        }
    return [rounds[k] for k in sorted(rounds.keys())]


def _build_entry_md(change_id: str, archive_commit: str | None, fix_rounds: list[dict]) -> str:
    """把提炼结果确定性拼接为一条 markdown 段落。

    格式（design D2）：标题含 archive 短 hash + fix 轮数；categories/regressions 为
    全轮去重并集（排序保证确定性）；notes 按 round 顺序逐条列出。字段为空时对应子项
    省略；全空时仍保留 ``- rounds: N`` 最简信号。
    """
    rounds = len(fix_rounds)
    short = (archive_commit or "").strip()[:8]
    if short:
        heading = f"## {change_id} (archived {short}, {rounds} fix rounds)"
    else:
        heading = f"## {change_id} ({rounds} fix rounds)"

    cats: set[str] = set()
    regs: set[str] = set()
    for r in fix_rounds:
        cats.update(r["categories_scanned"])
        regs.update(r["regressions_added"])

    lines = [heading, f"- rounds: {rounds}"]
    if cats:
        lines.append(f"- categories_scanned: {', '.join(sorted(cats))}")
    if regs:
        lines.append(f"- regressions_added: {', '.join(sorted(regs))}")
    note_lines = [(r["round"], r["notes"]) for r in fix_rounds if r["notes"]]
    if note_lines:
        lines.append("- notes:")
        for rn, note in note_lines:
            lines.append(f"  - r{rn}: {note}")
    return "\n".join(lines) + "\n"


# ============================================================
# state.lessons 节 helper
# ============================================================


def _lessons_node(state: dict) -> dict:
    """读取 ``state.lessons`` 节；旧 state 缺该节按空值解释。"""
    node = state.get("lessons")
    if not isinstance(node, dict):
        node = {}
    node.setdefault("entries_appended", [])
    node.setdefault("gate_processed_cursor", 0)
    node.setdefault("gate_decisions", [])
    return node


# ============================================================
# record：提炼并追加
# ============================================================


def extract_and_append(p: _paths.Paths, seq: int) -> dict:
    """从 change[seq] 的 events.jsonl 提炼失败模式并追加到 run 级 lessons.md。

    返回 ``{ok, appended, lessons_path, change_id, reason?}``。best-effort：任何失败
    返回结构化 ``{ok:false, error:...}``，MUST NOT 抛栈。整个读-判-追加-落状态在 run 级
    互斥锁内串行，避免并行层多 change 同时追加时的竞态/重复。
    """
    lessons_path = p.run_dir / "lessons.md"

    try:
        lock_fh = _state.acquire_state_lock(p.state_json)
    except _state.StateLockError as e:
        return {"ok": False, "error": "state-lock-timeout", "detail": str(e)}
    try:
        try:
            state = _state.read_state(p.state_json)
        except FileNotFoundError:
            return {"ok": False, "error": "state-missing"}
        except json.JSONDecodeError as e:
            return {"ok": False, "error": "state-invalid", "detail": str(e)}

        progress = state.get("progress") or []
        if not (1 <= seq <= len(progress)):
            return {"ok": False, "error": "seq-out-of-range", "seq": seq}
        entry = progress[seq - 1]
        change_id = entry.get("change_id")

        node = _lessons_node(state)

        # 幂等：state 已记录 或 lessons.md 已含该段落 → 跳过
        if change_id in node["entries_appended"] or _heading_exists(lessons_path, change_id):
            return {
                "ok": True,
                "appended": False,
                "reason": "already-recorded",
                "lessons_path": str(lessons_path),
                "change_id": change_id,
            }

        base_str = entry.get("base")
        base = Path(base_str) if base_str else _paths.base_for(p, seq, change_id)
        events_path = base / "events.jsonl"
        if not events_path.is_file():
            return {
                "ok": False,
                "error": "events-missing",
                "lessons_path": str(lessons_path),
                "change_id": change_id,
            }

        try:
            fix_rounds = _parse_fix_done_events(events_path)
        except OSError as e:
            return {"ok": False, "error": "events-read-failed", "detail": str(e), "change_id": change_id}

        # 无 fix 轮（round 0 review 即通过）→ 无失败模式可提炼，不追加
        if not fix_rounds:
            return {
                "ok": True,
                "appended": False,
                "reason": "no-fix-rounds",
                "lessons_path": str(lessons_path),
                "change_id": change_id,
            }

        md = _build_entry_md(change_id, entry.get("archive_commit"), fix_rounds)

        try:
            lessons_path.parent.mkdir(parents=True, exist_ok=True)
            existing = ""
            if lessons_path.is_file():
                existing = lessons_path.read_text(encoding="utf-8")
            # 段落之间留一个空行分隔
            sep = "" if (not existing or existing.endswith("\n\n")) else ("\n" if existing.endswith("\n") else "\n\n")
            lessons_path.write_text(existing + sep + md, encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": "lessons-write-failed", "detail": str(e), "change_id": change_id}

        # 落 state.lessons.entries_appended（同锁内，避免重复 record）
        def _mutate(st: dict) -> None:
            n = _lessons_node(st)
            if change_id not in n["entries_appended"]:
                n["entries_appended"].append(change_id)
            st["lessons"] = n

        _state.update_state(p.state_json, p.state_md, _mutate, use_lock=False)

        return {
            "ok": True,
            "appended": True,
            "lessons_path": str(lessons_path),
            "change_id": change_id,
        }
    finally:
        _state.release_state_lock(lock_fh)


# ============================================================
# gate：候选判定 + 决策落盘
# ============================================================


def gate_candidates(p: _paths.Paths, layer_idx: int) -> dict:
    """只读地算出层屏障后的候选下游集合与 lessons 增量。

    候选集 = ``dag_layer > layer_idx && status == "pending"`` 的 change（按 plan_order 顺序）。
    ``has_candidates`` = 候选集非空 AND lessons.md 存在游标之后的新增条目（逻辑与）。
    """
    lessons_path = p.run_dir / "lessons.md"
    try:
        state = _state.read_state(p.state_json)
    except FileNotFoundError:
        return {"ok": False, "error": "state-missing"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": "state-invalid", "detail": str(e)}

    progress = state.get("progress") or []
    candidates: list[str] = []
    for entry in progress:
        dag_layer = entry.get("dag_layer")
        if dag_layer is None:
            continue
        if int(dag_layer) > layer_idx and entry.get("status") == "pending":
            candidates.append(entry.get("change_id"))

    node = _lessons_node(state)
    cursor = int(node.get("gate_processed_cursor") or 0)
    total_entries = _entry_count(lessons_path)
    new_entry_count = max(0, total_entries - cursor)

    has_candidates = bool(candidates) and new_entry_count > 0
    return {
        "ok": True,
        "has_candidates": has_candidates,
        "layer_idx": layer_idx,
        "candidates": candidates,
        "new_entry_count": new_entry_count,
        "cursor": cursor,
        "total_entries": total_entries,
        "lessons_path": str(lessons_path),
    }


def apply_gate_decision(
    p: _paths.Paths, layer_idx: int, targets: list[str], decision: str
) -> dict:
    """落盘本次闸口决策并推进游标。

    ``decision == "rewrite"`` 时先校验每个 target 属于本次候选集（``dag_layer > layer_idx
    && status == "pending"``）；任一不满足则返回 ``{ok:false, error:"target-not-candidate"}``，
    **不**写 gate_decisions、**不**推进游标。``skip-rewrite`` 不受 targets 约束。
    """
    if decision not in ("rewrite", "skip-rewrite"):
        return {"ok": False, "error": "invalid-decision", "decision": decision}

    targets = [t for t in (targets or []) if t]

    try:
        lock_fh = _state.acquire_state_lock(p.state_json)
    except _state.StateLockError as e:
        return {"ok": False, "error": "state-lock-timeout", "detail": str(e)}
    try:
        try:
            state = _state.read_state(p.state_json)
        except FileNotFoundError:
            return {"ok": False, "error": "state-missing"}
        except json.JSONDecodeError as e:
            return {"ok": False, "error": "state-invalid", "detail": str(e)}

        progress = state.get("progress") or []
        candidate_set = {
            entry.get("change_id")
            for entry in progress
            if entry.get("dag_layer") is not None
            and int(entry.get("dag_layer")) > layer_idx
            and entry.get("status") == "pending"
        }

        if decision == "rewrite":
            invalid = [t for t in targets if t not in candidate_set]
            if invalid:
                return {
                    "ok": False,
                    "error": "target-not-candidate",
                    "invalid_targets": invalid,
                    "candidates": sorted(candidate_set),
                }

        lessons_path = p.run_dir / "lessons.md"
        new_cursor = _entry_count(lessons_path)
        ts = _io.now_iso()

        def _mutate(st: dict) -> None:
            node = _lessons_node(st)
            node["gate_decisions"].append(
                {
                    "layer_idx": layer_idx,
                    "targets": list(targets),
                    "decision": decision,
                    "ts": ts,
                }
            )
            node["gate_processed_cursor"] = new_cursor
            st["lessons"] = node

        _state.update_state(p.state_json, p.state_md, _mutate, use_lock=False)

        return {
            "ok": True,
            "layer_idx": layer_idx,
            "targets": list(targets),
            "decision": decision,
            "cursor": new_cursor,
        }
    finally:
        _state.release_state_lock(lock_fh)


# ============================================================
# CLI handlers
# ============================================================


def record(args: argparse.Namespace) -> None:
    """``npc lessons record --seq N``（best-effort，失败也 exit 0，调用方非阻塞）。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit({"ok": False, "error": "env_missing", "message": str(e)})
        return
    result = extract_and_append(p, args.seq)
    _io.emit(result)


def gate(args: argparse.Namespace) -> None:
    """``npc lessons gate --layer-idx N [--apply --targets <csv> --decision ...]``。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    if getattr(args, "apply", False):
        targets = [t.strip() for t in (args.targets or "").split(",") if t.strip()]
        decision = args.decision
        if decision is None:
            _io.emit_error("missing_decision", "--apply 需要 --decision rewrite|skip-rewrite", exit_code=2)
            return
        result = apply_gate_decision(p, args.layer_idx, targets, decision)
        _io.emit(result)
        return

    result = gate_candidates(p, args.layer_idx)
    _io.emit(result)
