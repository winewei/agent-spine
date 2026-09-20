"""Isolated change lifecycle (1.8.1).

The change lock protects its worktree/receipts, not main. A prepared patch is
reviewed at an immutable commit. Integration preserves that patch byte-for-byte
or requests another review, tests the combined tree outside the main lock, then
publishes only if main still has the captured base. Worktrees are retained for
recovery; no reset, forced checkout, or automatic conflict resolution is used.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess

from . import change, config, git_chain, locks, paths, pipeline, state, target, verify


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if proc.returncode:
        raise ValueError(f"git {' '.join(args[:2])}: {proc.stderr.strip()[-1500:]}")
    return proc.stdout.strip()


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD")


def ancestor(root: Path, commit: str, target: str = "HEAD") -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", commit, target],
                          cwd=root, capture_output=True).returncode == 0


def clean(root: Path) -> None:
    if git(root, "status", "--porcelain"):
        raise ValueError(f"worktree has uncommitted changes; preserve and reconcile them: {root}")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", marker)).exists():
            raise ValueError(f"unfinished git operation: {marker} in {root}")


def entry(p: paths.Paths, seq: int) -> dict:
    return change._entry(state.read_state(p.state_json), seq)


def update(p: paths.Paths, seq: int, **fields) -> None:
    change._update_entry(p, seq, lambda e: e.update(fields))


def lock_path(p: paths.Paths, seq: int) -> Path:
    return p.run_dir / f".change-{seq}.lock"


def result(seq: int, status: str, **fields) -> dict:
    return {"ok": status in ("ready-to-integrate", "archived"), "seq": seq,
            "status": status, **fields}


def workspace(p: paths.Paths, seq: int, supplied: str | None = None) -> paths.Paths:
    target.check(p, required=True)
    e = entry(p, seq)
    isolated = e.get("isolation") or {}
    root = Path(isolated.get("worktree") or supplied or (p.run_dir / "worktrees" / str(seq))).resolve()
    if supplied and Path(supplied).resolve() != root:
        raise ValueError("change already owns another worktree")
    if root == p.repo_root.resolve():
        raise ValueError("isolated worktree must differ from the target worktree")
    if root.exists():
        if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("not a worktree root")
        if git(root, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(
                p.repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise ValueError("worktree belongs to another repository")
    for other in state.read_state(p.state_json).get("progress", []):
        if other.get("seq") != seq and (other.get("isolation") or {}).get("worktree") == str(root):
            raise ValueError("worktree already owned by another change")
    # Intent is persisted before worktree creation, so recovery can adopt an
    # already-created worktree without spawning a second implementation.
    if not isolated:
        base = head(p.repo_root)
        if supplied:
            base = git(p.repo_root, "merge-base", base, head(root))
        isolated = {"worktree": str(root), "base_commit": base}
        update(p, seq, isolation=isolated)
    if not root.exists():
        if supplied:
            raise ValueError("supplied worktree is missing")
        root.parent.mkdir(parents=True, exist_ok=True)
        git(p.repo_root, "worktree", "add", "--detach", str(root), isolated["base_commit"])
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("not a worktree root")
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common != git(p.repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir"):
        raise ValueError("worktree belongs to another repository")
    return replace(p, repo_root=root, config_root=p.repo_root)


def patch(root: Path, base: str, tip: str) -> str:
    # No abbreviated object names; identical patches imply identical affected
    # file preimages/postimages, including modes, deletions and binary content.
    return git(root, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-renames", base, tip, "--")


def validate_tests(p: paths.Paths, *, config_path: Path | None = None) -> dict:
    cfg = config.load_config(p.config_root or p.repo_root, override_path=config_path)
    cmd = verify.resolve_test_cmd(p.repo_root, cfg)
    if cmd is None:
        return {"ok": True, "tests": "skipped", "reason": "no-test-command"}
    proc = verify.run_test_cmd(p.repo_root, cmd)
    return {"ok": proc.returncode == 0, "tests": "pass" if proc.returncode == 0 else "fail",
            "cmd": cmd, "tail": verify._tail(proc.stdout or "", proc.stderr or "", lines=30)[-3000:]}


def recover_coder(p: paths.Paths, seq: int) -> dict | None:
    """Recover a returned coder RESULT before reissuing a paid model call."""
    import re
    e = entry(p, seq)
    base = Path(e.get("base") or paths.base_for(p, seq, e["change_id"]))
    for phase, info in (e.get("phases") or {}).items():
        match = re.fullmatch(r"fix-r(\d+)", phase)
        if phase != "implement" and not match:
            continue
        if info.get("status") != "in-progress":
            continue
        filename = "implement.result.txt" if phase == "implement" else f"round-{match[1]}.fix.result.txt"
        receipt = base / filename
        if receipt.is_file():
            line = receipt.read_text()
            parsed = pipeline._parse_result_line(line, []) or {}
            commit = parsed.get("commit", "-")
            if commit != "-" and ancestor(p.repo_root, commit) and parsed.get("tests") == "pass":
                out = (pipeline.record_implement(p, seq, line) if phase == "implement"
                       else pipeline.record_fix(p, seq, int(match[1]), line))
                if not out.get("ok"):
                    return out
                continue
        if info.get("started_head") and info["started_head"] != head(p.repo_root):
            return result(seq, "needs-recovery", reason="unrecorded-coder-commits",
                          phase=phase, worktree=str(p.repo_root))
    return None


def run(p: paths.Paths, seq: int, *, worktree: str | None = None,
        result_file: str | None = None, manifest: str | None = None, **options) -> dict:
    """Caller holds the per-change lock. Never acquires main's lock."""
    e = entry(p, seq)
    if e.get("status") == "archived":
        return result(seq, "archived", archive_commit=e.get("archive_commit"))
    wp = workspace(p, seq, worktree)
    clean(wp.repo_root)
    e = entry(p, seq)
    config_path = options.get("config_path") or e["isolation"].get("config_path")
    if config_path:
        config_path = Path(config_path).resolve()
        options["config_path"] = config_path
        update(p, seq, isolation={**e["isolation"], "config_path": str(config_path)})
    receipt = e.get("prepared") or {}
    if receipt.get("head") == head(wp.repo_root) and not options.get("from_phase") and not options.get("decision"):
        return result(seq, "ready-to-integrate", worktree=str(wp.repo_root), **receipt)
    if result_file:
        if not manifest:
            raise ValueError("--result-file requires --manifest")
        line = Path(result_file).read_text()
        parsed = verify.parse_result_verdict(line, manifest)
        if parsed["verdict"] != "code":
            raise ValueError(f"invalid implementation receipt: {parsed['reason']}")
        commit = git(wp.repo_root, "rev-parse", f"{parsed['commit']}^{{commit}}")
        pending = e.get("pending_coder") or {}
        if pending or not e.get("implement_commit"):
            fields = pipeline._parse_result_line(line, []) or {}
            required = "fixed" if pending.get("phase") == "fix" else "tasks"
            if required not in fields:
                raise ValueError(f"receipt does not match pending phase: missing {required}")
            if commit != head(wp.repo_root):
                raise ValueError("RESULT commit must be worktree HEAD")
            if pending and not ancestor(wp.repo_root, pending["base_head"], commit):
                raise ValueError("coder rewrote task history")
            checked = verify.check_manifest_files(parsed["manifest"], repo_root=wp.repo_root, git_ref=commit)
            if not checked["ok"]:
                raise ValueError(f"invalid manifest: {checked}")
            artifact_base = Path(e.get("base") or paths.base_for(p, seq, e["change_id"]))
            artifact_base.mkdir(parents=True, exist_ok=True)
            filename = (f"round-{pending['round']}.fix.result.txt" if pending.get("phase") == "fix"
                        else "implement.result.txt")
            (artifact_base / filename).write_text(line)
            if pending.get("phase") == "fix":
                rec = pipeline.record_fix(wp, seq, pending["round"], line)
            else:
                rec = pipeline.record_implement(wp, seq, line)
            if not rec.get("ok"):
                return rec
            update(p, seq, pending_coder=None)
        elif not ancestor(wp.repo_root, commit):
            raise ValueError("implementation receipt is not in worktree history")
    e = entry(p, seq)
    if e.get("pending_coder"):
        return e["pending_coder"]
    if not options.get("from_phase"):
        recovered = recover_coder(wp, seq)
        if recovered is not None:
            return recovered
        e = entry(p, seq)
    if e.get("implement_commit") and not git_chain.check_chain(wp.repo_root, e)["ok"]:
        raise ValueError("recorded implementation/fixes missing from worktree; reconcile before resuming")
    if receipt or e.get("status") == "needs-review":
        options["from_phase"] = "review"
        base = git(wp.repo_root, "merge-base", head(p.repo_root), head(wp.repo_root))
        update(p, seq, isolation={**e["isolation"], "base_commit": base})
    update(p, seq, prepared=None, candidate=None)
    out = change.run_change(wp, seq, defer_archive=True, **options)
    if out.get("status") != "ready-to-integrate":
        return out
    e = entry(p, seq)
    reviewed = e.get("last_review") or {}
    tip = head(wp.repo_root)
    # Even auto-decide force-archive cannot bypass this gate. The isolated
    # publication protocol always requires a clean review of this exact HEAD.
    if reviewed.get("head") != tip or reviewed.get("blocking") != 0:
        update(p, seq, status="needs-review")
        return result(seq, "needs-review", reason="clean-review-required", worktree=str(wp.repo_root))
    clean(wp.repo_root)
    if not git_chain.check_chain(wp.repo_root, e)["ok"]:
        raise ValueError("recorded commits missing after inner loop")
    tests = validate_tests(wp, **({"config_path": config_path} if config_path else {}))
    if not tests["ok"]:
        update(p, seq, status="needs-review")
        return result(seq, "tests-failed", worktree=str(wp.repo_root), **{"validation": tests})
    clean(wp.repo_root)
    if head(wp.repo_root) != tip:
        raise ValueError("HEAD changed during validation")
    base = e["isolation"]["base_commit"]
    receipt = {"head": tip, "base_commit": base, "review_round": reviewed["round"],
               "patch_sha256": hashlib.sha256(patch(wp.repo_root, base, tip).encode()).hexdigest(),
               "tests": tests}
    update(p, seq, status="ready-to-integrate", prepared=receipt)
    return result(seq, "ready-to-integrate", worktree=str(wp.repo_root), **receipt)


