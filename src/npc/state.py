"""STATE_JSON 读写与 STATE_MD 同步渲染。

每次写 STATE_JSON 都自动重新渲染 STATE_MD，杜绝二者漂移（v1 历史事故）。
JSON 写入采用 tmp + os.replace 原子替换。

CLI handlers：init_run / get / add_change / set_progress / finalize
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import _io, paths as _paths


SCHEMA_VERSION = 2

VALID_PROGRESS_STATUS = {
    "pending",
    "implementing",
    "reviewing",
    "in-fix-loop",
    "archived",
    "failed",
    "needs-user-decision",
    "skipped-auto",
}

VALID_TOP_STATUS = {"in-progress", "completed", "completed-with-issues", "aborted"}


# ----------------------------- 读写核心 -----------------------------


def read_state(state_json: Path) -> dict:
    """读 STATE_JSON。文件缺失或解析失败均抛 FileNotFoundError / json.JSONDecodeError。"""
    with state_json.open("r", encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_text(path: Path, content: str) -> None:
    """tmp 文件 + os.replace 原子替换。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def write_state(state_json: Path, state_md: Path, state: dict) -> None:
    """原子写 STATE_JSON，紧接着重新渲染 STATE_MD。

    last_updated_at 在此处统一更新；调用方无需手工设置。
    """
    state["last_updated_at"] = _io.now_iso()
    json_text = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(state_json, json_text)
    md_text = render_state_md(state)
    _atomic_write_text(state_md, md_text)


def update_state(
    state_json: Path,
    state_md: Path,
    mutator: Callable[[dict], None],
) -> dict:
    """读 → mutator 就地修改 → 写。返回修改后的 state。"""
    state = read_state(state_json)
    mutator(state)
    write_state(state_json, state_md, state)
    return state


# ----------------------------- MD 渲染 -----------------------------


_PHASE_ORDER_RE = re.compile(r"^(implement|review-r(\d+)|fix-r(\d+)|archive)$")


def _phase_sort_key(phase: str) -> tuple[int, int]:
    """phase 排序 key：按 round 编号分组，同 round 内 fix 在 review 之前。

    顺序：implement(0,0) → review-r0(0,1) → fix-r1(1,0) → review-r1(1,1)
         → fix-r2(2,0) → review-r2(2,1) → ... → archive(∞,0)
    """
    if phase == "implement":
        return (0, 0)
    if phase == "review-r0":
        return (0, 1)
    if phase == "archive":
        return (10**9, 0)
    m = _PHASE_ORDER_RE.match(phase)
    if not m:
        return (10**9 + 1, 0)
    if phase.startswith("fix-r"):
        return (int(m.group(3)), 0)
    if phase.startswith("review-r"):
        return (int(m.group(2)), 1)
    return (10**9 + 1, 0)


def _fmt_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "?"
    s = ms // 1000
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _phases_summary(phases: dict) -> str:
    """phases 字典渲染为 'implement(2m 06s) → review-r0(1m 15s) → archive(4s)' 形式。"""
    keys = sorted(phases.keys(), key=_phase_sort_key)
    parts = []
    for k in keys:
        v = phases.get(k) or {}
        d = v.get("duration_ms")
        parts.append(f"{k}({_fmt_duration_ms(d)})")
    return " → ".join(parts) if parts else "—"


def _total_duration_ms(phases: dict) -> int | None:
    total = 0
    has_any = False
    for v in phases.values():
        d = (v or {}).get("duration_ms")
        if d is not None:
            total += int(d)
            has_any = True
    return total if has_any else None


def _current_phase_label(phases: dict) -> str | None:
    """找到第一个 status != done 的 phase（按 phase order）。"""
    keys = sorted(phases.keys(), key=_phase_sort_key)
    for k in keys:
        v = phases.get(k) or {}
        st = v.get("status")
        if st and st != "done":
            return f"{k} ({st})"
    return None


