"""Isolated change lifecycle (1.8.2).

The change lock protects its worktree/receipts, not main. A prepared patch is
reviewed at an immutable commit. Integration preserves that patch's identity
(``git patch-id --verbatim``: whitespace-sensitive, independent of line offsets
and preimage blob ids, excluding declared derived files) or requests a review of
the integration delta, tests the combined tree outside the main lock, then
publishes only if main still has the captured base. Conflicts are resolved
automatically only when confined to derived files that ``[integrate].regenerate``
rebuilds. Worktrees are retained for recovery; the only reset/checkout operations
are ``merge --abort`` of a merge started here and taking the target's version of
conflicted derived files. The target worktree is only fast-forwarded.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
import shlex
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
    # Legacy (<=1.8.1) receipt digest input; kept to validate receipts prepared
    # before patch identities were introduced.
    return git(root, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-renames", base, tip, "--")


def pathspecs(derived, *, exclude: bool = False) -> list[str]:
    magic = "exclude,glob" if exclude else "glob"
    return [f":({magic}){pattern}" for pattern in derived]


def names(root: Path, *args: str) -> list[str]:
    return [line for line in git(root, *args).splitlines() if line]


def identity(root: Path, base: str, tip: str, derived=()) -> str:
    """Patch identity of base..tip, excluding derived files.

    Hunk content (including context and whitespace), file modes, deletions and
    binary data are covered; hunk line numbers and preimage blob ids are not,
    so unrelated target edits elsewhere in a touched file keep the identity.
    """
    diff = subprocess.run(
        ["git", "diff", "--binary", "--no-color", "--no-ext-diff", "--no-renames", base, tip,
         "--", ".", *pathspecs(derived, exclude=True)],
        cwd=root, capture_output=True, check=True).stdout
    out = subprocess.run(["git", "patch-id", "--verbatim"], cwd=root, input=diff,
                         capture_output=True, check=True).stdout.decode()
    return out.split()[0] if out.strip() else ""


def receipt_derived(root: Path, receipt: dict, configured) -> tuple[str, ...]:
    """Derived set for a receipt: pinned at preparation, or (1.8.1 receipts) the current
    config unless the patch consists only of derived files."""
    if "derived" in receipt:
        return tuple(receipt["derived"])
    derived = tuple(configured)
    if derived and not identity(root, receipt["base_commit"], receipt["head"], derived):
        return ()
    return derived


def receipt_identity(root: Path, receipt: dict, derived=()) -> str:
    if "patch_id" not in receipt:
        digest = hashlib.sha256(patch(root, receipt["base_commit"], receipt["head"]).encode()).hexdigest()
        if digest != receipt.get("patch_sha256"):
            raise ValueError("prepared patch receipt mismatch")
    elif tuple(receipt.get("derived") or ()) == tuple(derived):
        return receipt["patch_id"]
    return identity(root, receipt["base_commit"], receipt["head"], derived)


def load(p: paths.Paths, config_path: Path | None = None) -> config.Config:
    return config.load_config(p.config_root or p.repo_root, override_path=config_path)


def untracked(root: Path, *spec: str) -> set[str]:
    return set(names(root, "ls-files", "--others", "--exclude-standard", *(("--", *spec) if spec else ())))


def worktree_files(root: Path) -> set[str]:
    """Untracked and ignored files, listed individually (never directory-collapsed)."""
    return set(names(root, "ls-files", "--others", "--exclude-standard")) | set(
        names(root, "ls-files", "--others", "--ignored", "--exclude-standard"))


def abort_merge(root: Path, reason: str, files_before: set[str], **fields) -> dict:
    """Abort the merge started by :func:`merge_target` and restore the pre-merge worktree.

    Tracked edits are staged first so ``merge --abort`` (reset --merge) restores them
    too. Only files absent from the pre-merge snapshot are removed, then directories
    left empty by those removals; pre-existing untracked or ignored content is kept.
    """
    subprocess.run(["git", "add", "-u"], cwd=root, capture_output=True)
    subprocess.run(["git", "merge", "--abort"], cwd=root, capture_output=True)
    for name in worktree_files(root) - files_before:
        path = root / name
        if not (path.is_symlink() or path.is_file()):
            continue
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    try:
        clean(root)
    except ValueError as exc:
        fields["abort_incomplete"] = str(exc)
    return {"ok": False, "reason": reason, **fields}


def merge_target(root: Path, base: str, integrate: config.IntegrateConfig) -> dict:
    """Merge the target commit into the (clean) change worktree.

    Conflicts confined to derived files take the target version (or the target's
    deletion) and are rebuilt by the regenerate commands; derived files edited on
    both sides are rebuilt as well. Regeneration must rewrite every conflicted
    derived file the target still has and must not touch other files. Any other
    outcome aborts the merge, leaving the previous HEAD.
    """
    if ancestor(root, base):
        return {"ok": True, "regenerated": [], "conflicts": []}
    before = head(root)
    derived = integrate.derived
    both = set()
    if integrate.regenerate and derived:
        ours = names(root, "diff", "--name-only", "--no-renames", f"{base}...{before}", "--", *pathspecs(derived))
        theirs = names(root, "diff", "--name-only", "--no-renames", f"{before}...{base}", "--", *pathspecs(derived))
        both = set(ours) & set(theirs)
    snapshot = worktree_files(root)
    # Git overwrites ignored files silently when the target adds a tracked file at the
    # same path; refuse before merging so local content is never clobbered.
    incoming = set(names(root, "diff", "--name-only", "--no-renames", "--diff-filter=A", f"{before}...{base}"))
    if incoming & snapshot:
        return {"ok": False, "reason": "merge-would-overwrite-ignored", "files": sorted(incoming & snapshot)}

    def abort(reason: str, **fields) -> dict:
        return abort_merge(root, reason, snapshot, **fields)

    proc = subprocess.run(["git", "merge", "--no-edit", "--no-commit", "--no-ff", base],
                          cwd=root, capture_output=True, text=True)
    try:
        conflicts = names(root, "diff", "--name-only", "--diff-filter=U")
        if proc.returncode and not conflicts:
            return abort("merge-failed", detail=(proc.stderr or proc.stdout)[-1500:])
        required: set[str] = set()
        if conflicts:
            derived_conflicts = (names(root, "diff", "--name-only", "--diff-filter=U", "--", *pathspecs(derived))
                                 if integrate.regenerate and derived else [])
            if set(derived_conflicts) != set(conflicts):
                return abort("merge-conflict", conflicts=conflicts,
                                   detail=(proc.stdout + proc.stderr)[-1500:])
            for name in conflicts:
                stages = {line.split()[2] for line in names(root, "ls-files", "-u", "--", name)}
                if "3" in stages:
                    git(root, "checkout", "--theirs", "--", name)
                    git(root, "add", "--", name)
                    required.add(name)
                else:  # deleted on the target side
                    git(root, "rm", "-q", "--", name)
        # Derived files edited on both sides were auto-merged textually; regeneration must rewrite them.
        required |= both
        targets = sorted(set(conflicts) | both)
        if targets:
            for cmd in integrate.regenerate:
                run = subprocess.run(shlex.split(cmd), cwd=root, capture_output=True, text=True)
                if run.returncode:
                    return abort("regenerate-failed", conflicts=conflicts, command=cmd,
                                       detail=(run.stdout + run.stderr)[-1500:])
            touched = set(names(root, "diff", "--name-only")) | untracked(root)
            rebuilt = set(names(root, "diff", "--name-only", "--", *pathspecs(derived))) | untracked(
                root, *pathspecs(derived))
            if touched - rebuilt:
                return abort("regenerate-touched-sources", conflicts=conflicts,
                                   files=sorted(touched - rebuilt))
            if required - rebuilt:
                return abort("regenerate-incomplete", conflicts=conflicts,
                                   files=sorted(required - rebuilt))
            if rebuilt:
                git(root, "add", "--", *sorted(rebuilt))
        # ``git commit`` concludes the merge; it runs pre-commit (not pre-merge-commit) hooks.
        git(root, "commit", "--no-edit")
    except ValueError as exc:
        return abort("merge-failed", detail=str(exc)[-1500:])
    dirty = git(root, "status", "--porcelain")
    if dirty:
        # A hook rewrote files after the merge commit was created; keep both for the agent.
        files = set(names(root, "diff", "--name-only", "HEAD")) | untracked(root)
        return {"ok": False, "reason": "hook-modified-worktree", "conflicts": conflicts, "files": sorted(files)}
    return {"ok": True, "regenerated": targets, "conflicts": conflicts}


def mark_reviewed(p: paths.Paths, seq: int, receipt: dict, derived, **isolation) -> None:
    """Record the last clean-reviewed patch so the next review covers only the integration delta."""
    e = entry(p, seq)
    reviewed = {"base": receipt["base_commit"], "head": receipt["head"], "derived": list(derived)}
    update(p, seq, isolation={**e["isolation"], **isolation, "reviewed": reviewed})


def validate_tests(p: paths.Paths, *, config_path: Path | None = None) -> dict:
    cfg = load(p, config_path)
    cmd = verify.resolve_test_cmd(p.repo_root, cfg)
    if cmd is None:
        return {"ok": True, "tests": "skipped", "reason": "no-test-command"}
    proc = verify.run_test_cmd(p.repo_root, cmd, timeout=cfg.verify.test_timeout)
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
    derived = list(load(wp, config_path).integrate.derived)
    patch_id = identity(wp.repo_root, base, tip, derived)
    if derived and not patch_id and identity(wp.repo_root, base, tip):
        # A change consisting only of derived files keeps them under the identity check.
        derived, patch_id = [], identity(wp.repo_root, base, tip)
    receipt = {"head": tip, "base_commit": base, "review_round": reviewed["round"],
               "patch_id": patch_id, "derived": derived, "tests": tests}
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
    integrate_cfg = load(wp, Path(config_path) if config_path else None).integrate
    derived = receipt_derived(wp.repo_root, receipt, integrate_cfg.derived)
    integrate_cfg = config.IntegrateConfig(derived=derived, regenerate=integrate_cfg.regenerate if derived else ())
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
            expected = receipt_identity(wp.repo_root, receipt, derived)
            merged = merge_target(wp.repo_root, base, integrate_cfg)
            if not merged.pop("ok"):
                # The merge started here was aborted; all earlier work is retained.
                mark_reviewed(p, seq, receipt, derived)
                return result(seq, "needs-resolution", worktree=str(wp.repo_root), **merged)
            tip = head(wp.repo_root)
            if merged["regenerated"]:
                from .integrate import _emit_run_event
                _emit_run_event(p, seq, e["change_id"], {"event": "integrate.regenerated",
                               "files": merged["regenerated"], "conflicts": merged["conflicts"]})
            if identity(wp.repo_root, base, tip, derived) != expected:
                mark_reviewed(p, seq, receipt, derived, base_commit=base)
                update(p, seq, prepared=None, candidate=None, status="needs-review")
                return result(seq, "needs-review", reason="patch-changed", worktree=str(wp.repo_root),
                              regenerated=merged["regenerated"])
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
                mark_reviewed(p, seq, receipt, derived)
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
    try:
        out.setdefault("change_id", entry(p, args.seq).get("change_id"))
    except (ValueError, OSError):
        pass
    from .integrate import _emit_run_event
    _emit_run_event(p, args.seq, out.get("change_id"),
                   {"event": "isolated.transition", "status": out.get("status"),
                    "reason": out.get("reason"), "duration_ms": out["duration_ms"],
                    **{k: out[k] for k in ("conflicts", "files", "regenerated") if out.get(k)}})
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