def publish(p: paths.Paths, seq: int) -> dict:
    """Validate a merge candidate outside main, then fast-forward and archive.

    A concurrent publisher causes target-moved, never a stale validation receipt
    applied to a newer HEAD. Retry reuses the prepared work and cached tests.
    """
    e = entry(p, seq)
    if e.get("status") == "archived":
        return result(seq, "archived", archive_commit=e.get("archive_commit"))
    if not e.get("isolation"):
        raise ValueError("no isolated worktree; run change run --isolated first")
    wp = workspace(p, seq)
    receipt = e.get("prepared") or {}
    if not receipt:
        raise ValueError("no prepared receipt; run change run --isolated first")
    clean(wp.repo_root)
    config_path = e["isolation"].get("config_path")
    config_options = {"config_path": Path(config_path)} if config_path else {}
    published = e.get("publication") or {}
    # Recover a crash after fast-forward but before state/archive recording.
    already = published.get("head") and ancestor(p.repo_root, published["head"])
    if not already:
        base = head(p.repo_root)
        candidate = e.get("candidate") or {}
        if candidate.get("base") != base or candidate.get("head") != head(wp.repo_root):
            allowed = {receipt["head"], candidate.get("head")}
            if head(wp.repo_root) not in allowed:
                return result(seq, "needs-review", reason="worktree-changed")
            expected = patch(wp.repo_root, receipt["base_commit"], receipt["head"])
            if hashlib.sha256(expected.encode()).hexdigest() != receipt["patch_sha256"]:
                raise ValueError("prepared patch receipt mismatch")
            proc = subprocess.run(["git", "merge", "--no-edit", base], cwd=wp.repo_root,
                                  capture_output=True, text=True)
            if proc.returncode:
                # Abort only the merge started here, retaining all earlier work.
                subprocess.run(["git", "merge", "--abort"], cwd=wp.repo_root, capture_output=True)
                return result(seq, "needs-resolution", reason="merge-conflict",
                              worktree=str(wp.repo_root), detail=proc.stderr[-1500:])
            tip = head(wp.repo_root)
            if patch(wp.repo_root, base, tip) != expected:
                iso = {**e["isolation"], "base_commit": base}
                update(p, seq, isolation=iso, prepared=None, candidate=None, status="needs-review")
                return result(seq, "needs-review", reason="patch-changed", worktree=str(wp.repo_root))
            # Persist the candidate before tests: failures retain a resumable HEAD.
            candidate = {"base": base, "head": tip}
            if tip == receipt["head"] and receipt.get("tests", {}).get("ok"):
                candidate["tests"] = receipt["tests"]
            update(p, seq, candidate=candidate)
        else:
            tip = candidate["head"]
        if not candidate.get("tests", {}).get("ok"):
            tests = validate_tests(wp, **config_options)
            clean(wp.repo_root)
            if head(wp.repo_root) != tip:
                raise ValueError("HEAD changed during integration tests")
            candidate["tests"] = tests
            update(p, seq, candidate=candidate)
            if not tests["ok"]:
                return result(seq, "tests-failed", validation=tests, worktree=str(wp.repo_root))
    target_lock = locks.try_acquire(locks.main_lock_path(p.task_log_dir), owner=f"publish seq={seq}")
    if target_lock is None:
        return result(seq, "target-busy", reason="retry-publication")
    try:
        target.check(p, required=True)
        clean(p.repo_root)
        if not already:
            if head(p.repo_root) != base:
                return result(seq, "target-moved", reason="retry-publication")
            # Write ahead of the side effect; subsequent calls check ancestry.
            update(p, seq, publication={"base": base, "head": tip})
            git(p.repo_root, "merge", "--ff-only", tip)
        update(p, seq, status="integrated", integrated_commit=(published.get("head") if already else tip))
        from .integrate import _emit_run_event
        _emit_run_event(p, seq, e["change_id"], {"event": "integrate.done",
                       "integrated_commit": head(p.repo_root), "isolated": True})
        archive = pipeline.run_archive(p, seq, **config_options)
        if not archive.get("ok"):
            return result(seq, "archive-failed", **{"detail": archive})
        return {**archive, "status": "archived"}
    finally:
        locks.release(target_lock)


