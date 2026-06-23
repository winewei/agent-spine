"""Archive 前 commit chain 校验。

读 progress[seq-1].implement_commit 与所有 phases.fix-rN.commit；
逐一 git merge-base --is-ancestor <c> HEAD；记录缺失者。
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from . import _io, paths as _paths, state as _state


def collect_expected_commits(progress_entry: dict) -> list[str]:
    """从 progress 条目按时序收集应在 HEAD 链上的所有 commit。"""
    commits: list[str] = []
    impl = progress_entry.get("implement_commit")
    if impl:
        commits.append(impl)
    phases = progress_entry.get("phases") or {}
    # 按 fix-rN 编号升序取 commit
    fix_keys = []
    for k in phases.keys():
        m = re.match(r"^fix-r(\d+)$", k)
        if m:
            fix_keys.append((int(m.group(1)), k))
    fix_keys.sort()
    for _, k in fix_keys:
        c = (phases.get(k) or {}).get("commit")
        if c:
            commits.append(c)
    return commits


def is_ancestor(repo_root: Path, commit: str) -> bool:
    """git merge-base --is-ancestor <commit> HEAD。"""
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=repo_root,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError("未找到 git 命令")
    return result.returncode == 0


def check_chain(repo_root: Path, progress_entry: dict) -> dict:
    """返回 {ok, expected, missing}。"""
    expected = collect_expected_commits(progress_entry)
    missing: list[str] = []
    for c in expected:
        try:
            if not is_ancestor(repo_root, c):
                missing.append(c)
        except RuntimeError as e:
            raise
    return {"ok": len(missing) == 0, "expected": expected, "missing": missing}


def commit_exists(repo_root: Path, commit: str) -> bool:
    """git cat-file -e <commit>：commit 对象是否存在（含 dangling / reflog）。

    注意：``git reset`` 后旧 commit 通常仍以 dangling 形式短期存在，``cat-file -e``
    仍返回 0。判定"是否还属于当前 HEAD 链"必须用 ``is_ancestor``。
    """
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", commit],
            cwd=repo_root,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError("未找到 git 命令")
    return result.returncode == 0


def scan_state_drift(repo_root: Path, state: dict) -> dict:
    """扫描 state.progress：检查所有非 pending 条目里记录的 commit 是否仍在 HEAD 链上。

    返回 ``{drifted_seqs: [...], total_drifted: N}``。每个 drifted_seq 含：
    ``{seq, change_id, status, missing_commits, missing_kinds}``。
    missing_kinds 记录每个 missing commit 属于哪类（implement / fix-rN / archive），
    供 ``npc state repair`` 决定如何回滚 progress。

    设计目的：``git reset`` 或外部 rebase 会让 task_log 与 git 完全脱钩
    （v1.0 实测：clean repo + 7 个 archived 状态共存，dangling commits 在 cat-file
    层面仍可寻，但实际 HEAD 早已不指向它们）。用 ``is_ancestor`` 才能识别"对象
    还在但已不属于当前历史"的漂移。
    """
    drifted: list[dict] = []
    for entry in state.get("progress") or []:
        if entry.get("status") in ("pending", None):
            continue
        kinds: list[tuple[str, str]] = []  # (kind, commit)
        impl = entry.get("implement_commit")
        if impl:
            kinds.append(("implement", impl))
        phases = entry.get("phases") or {}
        fix_keys = []
        for k in phases.keys():
            m = re.match(r"^fix-r(\d+)$", k)
            if m:
                fix_keys.append((int(m.group(1)), k))
        fix_keys.sort()
        for n, k in fix_keys:
            c = (phases.get(k) or {}).get("commit")
            if c:
                kinds.append((f"fix-r{n}", c))
        archive = entry.get("archive_commit")
        if archive:
            kinds.append(("archive", archive))

        missing_commits: list[str] = []
        missing_kinds: list[str] = []
        for kind, c in kinds:
            try:
                # 用 is_ancestor 判定 commit 是否仍属当前 HEAD 链：
                # cat-file -e 只查对象存在性，无法识别 git reset 后的 dangling commit
                if not is_ancestor(repo_root, c):
                    missing_commits.append(c)
                    missing_kinds.append(kind)
            except RuntimeError:
                pass  # git 完全缺失由外层兜底
        if missing_commits:
            drifted.append(
                {
                    "seq": entry.get("seq"),
                    "change_id": entry.get("change_id"),
                    "status": entry.get("status"),
                    "missing_commits": missing_commits,
                    "missing_kinds": missing_kinds,
                }
            )
    return {"drifted_seqs": drifted, "total_drifted": len(drifted)}


def precheck(args: argparse.Namespace) -> None:
    """archive precheck <seq>。

    成功（chain 完整）→ exit 0；失败 → exit 1（仍打印 JSON 含 missing 列表）。
    """
    try:
        p = _paths.load_paths(args)
        state = _state.read_state(p.state_json)
    except (_paths.PathsError, FileNotFoundError) as e:
        _io.emit_error("env_missing", str(e), exit_code=3)
        return

    progress = state.get("progress") or []
    if not (1 <= args.seq <= len(progress)):
        _io.emit_error(
            "seq_out_of_range",
            f"seq={args.seq} 超出 progress 长度（total={len(progress)}）",
            exit_code=1,
        )
        return

    entry = progress[args.seq - 1]
    try:
        result = check_chain(p.repo_root, entry)
    except RuntimeError as e:
        _io.emit_error("git_missing", str(e), exit_code=3)
        return

    _io.emit(result)
    if not result["ok"]:
        import sys

        sys.exit(1)
