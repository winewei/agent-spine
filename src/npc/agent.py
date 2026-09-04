"""Sub-agent prompt 渲染与 spawn 引导语生成（v1.0.0 起）。

两个 handler：

- ``prompt_render``：把 §A Implementer / §B Fixer 模板渲染到 disk
- ``spawn_prompt``：生成给 Claude ``Agent`` 工具 ``prompt`` 字段的薄引导语

两者均从 STATE_JSON 自包含 resolve seq / base / implement_commit /
categories_seen / blocking_trend，调用方仅需传 ``--phase`` 与 ``--change-id``
（fix 阶段加 ``--round``）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from . import (
    _io,
    config as _config,
    experience as _experience,
    paths as _paths,
    telemetry as _telemetry,
    templates,
)
from .fixer import render_findings
from .review import parse_review
from .state import read_state, update_state


# ============================================================
# Agent 调用 timeout 预算（渐进退避）
# ============================================================

# 默认值参见 skill.md v3 设计文档：base 1800s / mult 1.2 / max 3600s / 最多 5 次 record-timeout
TIMEOUT_BASE_SEC_DEFAULT = 1800
TIMEOUT_MULTIPLIER_DEFAULT = 1.2
TIMEOUT_MAX_SEC_DEFAULT = 3600
TIMEOUT_EXHAUSTED_AT_RETRIES = 5  # retries 达到该值即视为耗尽（含两次撞 3600 上限）


def _compute_budget(retries: int, base: int, mult: float, max_sec: int) -> int:
    raw = base * (mult ** max(0, int(retries)))
    return int(min(raw, max_sec))


def _exhausted(retries: int) -> bool:
    return retries >= TIMEOUT_EXHAUSTED_AT_RETRIES


# ----------------------------- 内部辅助 -----------------------------


def _resolve_seq(state: dict, change_id: str, explicit_seq: int | None) -> int:
    """按 change_id 在 state.progress 里找 seq；若 explicit_seq 给定则校验一致。"""
    progress = state.get("progress") or []
    matches = [p["seq"] for p in progress if p.get("change_id") == change_id]
    if not matches:
        raise ValueError(
            f"change_id={change_id!r} 不在 STATE_JSON.progress 中；"
            f"请先 `npc state add-change` 或检查 plan_order"
        )
    if len(matches) > 1:
        raise ValueError(f"change_id={change_id!r} 在 progress 中出现多次：seq={matches}")
    found = matches[0]
    if explicit_seq is not None and explicit_seq != found:
        raise ValueError(f"--seq={explicit_seq} 与 state 中 change_id={change_id} 的 seq={found} 不一致")
    return found


def _resolve_progress_entry(state: dict, seq: int) -> dict:
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise ValueError(f"seq={seq} 越界（total={len(progress)}）")
    return progress[seq - 1]


def _default_prompt_path(base: Path, phase: str, round_n: int | None) -> Path:
    if phase == "implement":
        return base / "implement.prompt.md"
    if phase == "fix":
        if round_n is None:
            raise ValueError("fix 阶段必须传 --round")
        return base / f"round-{round_n}.fix.prompt.md"
    raise ValueError(f"未知 phase：{phase!r}")


def _default_review_path(base: Path, round_n: int) -> Path:
    """fix round N 渲染时默认引用 round-(N-1).review.json 中的 blocking findings。"""
    return base / f"round-{round_n - 1}.review.json"


# ----------------------------- 经验层召回（旁路增强） -----------------------------

# 语言栈粗判：只用来给检索 query 加一个区分度较高的词，判错的代价是召回稍差。
_STACK_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", ("pyproject.toml", "setup.py", "requirements.txt")),
    ("node", ("package.json",)),
    ("go", ("go.mod",)),
    ("rust", ("Cargo.toml",)),
)


def _detect_stack(repo_root: Path) -> str:
    for name, markers in _STACK_MARKERS:
        if any((repo_root / m).is_file() for m in markers):
            return name
    return "unknown"


def _proposal_title(repo_root: Path, change_id: str) -> str:
    """proposal.md 的检索摘要：首个 ``# `` 标题 + ``## Why`` / ``## What Changes`` 段各自首句。

    只拼 change_id 会让 query 退化为标识符本身（对语义检索几乎无信息量）；
    Why / What 的首句才承载"这是什么问题、改了什么"。
    """
    path = repo_root / "openspec" / "changes" / change_id / "proposal.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    title = ""
    sections: dict[str, str] = {}
    current = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("# ") and not title:
            title = s[2:].strip()
            continue
        if s.startswith("## "):
            current = s[3:].strip().lower()
            continue
        if current and current not in sections:
            sections[current] = s.lstrip("-* ").strip()
    why = next((v for k, v in sections.items() if k.startswith("why")), "")
    what = next((v for k, v in sections.items() if k.startswith("what")), "")
    parts = [p for p in (title, why[:80], what[:80]) if p]
    return " ".join(parts)


def _short_head(repo_root: Path) -> str:
    """当前 HEAD 短 hash；取不到返回 ``-``（注入回执宁可缺 hash 也不缺记录）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "-"
    return out.stdout.strip() or "-"