def render_state_md(state: dict) -> str:
    """从 state dict 渲染 STATE_MD markdown 文本。"""
    lines: list[str] = []
    run_ts = state.get("run_ts", "?")
    lines.append(f"# New Plan Changes — Run {run_ts}")
    lines.append("")
    lines.append(f"Started: {state.get('started_at', '?')}")
    lines.append(f"Last updated: {state.get('last_updated_at', '?')}")
    lines.append(f"Mode: {state.get('mode', '?')}")
    lines.append(f"Status: {state.get('status', '?')}")
    lines.append("")

    lines.append("## Run Metadata")
    lines.append(f"- Project Root: {state.get('project_root', '?')}")
    lines.append(f"- Proj Key: {state.get('proj_key', '?')}")
    lines.append(f"- Git HEAD at start: {state.get('git_head_at_start', '?')}")
    cc = state.get("cc_session") or {}
    sid = cc.get("session_id") or "-"
    src = cc.get("source") or "unknown"
    lines.append(f"- CC Session: {sid} (source: {src})")
    lines.append(f"- CC Transcript: {cc.get('transcript_path') or '-'}")
    lines.append("")

    lines.append("## Plan Order")
    for i, cid in enumerate(state.get("plan_order") or [], start=1):
        lines.append(f"{i}. {cid}")
    lines.append("")

    lines.append("## Progress")
    lines.append("")
    for p in state.get("progress") or []:
        seq = p.get("seq", "?")
        cid = p.get("change_id", "?")
        st = p.get("status", "?")
        round_n = _current_round(p)
        suffix = f" (round {round_n})" if round_n and st in {"in-fix-loop", "reviewing"} else ""
        lines.append(f"### #{seq} {cid} — {st}{suffix}")
        if p.get("implement_commit"):
            lines.append(f"- Implement Commit: {p['implement_commit']}")
        if p.get("archive_commit"):
            lines.append(f"- Archive Commit: {p['archive_commit']}")
        if p.get("total_rounds") is not None:
            lines.append(f"- Total Rounds: {p['total_rounds']}")
        phases = p.get("phases") or {}
        cur = _current_phase_label(phases)
        if cur:
            lines.append(f"- Current Phase: {cur}")
        bt = p.get("blocking_trend") or []
        if bt:
            lines.append("- Blocking Trend: " + " → ".join(str(x) for x in bt))
        rsd = p.get("rounds_since_strict_decrease")
        if rsd is not None and rsd > 0:
            lines.append(f"- Rounds Since Strict Decrease: {rsd}")
        cs = p.get("categories_seen") or []
        if cs:
            lines.append("- Categories Seen: " + ", ".join(cs))
        td = _total_duration_ms(phases)
        if td is not None:
            in_progress = any((v or {}).get("status") == "in-progress" for v in phases.values())
            suf = " (so far)" if in_progress else ""
            lines.append(f"- Total Duration: {_fmt_duration_ms(td)}{suf}")
        ps = _phases_summary(phases)
        if ps != "—":
            lines.append(f"- Phases: {ps}")
        if p.get("reason"):
            lines.append(f"- Reason: {p['reason']}")
        if p.get("base"):
            lines.append(f"- Base: {p['base']}")
        lines.append("")

    repair_log = state.get("repair_log") or []
    if repair_log:
        lines.append("## Repair Log")
        lines.append("")
        for ent in repair_log:
            ts = ent.get("ts", "?")
            seq = ent.get("seq", "?")
            cid = ent.get("change_id", "?")
            prev = ent.get("previous_status") or "?"
            audit = ent.get("audit_base") or "-"
            moved = ent.get("openspec_moved_back")
            mv_label = "yes" if moved else "no"
            lines.append(
                f"- {ts} #{seq} {cid}: reset from `{prev}` → `pending`; audit={audit}; openspec_moved_back={mv_label}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _current_round(progress_entry: dict) -> int:
    """从 phases 中提取最大的 fix-rN / review-rN 编号。"""
    phases = progress_entry.get("phases") or {}
    max_n = 0
    for k in phases.keys():
        m = re.match(r"^(?:fix|review)-r(\d+)$", k)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return max_n


# ----------------------------- jq 路径查询 -----------------------------


def _jq_query(state: dict, jq_path: str) -> Any:
    """用外部 jq 计算路径表达式。

    我们不试图自己实现 jq；用户机器上 v1 skill 已大量依赖 jq，肯定有装。
    """
    try:
        out = subprocess.run(
            ["jq", "-c", jq_path],
            input=json.dumps(state, ensure_ascii=False),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"未找到 jq 命令：{e}") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"jq 表达式错误：{e.stderr.strip()}") from e
    return out.stdout.rstrip("\n")  # 保留原始 JSON 字符串（含引号 / 数组 / 标量）


