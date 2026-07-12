#!/usr/bin/env python3
"""Host-side activation and rollback for approved Multivac proposals.

This program is intentionally not callable by the Discord bot. A systemd timer
runs it as a separate authority on the Debian host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("MULTIVAC_BASE_DIR", "/srv/multivac")).resolve()
RELEASES_DIR = Path(os.environ.get("MULTIVAC_RELEASES_DIR", "/srv/multivac-releases")).resolve()
DB_PATH = BASE_DIR / "conversation_history.db"
STATE_PATH = BASE_DIR / ".multivac-release.json"
OVERRIDE_PATH = BASE_DIR / "ops" / "docker-compose.release.yml"
IMAGE_NAME = os.environ.get("MULTIVAC_TEST_IMAGE", "multivac-multivac")
HEALTH_TIMEOUT = int(os.environ.get("MULTIVAC_HEALTH_TIMEOUT", "75"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(args: list[str], *, cwd: Path = BASE_DIR, timeout: int = 180, check: bool = True):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise RuntimeError(f"{args[0]} failed: {detail[-2000:]}")
    return result


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def initialize() -> None:
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS code_deployments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id INTEGER NOT NULL,
                release_path TEXT NOT NULL,
                patch_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'building', 'testing', 'activating', 'active',
                    'failed', 'rolled_back'
                )),
                previous_release TEXT,
                previous_proposal_id INTEGER,
                created_at TEXT NOT NULL,
                activated_at TEXT,
                finished_at TEXT,
                detail TEXT,
                UNIQUE(proposal_id)
            );
            """
        )


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"active_release": str(BASE_DIR), "proposal_id": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def write_state(release: Path, proposal_id: int | None) -> None:
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(
        json.dumps(
            {"active_release": str(release), "proposal_id": proposal_id, "updated_at": now_iso()},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temp.replace(STATE_PATH)


def compose_args(release: Path, *args: str) -> list[str]:
    return [
        "docker", "compose",
        "-f", str(BASE_DIR / "docker-compose.yml"),
        "-f", str(OVERRIDE_PATH),
        *args,
    ]


def compose_env(release: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["MULTIVAC_APP_DIR"] = str(release)
    return env


def run_compose(release: Path, *args: str, timeout: int = 180):
    result = subprocess.run(
        compose_args(release, *args),
        cwd=BASE_DIR,
        env=compose_env(release),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "docker compose failed")[-3000:])
    return result


def proposal(proposal_id: int) -> sqlite3.Row:
    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT id, owner_id, request, baseline_sha, patch, status, validation_json
            FROM code_proposals WHERE id=?
            """,
            (proposal_id,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"Proposal #{proposal_id} does not exist")
    if row["status"] != "approved":
        raise RuntimeError(f"Proposal #{proposal_id} is {row['status']}, not approved")
    if not row["patch"]:
        raise RuntimeError(f"Proposal #{proposal_id} has no patch")
    validation = json.loads(row["validation_json"] or "null")
    if not validation or not validation.get("ok"):
        raise RuntimeError(f"Proposal #{proposal_id} has no successful validation")
    return row


def update_deployment(proposal_id: int, status: str, *, detail: str | None = None, **fields) -> None:
    allowed = {
        "release_path", "patch_sha256", "previous_release", "previous_proposal_id",
        "activated_at", "finished_at",
    }
    assignments = ["status=?", "detail=?"]
    values: list[object] = [status, detail]
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(key)
        assignments.append(f"{key}=?")
        values.append(value)
    values.append(proposal_id)
    with db_connect() as conn:
        conn.execute(
            f"UPDATE code_deployments SET {', '.join(assignments)} WHERE proposal_id=?",
            values,
        )


def create_worktree(row: sqlite3.Row) -> tuple[Path, str]:
    patch = row["patch"]
    patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    release = RELEASES_DIR / f"proposal-{row['id']}-{patch_hash[:12]}"
    if release.exists():
        run(["git", "worktree", "remove", "--force", str(release)], check=False)
        if release.exists():
            shutil.rmtree(release)
        run(["git", "worktree", "prune"], check=False)
    run(["git", "worktree", "add", "--detach", str(release), row["baseline_sha"]], timeout=60)
    patch_path = release / ".proposal.patch"
    patch_path.write_bytes(patch.replace("\r\n", "\n").encode("utf-8"))
    run(["git", "apply", "--whitespace=error", str(patch_path)], cwd=release)
    patch_path.unlink()
    return release, patch_hash


def validate_again(row: sqlite3.Row) -> None:
    sys.path.insert(0, str(BASE_DIR))
    from services.code_changes import validate_patch

    report = validate_patch(row["baseline_sha"], row["patch"])
    if not report.get("ok"):
        raise RuntimeError("Revalidation failed: " + "; ".join(report.get("errors", []))[:1500])


def test_release(release: Path) -> None:
    result = run(
        [
            "docker", "run", "--rm", "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "768m", "--cpus", "2", "--pids-limit", "256",
            "-e", "OPENSEARCH_ENABLED=false",
            "-v", f"{release}:/app", "-w", "/app",
            "--entrypoint", "python", IMAGE_NAME,
            "-m", "unittest", "discover", "-s", "tests",
        ],
        timeout=240,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Isolated tests failed: " + (result.stdout + result.stderr)[-3000:])


def restore_pristine_release(row: sqlite3.Row, release: Path) -> None:
    resolved = release.resolve()
    if resolved.parent != RELEASES_DIR or not resolved.name.startswith(f"proposal-{row['id']}-"):
        raise RuntimeError("Refusing to reset an unexpected release path")
    run(["git", "reset", "--hard", row["baseline_sha"]], cwd=resolved)
    run(["git", "clean", "-fdx"], cwd=resolved)
    patch_path = resolved / ".proposal.patch"
    patch_path.write_bytes(row["patch"].replace("\r\n", "\n").encode("utf-8"))
    run(["git", "apply", "--whitespace=error", str(patch_path)], cwd=resolved)
    patch_path.unlink()


def healthy(timeout: int = HEALTH_TIMEOUT, *, log_since: str = "2m") -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = "container not ready"
    while time.monotonic() < deadline:
        inspect = run(
            ["docker", "inspect", "multivac-multivac-1", "--format", "{{.State.Running}} {{.RestartCount}}"],
            check=False,
        )
        if inspect.returncode == 0:
            last = inspect.stdout.strip()
            if last.startswith("true "):
                logs = run_compose(
                    Path(read_state()["active_release"]),
                    "logs", "--since", log_since, "--no-color", "multivac",
                )
                if "Bot is online and ready!" in logs.stdout and "Synced" in logs.stdout:
                    return True, "Discord ready and commands synced"
        time.sleep(3)
    return False, last


def activate_release(release: Path, proposal_id: int | None) -> None:
    for filename in ("conversation_history.db", "user_locations.db", "usage_costs.db", "bot.log"):
        path = BASE_DIR / filename
        if not path.exists():
            path.touch()
    write_state(release, proposal_id)
    run_compose(release, "up", "-d", "--no-deps", "--force-recreate", "multivac", timeout=180)


def deploy(proposal_id: int) -> None:
    row = proposal(proposal_id)
    previous_state = read_state()
    with db_connect() as conn:
        prior = conn.execute(
            "SELECT status FROM code_deployments WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        if prior and prior[0] == "active":
            return
        if prior:
            raise RuntimeError(f"Proposal #{proposal_id} already has deployment state {prior[0]}")
        conn.execute(
            """
            INSERT INTO code_deployments
                (proposal_id, release_path, patch_sha256, status, previous_release,
                 previous_proposal_id, created_at)
            VALUES (?, '', '', 'building', ?, ?, ?)
            """,
            (
                proposal_id,
                previous_state["active_release"],
                previous_state.get("proposal_id"),
                now_iso(),
            ),
        )

    previous = Path(previous_state["active_release"])
    release = None
    try:
        validate_again(row)
        release, patch_hash = create_worktree(row)
        update_deployment(
            proposal_id, "testing", release_path=str(release), patch_sha256=patch_hash
        )
        test_release(release)
        restore_pristine_release(row, release)
        update_deployment(proposal_id, "activating")
        activate_release(release, proposal_id)
        ok, detail = healthy()
        if not ok:
            raise RuntimeError(f"Health check failed: {detail}")
        update_deployment(
            proposal_id, "active", detail=detail, activated_at=now_iso(), finished_at=now_iso()
        )
    except Exception as exc:
        detail = str(exc)[:3000]
        try:
            activate_release(previous, previous_state.get("proposal_id"))
            healthy(timeout=45)
        finally:
            update_deployment(proposal_id, "failed", detail=detail, finished_at=now_iso())
        raise


def reconcile() -> None:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT p.id FROM code_proposals p
            LEFT JOIN code_deployments d ON d.proposal_id=p.id
            WHERE p.status='approved' AND d.id IS NULL ORDER BY p.id
            """
        ).fetchall()
    for row in rows:
        deploy(int(row[0]))


def rollback() -> None:
    state = read_state()
    proposal_id = state.get("proposal_id")
    if proposal_id is None:
        raise RuntimeError("Baseline is already active")
    with db_connect() as conn:
        deployment = conn.execute(
            "SELECT previous_release, previous_proposal_id FROM code_deployments WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    if not deployment:
        raise RuntimeError("Active deployment record is missing")
    previous = Path(deployment[0])
    activate_release(previous, deployment[1])
    ok, detail = healthy()
    if not ok:
        raise RuntimeError(f"Rollback health check failed: {detail}")
    update_deployment(proposal_id, "rolled_back", detail=detail, finished_at=now_iso())


def status() -> None:
    state = read_state()
    ok, detail = healthy(timeout=6, log_since="24h")
    print(json.dumps({"state": state, "healthy": ok, "detail": detail}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("reconcile", "deploy", "rollback", "status"))
    parser.add_argument("proposal_id", nargs="?", type=int)
    args = parser.parse_args()
    initialize()
    if args.action == "reconcile":
        reconcile()
    elif args.action == "deploy":
        if args.proposal_id is None:
            parser.error("deploy requires proposal_id")
        deploy(args.proposal_id)
    elif args.action == "rollback":
        rollback()
    else:
        status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