def _snapshot_fingerprint(result) -> str:
    payload = "\n".join(sorted(f"{e['uri']}:{e['score']:.4f}" for e in result.entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sync_policy_snapshot(p, result) -> str | None:
    """本 run 首次成功召回时把经验库快照指纹钉进 state；返回生效的指纹。

    钉住之后不再更新：同一 run 内比较 review 复发率必须锚定同一个经验库版本，
    中途换锚点等于把两批不可比的数据混在一起。
    """
    try:
        state = read_state(p.state_json)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    existing = state.get("policy_snapshot_id")
    if existing:
        return str(existing)
    if not result.entries:
        return None

    snapshot = _snapshot_fingerprint(result)

    def mutate(s: dict) -> None:
        if not s.get("policy_snapshot_id"):
            s["policy_snapshot_id"] = snapshot

    try:
        update_state(p.state_json, p.state_md, mutate)
    except (FileNotFoundError, OSError, ValueError):
        pass
    return snapshot


def _injected_uris(base: Path) -> list[str]:
    """本 change 已注入过的 uri——同一条经验在同一个 change 内不重复注入。"""
    out: list[str] = []
    seen: set[str] = set()
    for rec in _experience.read_injection_records(base):
        for item in rec.get("uris") or []:
            uri = item.get("uri") if isinstance(item, dict) else item
            if uri and str(uri) not in seen:
                seen.add(str(uri))
                out.append(str(uri))
    return out


def _build_recall_query(p, change_id: str, phase: str, blocking_findings) -> str:
    if phase == "fix":
        titles = [
            f"{f.get('category') or 'unknown'}: {(f.get('title') or '').strip()}"
            for f in blocking_findings
        ]
        return _experience.build_query(
            "fix", change_id=change_id, findings_titles=titles
        )
    return _experience.build_query(
        "implement",
        change_id=change_id,
        proposal_title=_proposal_title(p.repo_root, change_id),
        stack=_detect_stack(p.repo_root),
    )


def _recall_experience(
    p,
    base: Path,
    seq: int,
    *,
    phase: str,
    round_n: int | None,
    change_id: str,
    blocking_findings=(),
) -> tuple[str, dict]:
    """召回历史经验并渲染注入块。返回 ``(block, meta)``，meta 并入 stdout 回执。

    读取侧是纯增强：未启用、无凭据、网络失败、模块内部异常一律退化为空块 +
    一个 error 码，prompt 照常渲染、退出码不变。故此处刻意兜底 ``Exception``。

    副作用（均在 ``<base>`` 下）：写 ``<phase>[-rN].experience.md`` 注入块正文与
    同名 ``.experience.json`` 注入回执，使 prompt 可完整重放并支持回抄检测。
    """
    try:
        cfg = _config.load_config(p.repo_root).experience
    except _config.ConfigError:
        return "", {"experience_injected": 0}
    if not cfg.enabled:
        return "", {"experience_injected": 0}

    try:
        client = _experience.from_config(cfg, p.repo_root)
        if client is None:
            return "", {"experience_injected": 0, "experience_error": "no-credentials"}

        query = _build_recall_query(p, change_id, phase, blocking_findings)

        started = time.monotonic()
        result = _experience.recall(
            client, cfg, phase=phase, query=query, exclude_uris=_injected_uris(base)
        )
        duration_ms = int((time.monotonic() - started) * 1000)

        block = _experience.render_block(
            result, max_tokens=cfg.inject_max_tokens(phase)
        )
        stem = _experience.injection_record_stem(phase, round_n)
        (base / f"{stem}.experience.md").write_text(block, encoding="utf-8")
        record = _experience.write_injection_record(
            base, phase, round_n, result, _short_head(p.repo_root), block=block
        )

        injected = block.count(_experience.INJECTION_TAG)
        tokens = _experience.estimate_tokens(block)
        snapshot = _sync_policy_snapshot(p, result)

        _telemetry.emit_experience_recall(
            proj_key=p.proj_key,
            run_ts=p.run_ts,
            change_seq=seq,
            change_id=change_id,
            phase=phase,
            round_n=round_n,
            ok=result.error is None,
            entries=injected,
            injected_tokens=tokens,
            uris=result.uris,
            error=result.error,
            duration_ms=duration_ms,
            state_json=p.state_json,
            run_events=p.run_events,
            record_path=record,
            policy_snapshot_id=snapshot,
        )

        meta = {
            "experience_injected": injected,
            "experience_tokens": tokens,
            "experience_record": str(record),
        }
        if result.error:
            meta["experience_error"] = result.error
        return block, meta
    except Exception:  # noqa: BLE001 - 读取侧永不影响 prompt 渲染
        return "", {"experience_injected": 0, "experience_error": "internal"}


# ----------------------------- CLI handlers -----------------------------


def prompt_render(args: argparse.Namespace) -> None:
    """``npc agent prompt render --phase {implement|fix} --change-id CID [...]``。

    Implement 路径：
        渲染 §A Implementer 模板到 ``$BASE/implement.prompt.md``。

    Fix 路径：
        - 读 ``--review-json``（默认 ``$BASE/round-{N-1}.review.json``）抽 blocking findings
        - 从 state 取 implement_commit / categories_seen / blocking_trend
        - 渲染 §B Fixer 模板到 ``$BASE/round-N.fix.prompt.md``
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = read_state(p.state_json)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _io.emit_error("env_missing", f"读取 STATE_JSON 失败：{e}", exit_code=3)
        return

    try:
        seq = _resolve_seq(state, args.change_id, args.seq)
        entry = _resolve_progress_entry(state, seq)
    except ValueError as e:
        _io.emit_error("state_inconsistent", str(e), exit_code=1)
        return

    base = Path(entry.get("base") or _paths.base_for(p, seq, args.change_id))
    base.mkdir(parents=True, exist_ok=True)

    phase = args.phase
    round_n = args.round_n

    if phase == "fix" and round_n is None:
        _io.emit_error("missing_round", "fix 阶段必须传 --round", exit_code=2)
        return
    if phase == "implement" and round_n is not None:
        _io.emit_error(
            "round_not_allowed", "implement 阶段不接受 --round（implement 是单次 phase）", exit_code=2
        )
        return

    try:
        output = Path(args.output) if args.output else _default_prompt_path(base, phase, round_n)
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return
    output.parent.mkdir(parents=True, exist_ok=True)

    if phase == "implement":
        exp_block, exp_meta = _recall_experience(
            p, base, seq, phase="implement", round_n=None, change_id=args.change_id
        )
        text = templates.render_implementer(
            change_id=args.change_id,
            base=str(base),
            repo_root=str(p.repo_root),
            experience_block=exp_block,
        )
        meta_extra: dict = {}
    else:  # fix
        implement_commit = args.implement_commit or entry.get("implement_commit")
        if not implement_commit:
            _io.emit_error(
                "missing_implement_commit",
                "fix 渲染需要 implement_commit；请先 `npc implement record` 或传 --implement-commit",
                exit_code=2,
            )
            return

        review_path = Path(args.review_json) if args.review_json else _default_review_path(base, round_n)
        if not review_path.exists():
            _io.emit_error(
                "review_not_found",
                f"review.json 不存在：{review_path}（默认取 round-{round_n - 1}.review.json）",
                exit_code=3,
            )
            return
        try:
            review_data = json.loads(review_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            _io.emit_error("invalid_json", f"review.json 解析失败：{e}", exit_code=1)
            return
        try:
            parsed = parse_review(review_data)
        except ValueError as e:
            _io.emit_error("invalid_schema", f"review.json schema 不合法：{e}", exit_code=1)
            return

        findings_md = render_findings(parsed["blocking_findings"])

        exp_block, exp_meta = _recall_experience(
            p,
            base,
            seq,
            phase="fix",
            round_n=round_n,
            change_id=args.change_id,
            blocking_findings=parsed["blocking_findings"],
        )

        text = templates.render_fixer(
            change_id=args.change_id,
            round_n=round_n,
            implement_commit=implement_commit,
            base=str(base),
            repo_root=str(p.repo_root),
            blocking_findings_md=findings_md,
            categories_seen=entry.get("categories_seen") or [],
            blocking_trend=entry.get("blocking_trend") or [],
            experience_block=exp_block,
        )
        meta_extra = {
            "round": round_n,
            "blocking_count": len(parsed["blocking_findings"]),
            "review_json": str(review_path),
            "implement_commit": implement_commit,
        }

    output.write_text(text, encoding="utf-8")

    _io.emit(
        {
            "ok": True,
            "phase": phase,
            "seq": seq,
            "change_id": args.change_id,
            "output": str(output),
            "bytes": len(text.encode("utf-8")),
            "template_version": templates.TEMPLATE_VERSION,
            **meta_extra,
            **exp_meta,
        }
    )


def spawn_prompt(args: argparse.Namespace) -> None:
    """``npc agent spawn-prompt --phase ... --change-id CID [...]``。

    生成给主 session 调 ``Agent(prompt=...)`` 使用的引导语字符串（含 prompt 文件
    绝对路径 + 可选 extension）。stdout JSON 含 ``prompt`` / ``prompt_file`` 两字段。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = read_state(p.state_json)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        _io.emit_error("env_missing", f"读取 STATE_JSON 失败：{e}", exit_code=3)
        return

    try:
        seq = _resolve_seq(state, args.change_id, args.seq)
        entry = _resolve_progress_entry(state, seq)
    except ValueError as e:
        _io.emit_error("state_inconsistent", str(e), exit_code=1)
        return

    phase = args.phase
    round_n = args.round_n
    if phase == "fix" and round_n is None:
        _io.emit_error("missing_round", "fix 阶段必须传 --round", exit_code=2)
        return

    base = Path(entry.get("base") or _paths.base_for(p, seq, args.change_id))

    if args.prompt_file:
        prompt_file = Path(args.prompt_file)
    else:
        try:
            prompt_file = _default_prompt_path(base, phase, round_n)
        except ValueError as e:
            _io.emit_error("invalid_args", str(e), exit_code=2)
            return

    if not prompt_file.is_absolute():
        prompt_file = prompt_file.resolve()

    if not prompt_file.exists():
        _io.emit_error(
            "prompt_file_not_found",
            f"prompt 文件不存在：{prompt_file}（先跑 `npc agent prompt render`）",
            exit_code=3,
        )
        return

    extension_text: str | None = None
    if args.extension and args.extension_inline:
        _io.emit_error(
            "conflicting_args",
            "--extension 与 --extension-inline 互斥",
            exit_code=2,
        )
        return
    if args.extension:
        ext_path = Path(args.extension)
        if not ext_path.exists():
            _io.emit_error("extension_not_found", f"extension 文件不存在：{ext_path}", exit_code=3)
            return
        extension_text = ext_path.read_text(encoding="utf-8").strip()
    elif args.extension_inline:
        extension_text = args.extension_inline.strip()

    prompt_text = templates.render_spawn_prompt(
        phase=phase,
        change_id=args.change_id,
        prompt_file=str(prompt_file),
        extension=extension_text,
    )

    _telemetry.emit_agent_spawn(
        proj_key=p.proj_key,
        run_ts=p.run_ts,
        change_seq=seq,
        change_id=args.change_id,
        phase=phase,
        round_n=round_n,
        prompt_file=prompt_file,
        state_json=p.state_json,
    )

    _io.emit(
        {
            "ok": True,
            "phase": phase,
            "seq": seq,
            "change_id": args.change_id,
            "prompt": prompt_text,
            "prompt_file": str(prompt_file),
            "has_extension": extension_text is not None,
            "bytes": len(prompt_text.encode("utf-8")),
        }
    )


# ============================================================
# Timeout budget / record-timeout
# ============================================================


def _resolve_phase_entry(state: dict, seq: int, phase: str) -> tuple[dict, dict]:
    """返回 (progress_entry, phase_dict)。phase_dict 不存在时返回 ({})。"""
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
    entry = progress[seq - 1]
    phase_dict = (entry.get("phases") or {}).get(phase) or {}
    return entry, phase_dict


def timeout_budget(args: argparse.Namespace) -> None:
    """``npc agent timeout-budget --seq N --phase X [--base N --mult F --max N]``。

    纯查询；不修改 state。返回 ``{timeout_sec, retries, exhausted, max_reached}``。
    主 session 在每次 Agent(...) 调用前先取一次预算，超时则调 ``record-timeout``，
    再下次 Agent 调用时再取——直到 ``exhausted=true`` 则放弃当前 change。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    try:
        state = read_state(p.state_json)
    except FileNotFoundError as e:
        _io.emit_error("state_not_found", str(e), exit_code=3)
        return

    try:
        _, phase_dict = _resolve_phase_entry(state, args.seq, args.phase)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return

    retries = int(phase_dict.get("timeout_retries") or 0)
    base = int(args.base) if args.base is not None else TIMEOUT_BASE_SEC_DEFAULT
    mult = float(args.mult) if args.mult is not None else TIMEOUT_MULTIPLIER_DEFAULT
    max_sec = int(args.max_sec) if args.max_sec is not None else TIMEOUT_MAX_SEC_DEFAULT

    timeout_sec = _compute_budget(retries, base, mult, max_sec)
    _io.emit(
        {
            "ok": True,
            "seq": args.seq,
            "phase": args.phase,
            "timeout_sec": timeout_sec,
            "retries": retries,
            "exhausted": _exhausted(retries),
            "max_reached": timeout_sec >= max_sec,
            "base_sec": base,
            "multiplier": mult,
            "max_sec": max_sec,
            "exhausted_at_retries": TIMEOUT_EXHAUSTED_AT_RETRIES,
        }
    )


def record_timeout(args: argparse.Namespace) -> None:
    """``npc agent record-timeout --seq N --phase X``。

    递增 ``phases[X].timeout_retries`` 并写 ``timeout_last_ts``。返回新的预算。
    """
    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    base = int(args.base) if args.base is not None else TIMEOUT_BASE_SEC_DEFAULT
    mult = float(args.mult) if args.mult is not None else TIMEOUT_MULTIPLIER_DEFAULT
    max_sec = int(args.max_sec) if args.max_sec is not None else TIMEOUT_MAX_SEC_DEFAULT
    seq = args.seq
    phase = args.phase

    captured: dict = {}

    def mutate(state: dict) -> None:
        progress = state.get("progress") or []
        if not (1 <= seq <= len(progress)):
            raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
        entry = progress[seq - 1]
        phases = entry.setdefault("phases", {})
        phase_dict = phases.setdefault(phase, {})
        retries = int(phase_dict.get("timeout_retries") or 0) + 1
        phase_dict["timeout_retries"] = retries
        phase_dict["timeout_last_ts"] = _io.now_iso()
        captured["retries"] = retries

    try:
        update_state(p.state_json, p.state_md, mutate)
    except ValueError as e:
        _io.emit_error("seq_out_of_range", str(e), exit_code=1)
        return
    except FileNotFoundError as e:
        _io.emit_error("state_not_found", str(e), exit_code=3)
        return

    retries = captured["retries"]
    next_budget = _compute_budget(retries, base, mult, max_sec)
    _io.emit(
        {
            "ok": True,
            "seq": seq,
            "phase": phase,
            "retries": retries,
            "next_timeout_sec": next_budget,
            "exhausted": _exhausted(retries),
            "max_reached": next_budget >= max_sec,
        }
    )