# ----------------------------- 校验 git HEAD -----------------------------


def _git_head(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()
    except subprocess.CalledProcessError:
        return ""


# ----------------------------- CLI handlers -----------------------------


def init_run(args: argparse.Namespace) -> None:
    """state init-run：首次创建 STATE_JSON / STATE_MD / RUN_EVENTS。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        plan_order = json.loads(args.plan_order)
        if not isinstance(plan_order, list) or not all(isinstance(x, str) for x in plan_order):
            raise ValueError("plan_order 必须是字符串数组")
    except (json.JSONDecodeError, ValueError) as e:
        _io.emit_error("invalid_plan_order", f"--plan-order 解析失败：{e}", exit_code=2)
        return

    if p.state_json.exists():
        _io.emit_error(
            "state_already_exists",
            f"STATE_JSON 已存在：{p.state_json}（如需新建请用 --fresh 重新 init）",
            exit_code=1,
        )
        return

    p.task_log_dir.mkdir(parents=True, exist_ok=True)
    p.run_dir.mkdir(parents=True, exist_ok=True)

    # session 信息从环境读取（init 命令已写入）
    cc_session = {
        "session_id": os.environ.get("NPC_SESSION_ID") or None,
        "transcript_path": os.environ.get("NPC_TRANSCRIPT_PATH") or None,
        "source": os.environ.get("NPC_SESSION_SOURCE") or "unknown",
    }

    mode = os.environ.get("NPC_MODE", "interactive")
    fresh = os.environ.get("NPC_FRESH", "false") == "true"

    progress = [
        {
            "seq": i + 1,
            "change_id": cid,
            "status": "pending",
            "blocking_trend": [],
            "categories_seen": [],
            "rounds_since_strict_decrease": 0,
            "phases": {},
        }
        for i, cid in enumerate(plan_order)
    ]

    state = {
        "schema_version": SCHEMA_VERSION,
        "run_ts": p.run_ts,
        "started_at": _io.now_iso(),
        "last_updated_at": _io.now_iso(),
        "mode": mode,
        "fresh": fresh,
        "status": "in-progress",
        "project_root": str(p.repo_root),
        "proj_key": p.proj_key,
        "git_head_at_start": _git_head(p.repo_root),
        "cc_session": cc_session,
        "plan_order": plan_order,
        "progress": progress,
    }

    write_state(p.state_json, p.state_md, state)
    p.run_events.touch(exist_ok=True)

    _io.emit({"ok": True, "state_json": str(p.state_json), "total_changes": len(plan_order)})


def get(args: argparse.Namespace) -> None:
    """state get <jq-path>。"""
    try:
        p = _paths.load_paths(args)
        state = read_state(p.state_json)
    except (_paths.PathsError, FileNotFoundError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        result = _jq_query(state, args.jq_path)
    except RuntimeError as e:
        _io.emit_error("jq_failed", str(e), exit_code=1)
        return

    # 直接打印 jq 输出（保留原始 JSON 形态，不再包一层 envelope）
    import sys as _sys

    _sys.stdout.write(result + "\n")


def add_change(args: argparse.Namespace) -> None:
    """state add-change <seq> <change_id> [--base PATH]。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    base = Path(args.base) if args.base else _paths.base_for(p, args.seq, args.change_id)
    base.mkdir(parents=True, exist_ok=True)

    def mutate(state: dict) -> None:
        progress = state.setdefault("progress", [])
        if 1 <= args.seq <= len(progress):
            entry = progress[args.seq - 1]
            if entry.get("change_id") != args.change_id:
                raise ValueError(
                    f"seq={args.seq} 已存在但 change_id 不匹配：{entry.get('change_id')} vs {args.change_id}"
                )
            entry["base"] = str(base)
        elif args.seq == len(progress) + 1:
            progress.append(
                {
                    "seq": args.seq,
                    "change_id": args.change_id,
                    "status": "pending",
                    "blocking_trend": [],
                    "categories_seen": [],
                    "rounds_since_strict_decrease": 0,
                    "phases": {},
                    "base": str(base),
                }
            )
        else:
            raise ValueError(
                f"seq={args.seq} 不连续（当前 progress 长度 {len(progress)}）"
            )

    try:
        update_state(p.state_json, p.state_md, mutate)
    except ValueError as e:
        _io.emit_error("seq_invalid", str(e), exit_code=1)
        return
    except FileNotFoundError:
        _io.emit_error(
            "state_not_found", f"STATE_JSON 不存在：{p.state_json}", exit_code=3
        )
        return

    _io.emit({"ok": True, "seq": args.seq, "change_id": args.change_id, "base": str(base)})


def set_progress(args: argparse.Namespace) -> None:
    """state set-progress <seq> [--status ... --reason ... ...]。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    updates: dict[str, Any] = {}
    if args.status is not None:
        if args.status not in VALID_PROGRESS_STATUS:
            _io.emit_error(
                "invalid_status",
                f"status={args.status} 不在合法集 {sorted(VALID_PROGRESS_STATUS)}",
                exit_code=2,
            )
            return
        updates["status"] = args.status
    if args.reason is not None:
        updates["reason"] = args.reason
    if args.implement_commit is not None:
        updates["implement_commit"] = args.implement_commit
    if args.archive_commit is not None:
        updates["archive_commit"] = args.archive_commit
    if args.total_rounds is not None:
        updates["total_rounds"] = args.total_rounds
    if args.stale_verdict is not None:
        updates["stale_verdict"] = None if args.stale_verdict == "null" else args.stale_verdict

    if not updates:
        _io.emit_error("no_updates", "至少需要传入一个待更新字段", exit_code=2)
        return

    def mutate(state: dict) -> None:
        progress = state.get("progress") or []
        if not (1 <= args.seq <= len(progress)):
            raise ValueError(
                f"seq={args.seq} 超出 progress 数组长度（total={len(progress)}）"
            )
        progress[args.seq - 1].update(updates)
        # 状态进入"开始"时记录 started_at（若尚未设置）
        if updates.get("status") and not progress[args.seq - 1].get("started_at"):
            progress[args.seq - 1]["started_at"] = _io.now_iso()

    try:
        update_state(p.state_json, p.state_md, mutate)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return
    except FileNotFoundError:
        _io.emit_error(
            "state_not_found", f"STATE_JSON 不存在：{p.state_json}", exit_code=3
        )
        return

    _io.emit({"ok": True, "seq": args.seq, **updates})


def finalize(args: argparse.Namespace) -> None:
    """state finalize：判定顶层 status。"""
    try:
        p = _paths.load_paths(args)
        state = read_state(p.state_json)
    except (_paths.PathsError, FileNotFoundError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    progress = state.get("progress") or []
    counts = {
        "archived": sum(1 for x in progress if x.get("status") == "archived"),
        "failed": sum(1 for x in progress if x.get("status") == "failed"),
        "skipped": sum(1 for x in progress if x.get("status") == "skipped-auto"),
        "needs_decision": sum(
            1 for x in progress if x.get("status") == "needs-user-decision"
        ),
        "total": len(progress),
    }

    if counts["needs_decision"] > 0:
        _io.emit_error(
            "has_needs_decision",
            f"存在 {counts['needs_decision']} 个 needs-user-decision change，不能 finalize",
            exit_code=1,
        )
        return

    if counts["archived"] == counts["total"]:
        final = "completed"
    elif counts["archived"] + counts["failed"] + counts["skipped"] == counts["total"]:
        final = "completed-with-issues"
    else:
        _io.emit_error(
            "incomplete",
            f"仍有 {counts['total'] - counts['archived'] - counts['failed'] - counts['skipped']} 个 change 处于非终态",
            exit_code=1,
        )
        return

    state["status"] = final
    write_state(p.state_json, p.state_md, state)

    _io.emit(
        {
            "ok": True,
            "final_status": final,
            "archived": counts["archived"],
            "failed": counts["failed"],
            "skipped": counts["skipped"],
            "total": counts["total"],
        }
    )
