#!/usr/bin/env python3
"""Publish sanitized dashboard assets to the change-dashboard branch."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

BASE_DIR = Path("/srv/multivac")
WORKTREE = Path("/srv/multivac-dashboard")
BRANCH = "change-dashboard"
DASHBOARD_DATA = "dashboard/data/changes.json"


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


def _without_timestamp(text: str) -> str | None:
    """The payload with its generation time removed, for comparing substance."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(data, dict):
        data.pop("generated_at", None)
    return json.dumps(data, sort_keys=True)


def drop_timestamp_only_rewrite() -> None:
    """Restore the committed dashboard payload when only its timestamp moved.

    The exporter stamps `generated_at` on every run, so the file always differs
    from the committed copy even when no proposal, baseline or status changed.
    The staged-diff check downstream could therefore never be false, and the
    timer committed on all 288 of its daily runs: 6,300 commits in a year, of
    which a sampled 300 out of 300 carried nothing but the timestamp line. Each
    one also pushed, and each push ran the Pages workflow.

    Comparing substance rather than bytes makes the existing check meaningful.
    """
    current_path = WORKTREE / DASHBOARD_DATA
    committed = run("git", "show", f"HEAD:{DASHBOARD_DATA}", cwd=WORKTREE, check=False)
    if committed.returncode != 0:
        return
    try:
        current = _without_timestamp(current_path.read_text(encoding="utf-8"))
    except OSError:
        return
    if current is None or current != _without_timestamp(committed.stdout):
        return
    run("git", "checkout", "HEAD", "--", DASHBOARD_DATA, cwd=WORKTREE)


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
    drop_timestamp_only_rewrite()
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
