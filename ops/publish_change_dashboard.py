#!/usr/bin/env python3
"""Publish sanitized dashboard assets to the change-dashboard branch."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

BASE_DIR = Path("/srv/multivac")
WORKTREE = Path("/srv/multivac-dashboard")
BRANCH = "change-dashboard"


def run(*args: str, cwd: Path = BASE_DIR, check: bool = True):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=60)
    if check and result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-2000:])
    return result


def ensure_worktree() -> None:
    run("git", "fetch", "origin", BRANCH, check=False)
    if not (WORKTREE / ".git").exists():
        if WORKTREE.exists():
            shutil.rmtree(WORKTREE)
        remote = run("git", "show-ref", "--verify", f"refs/remotes/origin/{BRANCH}", check=False)
        start = f"origin/{BRANCH}" if remote.returncode == 0 else "HEAD"
        run("git", "worktree", "add", "-B", BRANCH, str(WORKTREE), start)


def main() -> int:
    ensure_worktree()
    target = WORKTREE / "dashboard"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(BASE_DIR / "dashboard", target)
    run(
        "/usr/bin/python3",
        str(BASE_DIR / "ops" / "export_change_dashboard.py"),
        "--output",
        str(target / "data" / "changes.json"),
    )
    run("git", "add", "dashboard", cwd=WORKTREE)
    changed = run("git", "diff", "--cached", "--quiet", cwd=WORKTREE, check=False)
    if changed.returncode == 0:
        return 0
    run("git", "-c", "user.name=Multivac Dashboard", "-c", "user.email=dashboard@localhost",
        "commit", "-m", "Update sanitized change dashboard", cwd=WORKTREE)
    run("git", "push", "origin", BRANCH, cwd=WORKTREE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
