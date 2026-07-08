"""``npc spec-report render --seq N`` —— 单 change「工作 agent 表现」收尾回执。

对一个**成功 archived** 的 change，从 ``STATE_JSON`` + ``_telemetry`` + git 派生
一份纯确定性的收尾报告，产出三份同源产物：

- ``spec-report.json``：审计契约源，字段齐全。
- ``spec-report.md``：由同一派生对象渲染的人读简报（固定标题段、行数上限）。
- 一条 ``kind=spec.report`` 的 telemetry 事件（``common_metrics`` 子集 + pointer）。

不新增采集、不 spawn agent；自报核验（C）用通用启发式（文件路径含 test/spec）
判定 diff 是否触及测试文件，缺数据一律标 ``unverifiable``，不误报 ``warn``。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from . import _io, cost as _cost, paths as _paths, state as _state, telemetry as _telemetry


MD_LINE_LIMIT = 80

# 测试文件识别：通用启发式——路径分段含 test(s)/spec(s)，前后以 / _ . - 或首尾分隔。
_TEST_PATH_RE = re.compile(r"(?:^|[/_.\-])(test|tests|spec|specs)(?:[/_.\-]|$)", re.IGNORECASE)

_FIX_RE = re.compile(r"^fix-r(\d+)$")
_REVIEW_RE = re.compile(r"^review-r(\d+)$")


# ============================================================
# 纯函数：从 progress entry 派生各维度
# ============================================================


def _fmt_duration_ms(ms: int | None) -> str:
    return _state._fmt_duration_ms(ms)


def _fix_round_keys(phases: dict) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for k in phases.keys():
        m = _FIX_RE.match(k)
        if m:
            out.append((int(m.group(1)), k))
    out.sort()
    return out


def _review_round_keys(phases: dict) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for k in phases.keys():
        m = _REVIEW_RE.match(k)
        if m:
            out.append((int(m.group(1)), k))
    out.sort()
    return out


def _commit_chain(entry: dict) -> list[dict]:
    """按时序收集 commit chain：implement → fix-r1..N → archive。"""
    chain: list[dict] = []
    impl = entry.get("implement_commit")
    if impl:
        chain.append({"phase": "implement", "commit": impl})
    phases = entry.get("phases") or {}
    for n, k in _fix_round_keys(phases):
        c = (phases.get(k) or {}).get("commit")
        if c:
            chain.append({"phase": k, "commit": c})
    arc = entry.get("archive_commit")
    if arc:
        chain.append({"phase": "archive", "commit": arc})
    return chain


def _phase_durations(entry: dict) -> dict[str, int | None]:
    phases = entry.get("phases") or {}
    return {k: (v or {}).get("duration_ms") for k, v in phases.items()}


def _total_duration_ms(entry: dict) -> int | None:
    phases = entry.get("phases") or {}
    total = 0
    has_any = False
    for v in phases.values():
        d = (v or {}).get("duration_ms")
        if d is not None:
            total += int(d)
            has_any = True
    return total if has_any else None


def _one_shot(entry: dict) -> bool | None:
    """MUST：有 fix 轮 → false；首轮 review blocking=0 → true；缺 review 数据 → null。"""
    phases = entry.get("phases") or {}
    if _fix_round_keys(phases):
        return False
    review0 = phases.get("review-r0")
    if not review0 or review0.get("blocking") is None:
        return None
    return int(review0.get("blocking")) == 0


def _category_distribution(entry: dict) -> dict[str, int]:
    """每 category 在多少个 review 轮次中被判定 → 近似「每类 fix 轮数」。

    数据来源：各 review-rN phase 记录的 ``categories`` 字段（该轮 blocking/advisory
    findings 涉及的类目），而非重用整个 change 的 fix 轮总数（避免所有类目共享同一
    粗粒度数字，丢失「哪类返工多」信号）。
    """
    phases = entry.get("phases") or {}
    counts: dict[str, int] = {}
    for _n, k in _review_round_keys(phases):
        cats = (phases.get(k) or {}).get("categories") or []
        for c in cats:
            if c:
                counts[c] = counts.get(c, 0) + 1
    return counts


def _get_entry(state: dict, seq: int) -> dict:
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
    return progress[seq - 1]


# ============================================================
# 自报核验（C，确定性）
# ============================================================


def _diff_touches_tests(repo_root: Path, prev_commit: str, commit: str) -> bool | None:
    """git diff --name-only prev..commit 是否触及测试文件；git 失败返回 None（unverifiable）。"""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", prev_commit, commit],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    files = [f for f in result.stdout.splitlines() if f.strip()]
    return any(_TEST_PATH_RE.search(f) for f in files)


def _verify_regressions(repo_root: Path, entry: dict) -> list[dict]:
    """逐 fix 轮核验 ``regressions_added`` 自报。

    - 未声明（缺失字段/`-`）→ ok（无可矛盾之处）
    - 声明了但缺 commit range → unverifiable
    - 声明了且 diff 触及测试文件 → ok；未触及 → warn
    """
    phases = entry.get("phases") or {}
    fix_keys = _fix_round_keys(phases)
    out: list[dict] = []
    prev_commit = entry.get("implement_commit")
    for n, k in fix_keys:
        ph = phases.get(k) or {}
        commit = ph.get("commit")
        raw = ph.get("regressions_added")
        item: dict[str, Any] = {"round": n, "phase": k}
        if raw is None:
            item.update({"verdict": "unverifiable", "reason": "missing-self-report"})
        else:
            claim = str(raw).strip()
            claimed_list = [c.strip() for c in claim.split(",") if c.strip() and c.strip() != "-"]
            if not claimed_list:
                item.update({"verdict": "ok", "claimed": []})
            elif not commit or not prev_commit:
                item.update(
                    {"verdict": "unverifiable", "claimed": claimed_list, "reason": "missing-commit-range"}
                )
            else:
                touched = _diff_touches_tests(repo_root, prev_commit, commit)
                if touched is None:
                    item.update(
                        {"verdict": "unverifiable", "claimed": claimed_list, "reason": "git-diff-failed"}
                    )
                elif touched:
                    item.update({"verdict": "ok", "claimed": claimed_list})
                else:
                    item.update(
                        {"verdict": "warn", "claimed": claimed_list, "reason": "diff-has-no-test-files"}
                    )
        out.append(item)
        if commit:
            prev_commit = commit
    return out


def _collect_categories_scanned(entry: dict) -> tuple[set[str], bool]:
    """聚合整个 change 各 fix 轮自报的 categories_scanned；返回 (集合, 是否有任一轮提供数据)。"""
    phases = entry.get("phases") or {}
    scanned: set[str] = set()
    any_present = False
    for _n, k in _fix_round_keys(phases):
        raw = (phases.get(k) or {}).get("categories_scanned")
        if raw is None:
            continue
        any_present = True
        raw = str(raw).strip()
        if raw and raw != "-":
            for c in raw.split(","):
                c = c.strip()
                if c:
                    scanned.add(c)
    return scanned, any_present


def _verify_categories_scanned(entry: dict) -> dict:
    """``categories_scanned`` 与 ``categories_seen``（观测源）对照。"""
    seen = set(entry.get("categories_seen") or [])
    scanned, any_present = _collect_categories_scanned(entry)
    if not seen:
        return {
            "verdict": "ok",
            "categories_seen": [],
            "categories_scanned": sorted(scanned),
            "missing": [],
        }
    if not any_present:
        return {
            "verdict": "unverifiable",
            "categories_seen": sorted(seen),
            "categories_scanned": [],
            "missing": sorted(seen),
            "reason": "missing-self-report",
        }
    missing = sorted(seen - scanned)
    return {
        "verdict": "ok" if not missing else "warn",
        "categories_seen": sorted(seen),
        "categories_scanned": sorted(scanned),
        "missing": missing,
    }


def _aggregate_self_report_verdict(regressions: list[dict], categories: dict) -> str:
    """汇总结论：warn > unverifiable > ok（任一 warn 即整体 warn，缺数据不误报 warn）。"""
    verdicts = [r["verdict"] for r in regressions] + [categories["verdict"]]
    if "warn" in verdicts:
        return "warn"
    if "unverifiable" in verdicts:
        return "unverifiable"
    return "ok"


# ============================================================
# 叙事
# ============================================================


def _extract_narrative(base: Path) -> dict:
    """从 implement.summary.md / archive.summary.md 抽 headline + notes（best-effort）。"""
    headline: str | None = None
    notes_parts: list[str] = []
    for name in ("implement.summary.md", "archive.summary.md"):
        fp = base / name
        if not fp.is_file():
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except OSError:
            continue
        if headline is None:
            for line in text.splitlines():
                s = line.strip()
                if s:
                    headline = s.lstrip("#").strip()
                    break
        m = re.search(
            r"^##\s*Issues Encountered\s*\n(.+?)(?:\n##|\Z)",
            text,
            re.MULTILINE | re.DOTALL,
        )
        if m:
            snippet = m.group(1).strip()
            if snippet and snippet.lower().rstrip(".") not in ("none", "无"):
                notes_parts.append(snippet.splitlines()[0].strip())
    return {"headline": headline, "notes": "; ".join(notes_parts) if notes_parts else None}


# ============================================================
# 派生主入口
# ============================================================


def derive_report(
    state: dict,
    seq: int,
    repo_root: Path,
    *,
    base: Path | None = None,
    telemetry_events: list[dict] | None = None,
) -> dict:
    """从 state + git + telemetry 派生单 change 的完整报告 dict（json 契约源）。"""
    entry = _get_entry(state, seq)
    change_id = entry.get("change_id")
    status = entry.get("status")
    phases = entry.get("phases") or {}

    review_keys = _review_round_keys(phases)
    fix_keys = _fix_round_keys(phases)

    if base is None:
        base_str = entry.get("base")
        base = Path(base_str) if base_str else None

    run_ts = state.get("run_ts")
    events = telemetry_events if telemetry_events is not None else list(_telemetry.iter_events())
    change_events = [
        ev
        for ev in events
        if isinstance(ev, dict)
        and ev.get("change_seq") == seq
        and (run_ts is None or ev.get("run_ts") == run_ts)
    ]
    cost_agg = _cost.aggregate_cost(change_events)

    regressions_verification = _verify_regressions(repo_root, entry)
    categories_verification = _verify_categories_scanned(entry)
    self_report_summary_verdict = _aggregate_self_report_verdict(
        regressions_verification, categories_verification
    )

    narrative = _extract_narrative(base) if base is not None else {"headline": None, "notes": None}

    return {
        "schema_version": 1,
        "change_id": change_id,
        "change_seq": seq,
        "run_ts": run_ts,
        "proj_key": state.get("proj_key"),
        "status": status,
        "delivery": {
            "commit_chain": _commit_chain(entry),
            "final_status": status,
        },
        "convergence": {
            "review_rounds": len(review_keys),
            "fix_rounds": len(fix_keys),
            "blocking_trend": list(entry.get("blocking_trend") or []),
            "one_shot": _one_shot(entry),
        },
        "rework": {
            "categories_seen": list(entry.get("categories_seen") or []),
            "category_distribution": _category_distribution(entry),
        },
        "duration": {
            "phases_ms": _phase_durations(entry),
            "total_duration_ms": _total_duration_ms(entry),
        },
        "resources": {
            "estimated_tokens_by_backend": cost_agg["by_bucket"],
            "estimated_tokens_total": cost_agg["total"],
            "method": cost_agg["method"],
        },
        "self_report_verification": {
            "regressions_added": regressions_verification,
            "categories_scanned": categories_verification,
            "summary_verdict": self_report_summary_verdict,
        },
        "narrative": narrative,
    }


def common_metrics(report: dict) -> dict:
    """md/json/telemetry 三视图共享的取值子集（一致性约束的唯一来源）。"""
    conv = report["convergence"]
    dur = report["duration"]
    res = report["resources"]
    srv = report["self_report_verification"]
    return {
        "final_status": report.get("status"),
        "review_rounds": conv["review_rounds"],
        "fix_rounds": conv["fix_rounds"],
        "blocking_trend": conv["blocking_trend"],
        "total_duration_ms": dur["total_duration_ms"],
        "estimated_tokens_total": res["estimated_tokens_total"],
        "self_report_summary_verdict": srv["summary_verdict"],
    }


# ============================================================
# md 渲染
# ============================================================


def render_md(report: dict) -> str:
    """从派生对象渲染人读简报：固定指标标题段，行数不超 ``MD_LINE_LIMIT``。"""
    lines: list[str] = []
    seq = report.get("change_seq")
    change_id = report.get("change_id")
    lines.append(f"# Spec Report — #{seq} {change_id}")
    lines.append("")

    lines.append("## 终态")
    lines.append(f"- status: {report.get('status')}")
    chain = report["delivery"]["commit_chain"]
    if chain:
        chain_str = " → ".join(f"{c['phase']}={c['commit']}" for c in chain)
        lines.append(f"- commit chain: {chain_str}")
    else:
        lines.append("- commit chain: (无)")
    lines.append("")

    conv = report["convergence"]
    lines.append("## 收敛")
    lines.append(f"- review rounds: {conv['review_rounds']}")
    lines.append(f"- fix rounds: {conv['fix_rounds']}")
    bt = conv["blocking_trend"]
    lines.append(f"- blocking trend: {' → '.join(str(x) for x in bt) if bt else '(无)'}")
    one_shot = conv["one_shot"]
    one_shot_str = "null" if one_shot is None else str(bool(one_shot)).lower()
    lines.append(f"- one_shot: {one_shot_str}")
    lines.append("")

    rework = report["rework"]
    lines.append("## 返工")
    dist = rework["category_distribution"]
    if dist:
        for cat, n in sorted(dist.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"- {cat}: {n} 轮")
    else:
        lines.append("- (无返工)")
    lines.append("")

    dur = report["duration"]
    lines.append("## 耗时")
    lines.append(f"- total: {_fmt_duration_ms(dur['total_duration_ms'])}")
    for ph, ms in sorted((dur["phases_ms"] or {}).items()):
        lines.append(f"- {ph}: {_fmt_duration_ms(ms)}")
    lines.append("")

    res = report["resources"]
    lines.append("## 资源")
    lines.append(
        f"- estimated_tokens_total（估算，method={res['method']}）: "
        f"{res['estimated_tokens_total'].get('est_input_tokens', 0)} in / "
        f"{res['estimated_tokens_total'].get('est_output_tokens', 0)} out"
    )
    for backend, b in sorted((res["estimated_tokens_by_backend"] or {}).items()):
        lines.append(
            f"- {backend}: input≈{b.get('est_input_tokens', 0)} output≈{b.get('est_output_tokens', 0)}"
        )
    lines.append("")

    srv = report["self_report_verification"]
    lines.append("## 自报核验")
    lines.append(f"- 汇总: {srv['summary_verdict']}")
    for r in srv["regressions_added"]:
        lines.append(f"- fix-r{r['round']} regressions_added: {r['verdict']}")
    cs = srv["categories_scanned"]
    missing = cs.get("missing") or []
    lines.append(f"- categories_scanned: {cs['verdict']} (missing={','.join(missing) if missing else '-'})")
    lines.append("")

    narrative = report.get("narrative") or {}
    lines.append("## 叙事")
    lines.append(f"- {narrative.get('headline') or '(无)'}")
    if narrative.get("notes"):
        lines.append(f"- notes: {narrative['notes']}")

    text_lines = [l.rstrip() for l in lines]
    if len(text_lines) > MD_LINE_LIMIT:
        text_lines = text_lines[: MD_LINE_LIMIT - 1] + ["- …（截断，完整数据见 spec-report.json）"]
    return "\n".join(text_lines).rstrip() + "\n"


# ============================================================
# CLI handler
# ============================================================


def render(args: argparse.Namespace) -> None:
    """``npc spec-report render --seq N``。"""
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = _state.read_state(p.state_json)
    except FileNotFoundError as e:
        _io.emit_error("state_not_found", str(e), exit_code=3)
        return
    except json.JSONDecodeError as e:
        _io.emit_error("state_invalid", f"STATE_JSON 解析失败：{e}", exit_code=3)
        return

    seq = args.seq
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        _io.emit_error(
            "seq_out_of_range",
            f"seq={seq} 超出 progress 数组长度（total={len(progress)}）",
            exit_code=1,
        )
        return

    entry = progress[seq - 1]
    if entry.get("status") != "archived":
        _io.emit_error(
            "not_archived",
            f"seq={seq} change_id={entry.get('change_id')} 非 archived 终态"
            f"（status={entry.get('status')}），不生成 delivery receipt",
            exit_code=1,
        )
        return

    change_id = entry.get("change_id")
    base = Path(entry.get("base") or _paths.base_for(p, seq, change_id))

    try:
        report = derive_report(state, seq, p.repo_root, base=base)
    except ValueError as e:
        _io.emit_error("derive_failed", f"派生报告失败：{e}", exit_code=1)
        return

    json_path = base / "spec-report.json"
    md_path = base / "spec-report.md"
    written = {"json": False, "md": False, "telemetry": False}
    errors: list[str] = []

    try:
        base.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written["json"] = True
    except OSError as e:
        errors.append(f"json_write_failed: {e}")

    try:
        md_text = render_md(report)
        md_path.write_text(md_text, encoding="utf-8")
        written["md"] = True
    except OSError as e:
        errors.append(f"md_write_failed: {e}")

    common = common_metrics(report)
    try:
        ok_emit = _telemetry.emit_event(
            {
                "kind": "spec.report",
                "proj_key": p.proj_key,
                "canonical_proj_key": p.canonical_proj_key,
                "run_ts": p.run_ts,
                "change_seq": seq,
                "change_id": change_id,
                "status": entry.get("status"),
                **common,
                "pointer": {
                    "report_json": str(json_path) if written["json"] else None,
                    "report_md": str(md_path) if written["md"] else None,
                },
            }
        )
        written["telemetry"] = bool(ok_emit)
    except Exception as e:  # best-effort：telemetry 写入绝不阻塞主流程
        errors.append(f"telemetry_failed: {e}")

    ok = written["json"] and written["md"]
    payload: dict[str, Any] = {
        "ok": ok,
        "seq": seq,
        "change_id": change_id,
        "spec_report_json": str(json_path) if written["json"] else None,
        "spec_report_md": str(md_path) if written["md"] else None,
        "telemetry_emitted": written["telemetry"],
    }
    if errors:
        payload["errors"] = errors
    _io.emit(payload)
