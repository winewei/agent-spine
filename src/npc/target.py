"""The integration target is the branch checked out when the run starts."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess


def capture(root: Path) -> dict:
    ref = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=root,
                         capture_output=True, text=True)
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    return {"target_ref": ref.stdout.strip() or None,
            "target_initial_commit": tip.stdout.strip() or None}


def metadata(p) -> dict:
    for path in (p.run_dir / "run.json", p.state_json):
        if path.is_file():
            data = json.loads(path.read_text())
            if "target_ref" in data:
                return {k: data.get(k) for k in ("target_ref", "target_initial_commit")}
    return {}


def check(p, *, required: bool = False) -> None:
    expected = metadata(p)
    if not expected.get("target_ref") and not required:
        return  # legacy run: no historical branch identity to infer
    if not expected.get("target_ref"):
        raise ValueError("run has no recorded target branch (legacy or detached HEAD); explicitly migrate/reinitialize the run")
    actual = capture(p.repo_root)
    if actual["target_ref"] != expected["target_ref"]:
        raise ValueError(f"target branch changed: expected {expected['target_ref']}, got {actual['target_ref']}; restore the original branch before publishing")
    initial = expected.get("target_initial_commit")
    if initial and subprocess.run(["git", "merge-base", "--is-ancestor", initial, "HEAD"],
                                  cwd=p.repo_root, capture_output=True).returncode:
        raise ValueError("target history was rewritten since run start; reconcile before publishing")