def _cli(p: paths.Paths, args: argparse.Namespace, action) -> None:
    import sys
    import time
    from . import _io
    started = time.monotonic()
    try:
        with locks.held(lock_path(p, args.seq), owner=f"isolated seq={args.seq}", wait_sec=0):
            out = action()
    except locks.LockBusy:
        out = result(args.seq, "change-busy", reason="existing-worker-owns-change")
    except (ValueError, OSError, config.ConfigError, subprocess.SubprocessError) as exc:
        out = result(args.seq, "error", reason=str(exc))
    out["duration_ms"] = int((time.monotonic() - started) * 1000)
    from .integrate import _emit_run_event
    _emit_run_event(p, args.seq, out.get("change_id"),
                   {"event": "isolated.transition", "status": out.get("status"),
                    "reason": out.get("reason"), "duration_ms": out["duration_ms"]})
    _io.emit(out)
    if not out.get("ok"):
        sys.exit(5 if out.get("status") == "needs-decision" else 1)


def cli_run(p: paths.Paths, args: argparse.Namespace) -> None:
    options = {k: getattr(args, k, default) for k, default in (
        ("from_phase", None), ("decision", None), ("max_rounds", change.DEFAULT_MAX_ROUNDS),
        ("auto", False), ("backend", None), ("coder_timeout", None),
        ("review_retries", 1), ("review_timeout", 900), ("handoff", False))}
    options["engine_name"] = getattr(args, "engine", None)
    options["config_path"] = Path(args.config).resolve() if getattr(args, "config", None) else None
    _cli(p, args, lambda: run(p, args.seq, worktree=getattr(args, "worktree", None),
                             result_file=getattr(args, "result_file", None),
                             manifest=getattr(args, "manifest", None), **options))


def cli_publish(p: paths.Paths, args: argparse.Namespace) -> None:
    if getattr(args, "force", False) or getattr(args, "no_verify_tests", False):
        from . import _io
        _io.emit_error("invalid_args", "--prepared does not permit --force or --no-verify-tests", exit_code=2)
        return
    _cli(p, args, lambda: publish(p, args.seq))
