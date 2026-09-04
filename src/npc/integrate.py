"""npc integrate —— worktree 产物整合进 main 的多步编排下沉（v1.5，P2）。

替代 v3 skill Step 9 里那段最大的"skill 内多行 bash"（cherry-pick + sed 换 hash
+ implement record + verify tests + fail 则 revert）。设计依据
docs/optimization-proposals/2026-07-05-orchestration-context-budget.md §3.3。

单命令完成（任一步失败即收拾现场返回结构化错误，main 保持绿）：

1. ``verify manifest``：RESULT 行 plan-only 判定 + manifest 文件存在性/sha 核对。
2. ``git cherry-pick <worktree_commit>``：失败 → ``cherry-pick --abort``。
3. **hash 翻译**：RESULT 行的 ``commit=<worktree_hash>`` 换成整合后 main HEAD——
   这是必要项而非防御（archive precheck 用 merge-base --is-ancestor 校验 chain，
   worktree 原始 hash 不在 main 链上会被判 chain-broken）。
4. ``implement record``：装订 state；失败 → ``git revert`` 摘除该 commit。
5. ``verify tests``：main 上真实复跑（不裸信 RESULT 自报）；失败 → revert +
   progress 状态回退 failed。探测不到测试命令 → skipped（警告不阻塞）。

退出码：0 成功；1 任一步失败（stdout 带 step 定位）；2 用法错；3 环境错。
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from . import _io, paths as _paths, pipeline as _pipeline, state as _state, telemetry as _telemetry, verify as _verify
from . import config as _config
from . import events as _events
from . import locks as _locks


def _git(repo_root: Path, *argv: str, runner=subprocess.run) -> subprocess.CompletedProcess:
    return runner(
        ["git", *argv], cwd=str(repo_root), capture_output=True, text=True
    )


def _head(repo_root: Path, runner=subprocess.run) -> str:
    out = _git(repo_root, "rev-parse", "HEAD", runner=runner)
    return out.stdout.strip()


def _translate_result(result_line: str, old_commit: str, new_commit: str) -> str:
    """把 RESULT 行的 ``commit=<old>`` 换成 ``commit=<new>``（词边界，防前缀误伤）。"""
    return re.sub(
        rf"commit={re.escape(old_commit)}(?=\s|$)", f"commit={new_commit}", result_line
    )


def _revert(repo_root: Path, commit: str, runner=subprocess.run) -> bool:
    rv = _git(repo_root, "revert", "--no-edit", commit, runner=runner)
    return rv.returncode == 0


def _fail(
    step: str,
    reason: str,
    *,
    seq: int,
    reverted: str | None = None,
    extra: dict | None = None,
) -> dict:
    out = {
        "ok": False,
        "seq": seq,
        "step": step,
        "reason": reason,
        "reverted": reverted,
    }
    if extra:
        out.update(extra)
    return out


def _emit_run_event(p: _paths.Paths, seq: int, change_id: str | None, payload: dict) -> None:
    """best-effort 追加 integrate 事件到 run.events.jsonl（失败不阻塞）。"""
    try:
        entry_base = _paths.base_for(p, seq, change_id or "unknown")
        _events.append_event(
            entry_base / "events.jsonl",
            p.run_events,
            {"ts": _io.now_utc_iso(), "change_seq": seq, "change_id": change_id, **payload},
        )
    except OSError:
        pass


_MAIN_MUTATING_PHASE_RE = re.compile(r"^(review-r\d+|fix-r\d+|archive)$")


def inner_loop_active(progress: list[dict], *, exclude_seq: int | None = None) -> list[dict]:
    """列出仍在 main worktree 上跑内环（review / fix / archive 处于 in-progress）的 change。

    integrate 会在 main 上 cherry-pick、跑测试、读改写 state.json；与 ``npc change run``
    的 fix commit / archive commit 并发会导致 git 锁冲突、测错 HEAD、state 覆盖写。
    implement 阶段在 worktree 内进行，不算占用 main。
    """
    active: list[dict] = []
    for idx, entry in enumerate(progress, start=1):
        if idx == exclude_seq:
            continue
        for phase, info in ((entry or {}).get("phases") or {}).items():
            if _MAIN_MUTATING_PHASE_RE.match(phase) and (info or {}).get("status") == "in-progress":
                active.append({"seq": idx, "change_id": entry.get("change_id"), "phase": phase})
    return active


def run_integrate(
    p: _paths.Paths,
    seq: int,
    result_line: str,
    manifest_path: str | None,
    *,
    verify_tests: bool = True,
    force: bool = False,
    runner=subprocess.run,
) -> dict:
    """整合编排主体。返回结构化结果 dict（ok=false 时带 step 定位）。

    **main 互斥**：整合全程持 ``<task_log_dir>/.main.lock``（非阻塞抢锁）。锁被
    ``npc change run``（fix / archive 在 main 上提交）或另一个 integrate 持有时，未做任何
    改动即返回 ``step=inner-loop-active, reason=main-busy``，附锁持有者与 state 里处于
    in-progress 的内环 phase 快照供诊断；调用方排队到内环结束后重试。
    ``force=True`` 跳过互斥（仅用于人工确认无并发、或清理残留锁后的补偿整合）。
    """
    state = _state.read_state(p.state_json)
    progress = state.get("progress") or []
    if not (1 <= seq <= len(progress)):
        raise ValueError(f"seq={seq} 超出 progress 数组长度（total={len(progress)}）")
    change_id = progress[seq - 1].get("change_id")

    if force:
        return _run_integrate_locked(
            p, seq, change_id, result_line, manifest_path,
            verify_tests=verify_tests, runner=runner,
        )
    lock_path = _locks.main_lock_path(p.task_log_dir)
    fh = _locks.try_acquire(lock_path, owner=f"integrate seq={seq} {change_id}")
    if fh is None:
        return _fail(
            "inner-loop-active", "main-busy", seq=seq,
            extra={
                "holder": _locks.read_holder(lock_path),
                "active": inner_loop_active(progress, exclude_seq=seq),
                "hint": "等待 change run / 另一 integrate 结束后重试；确认无并发后可 --force",
            },
        )
    try:
        return _run_integrate_locked(
            p, seq, change_id, result_line, manifest_path,
            verify_tests=verify_tests, runner=runner,
        )
    finally:
        _locks.release(fh)


def _run_integrate_locked(
    p: _paths.Paths,
    seq: int,
    change_id: str | None,
    result_line: str,
    manifest_path: str | None,
    *,
    verify_tests: bool,
    runner,
) -> dict:

    # 1. verify manifest（plan-only 判定 + 文件核对）
    parsed = _verify.parse_result_verdict(result_line, manifest_path)
    if parsed["verdict"] != "code":
        return _fail(
            "verify-manifest", parsed["reason"] or parsed["verdict"], seq=seq,
            extra={"verdict": parsed["verdict"]},
        )
    files = _verify.check_manifest_files(
        parsed["manifest"], repo_root=p.repo_root, git_ref=parsed["commit"], runner=runner,
    )
    if not files["ok"]:
        return _fail(
            "verify-manifest", files["reason"], seq=seq,
            extra={"verdict": "code", "files": files},
        )
    worktree_commit = parsed["commit"]

    # 1b. diff 基线：整合前在当前 HEAD 记录失败集合（仅 [verify].test_baseline="diff"）
    test_cmd: str | None = None
    baseline_failed: set[str] | None = None
    if verify_tests:
        try:
            cfg = _config.load_config(p.repo_root)
        except _config.ConfigError:
            cfg = _config.Config()
        test_cmd = _verify.resolve_test_cmd(p.repo_root, cfg)
        if test_cmd is not None and cfg.verify.test_baseline == "diff":
            base_proc = _verify.run_test_cmd(p.repo_root, test_cmd, runner=runner)
            baseline_failed = (
                set() if base_proc.returncode == 0
                else _verify.parse_failed_ids(base_proc.stdout or "", base_proc.stderr or "")
            )
            _emit_run_event(
                p, seq, change_id,
                {"event": "integrate.baseline", "head": _head(p.repo_root, runner=runner),
                 "baseline_failed": len(baseline_failed)},
            )

    # 2. cherry-pick
    cp = _git(p.repo_root, "cherry-pick", worktree_commit, runner=runner)
    if cp.returncode != 0:
        _git(p.repo_root, "cherry-pick", "--abort", runner=runner)
        _emit_run_event(
            p, seq, change_id,
            {"event": "integrate.conflict", "worktree_commit": worktree_commit},
        )
        _telemetry.emit_deviation(
            proj_key=p.proj_key, run_ts=p.run_ts, change_seq=seq, change_id=change_id,
            trigger="cherry-pick-conflict", action="abort", phase="integrate",
            layer="decompose", state_json=p.state_json, run_events=p.run_events,
        )
        return _fail(
            "cherry-pick", "conflict", seq=seq,
            extra={
                "worktree_commit": worktree_commit,
                "stderr_tail": (cp.stderr or "").strip()[-1000:],
            },
        )
    integrated = _head(p.repo_root, runner=runner)

    # 3. hash 翻译 + 4. implement record
    translated = _translate_result(result_line, worktree_commit, integrated)
    rec = _pipeline.record_implement(p, seq, translated)
    if not rec.get("ok"):
        reverted = _revert(p.repo_root, integrated, runner=runner)
        return _fail(
            "record", rec.get("error", "record-failed"), seq=seq,
            reverted=integrated if reverted else None,
            extra={"worktree_commit": worktree_commit},
        )

    # 5. verify tests（真实复跑；探测不到命令 → skipped）
    #    strict 模式（未取基线）在 cherry-pick 之后重新探测测试命令：change 可能首次
    #    引入 pyproject / tests/ / Makefile test target，整合前探测不到不代表无测试。
    #    diff 模式保留整合前的命令，保证与基线可比。
    tests_status = "skipped"
    tests_detail: dict | None = None
    if verify_tests:
        cmd = test_cmd
        if baseline_failed is None:
            try:
                cfg_after = _config.load_config(p.repo_root)
            except _config.ConfigError:
                cfg_after = _config.Config()
            cmd = _verify.resolve_test_cmd(p.repo_root, cfg_after)
        if cmd is not None:
            proc = _verify.run_test_cmd(p.repo_root, cmd, runner=runner)
            judged = _verify.judge_against_baseline(proc, baseline_failed)
            tests_detail = {k: v for k, v in judged.items() if k != "passed"}
            if not judged["passed"]:
                reverted = _revert(p.repo_root, integrated, runner=runner)
                # 诊断三件套（命令 / 判定明细 / 输出尾段）——只报 reverted 无法定位失败原因
                tail = _verify._tail(proc.stdout or "", proc.stderr or "", lines=40)[-4000:]

                def mut(st: dict) -> None:
                    e = (st.get("progress") or [])[seq - 1]
                    e["status"] = "failed"
                    e["reason"] = "verify-tests-failed"
                    e["verify_tests_detail"] = tests_detail

                _state.update_state(p.state_json, p.state_md, mut)
                _emit_run_event(
                    p, seq, change_id,
                    {
                        "event": "integrate.verify_tests_failed",
                        "reverted": integrated,
                        "cmd": cmd,
                        "tests": tests_detail,
                        "tail": tail,
                    },
                )
                _telemetry.emit_deviation(
                    proj_key=p.proj_key, run_ts=p.run_ts, change_seq=seq,
                    change_id=change_id, trigger="verify-tests-failed", action="revert",
                    phase="integrate", state_json=p.state_json, run_events=p.run_events,
                )
                return _fail(
                    "verify-tests", "tests-failed", seq=seq,
                    reverted=integrated if reverted else None,
                    extra={"cmd": cmd, "tests": tests_detail, "tail": tail},
                )
            tests_status = "pass" if judged["mode"] == "strict" or judged["failed"] == 0 else "pass-baseline-diff"
        else:
            _io.warn("integrate: 未探测到测试命令，verify tests 跳过")

    _emit_run_event(
        p, seq, change_id,
        {
            "event": "integrate.done",
            "worktree_commit": worktree_commit,
            "integrated_commit": integrated,
            "verify_tests": tests_status,
            "tests": tests_detail,
        },
    )
    return {
        "ok": True,
        "seq": seq,
        "change_id": change_id,
        "worktree_commit": worktree_commit,
        "integrated_commit": integrated,
        "verify_tests": tests_status,
        "tests": tests_detail,
        "files": {"present": files["present"], "total": files["total"]},
    }


# ============================================================
# CLI handler
# ============================================================


def cli_integrate(args: argparse.Namespace) -> None:
    """``npc integrate`` handler。"""
    import sys

    try:
        p = _paths.load_paths(args)
    except _paths.PathsError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    result_line = args.result
    if not result_line and getattr(args, "result_file", None):
        try:
            result_line = Path(args.result_file).read_text(encoding="utf-8")
        except OSError as e:
            _io.emit_error("input_unreadable", f"--result-file 读取失败：{e}", exit_code=2)
            return
    if not result_line:
        _io.emit_error("invalid_args", "必须提供 --result 或 --result-file", exit_code=2)
        return

    try:
        result = run_integrate(
            p,
            args.seq,
            result_line,
            getattr(args, "manifest", None),
            verify_tests=not getattr(args, "no_verify_tests", False),
            force=bool(getattr(args, "force", False)),
        )
    except FileNotFoundError as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return
    except ValueError as e:
        _io.emit_error("invalid_args", str(e), exit_code=2)
        return

    _io.emit(result)
    if not result.get("ok"):
        sys.exit(1)
