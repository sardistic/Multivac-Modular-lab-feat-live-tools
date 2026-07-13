#!/usr/bin/env python3
"""Host-side activation and rollback for approved Multivac proposals.

This program is intentionally not callable by the Discord bot. A systemd timer
runs it as a separate authority on the Debian host.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(os.environ.get("MULTIVAC_BASE_DIR", "/srv/multivac")).resolve()
RELEASES_DIR = Path(os.environ.get("MULTIVAC_RELEASES_DIR", "/srv/multivac-releases")).resolve()
STATE_DIR = Path(os.environ.get("MULTIVAC_STATE_DIR", str(BASE_DIR))).resolve()
DB_PATH = STATE_DIR / "conversation_history.db"
STATE_PATH = BASE_DIR / ".multivac-release.json"
OVERRIDE_PATH = BASE_DIR / "ops" / "docker-compose.release.yml"
IMAGE_NAME = os.environ.get("MULTIVAC_TEST_IMAGE", "multivac-multivac")
HEALTH_TIMEOUT = int(os.environ.get("MULTIVAC_HEALTH_TIMEOUT", "75"))
SIGNING_KEY_PATH = Path(os.environ.get("MULTIVAC_SIGNING_KEY", "/etc/multivac-supervisor.key"))
RELEASE_RETENTION = int(os.environ.get("MULTIVAC_RELEASE_RETENTION", "5"))
CANONICAL_BRANCH = os.environ.get("MULTIVAC_CANONICAL_BRANCH", "main")


def proposal_name(row, fallback: int | str) -> str:
    if isinstance(row, dict):
        return str(row.get("public_id") or fallback)
    return str(row["public_id"] or fallback) if "public_id" in row.keys() else str(fallback)


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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(code_deployments)")}
        if "manifest_signature" not in columns:
            conn.execute("ALTER TABLE code_deployments ADD COLUMN manifest_signature TEXT")


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
            SELECT id, public_id, owner_id, request, baseline_sha, patch, status, validation_json
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
        "activated_at", "finished_at", "manifest_signature",
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
    run(["git", "apply", "--recount", "--whitespace=error", str(patch_path)], cwd=release)
    patch_path.unlink()
    return release, patch_hash


def require_current_baseline(row: sqlite3.Row) -> None:
    current = run(["git", "rev-parse", "HEAD"], cwd=BASE_DIR).stdout.strip().lower()
    if current != row["baseline_sha"].lower():
        raise RuntimeError(
            f"Proposal baseline {row['baseline_sha'][:12]} is stale; current baseline is {current[:12]}. "
            "Generate a new proposal against the current code."
        )


def commit_release(row: sqlite3.Row, release: Path) -> str:
    run(["git", "add", "--all"], cwd=release)
    staged = run(["git", "diff", "--cached", "--quiet"], cwd=release, check=False)
    if staged.returncode == 0:
        raise RuntimeError("Approved proposal produced no changes to commit")
    run(
        [
            "git", "-c", "user.name=Multivac Release Supervisor",
            "-c", "user.email=multivac@localhost", "commit",
            "-m", f"Apply approved proposal {proposal_name(row, row['id'])}",
        ],
        cwd=release,
    )
    return run(["git", "rev-parse", "HEAD"], cwd=release).stdout.strip()


def promote_release(commit_sha: str) -> None:
    run(
        ["git", "push", "origin", f"{commit_sha}:refs/heads/{CANONICAL_BRANCH}"],
        cwd=BASE_DIR,
        timeout=90,
    )
    run(["git", "merge", "--ff-only", commit_sha], cwd=BASE_DIR, timeout=60)


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
            # A linked worktree's .git file points back into the baseline
            # checkout. Mount only that metadata read-only so Git-based policy
            # tests can resolve and archive the recorded commit.
            "-v", f"{BASE_DIR / '.git'}:{BASE_DIR / '.git'}:ro",
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
    run(["git", "apply", "--recount", "--whitespace=error", str(patch_path)], cwd=resolved)
    patch_path.unlink()


def sign_release(row: sqlite3.Row, patch_hash: str, release: Path) -> str:
    if not SIGNING_KEY_PATH.is_file():
        raise RuntimeError("Host release-signing key is missing")
    key = SIGNING_KEY_PATH.read_bytes()
    payload = f"{row['id']}\n{row['baseline_sha']}\n{patch_hash}\n{release}\n".encode()
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    (release / ".multivac-release.json").write_text(
        json.dumps(
            {
                "proposal_id": row["id"],
                "baseline_sha": row["baseline_sha"],
                "patch_sha256": patch_hash,
                "signature": signature,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return signature


def _load_bot_token() -> str | None:
    env_path = BASE_DIR / ".env"
    if not env_path.is_file():
        return None
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("DISCORD_TOKEN="):
            return raw.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def notify_owner(owner_id: str, message: str) -> None:
    """Best-effort DM. Deployment and rollback never depend on notification."""
    try:
        token = _load_bot_token()
        if not token:
            return
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/sardistic/Multivac-Refactored, 1.0)",
        }
        request = urllib.request.Request(
            "https://discord.com/api/v10/users/@me/channels",
            data=json.dumps({"recipient_id": str(owner_id)}).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            channel_id = json.loads(response.read())["id"]
        request = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=json.dumps({"content": message[:1900]}).encode(),
            headers=headers,
            method="POST",
        )
        urllib.request.urlopen(request, timeout=10).close()
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        print(
            f"notification_failed: HTTP {exc.code} {exc.reason}: {detail}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"notification_failed: {type(exc).__name__}: {exc}", file=sys.stderr)


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
        path = STATE_DIR / filename
        if not path.exists():
            path.touch()
        # Compose mounts each persistent file beneath the read-only /app bind.
        # The mountpoint must already exist in a freshly-created Git worktree.
        mountpoint = release / filename
        if not mountpoint.exists():
            mountpoint.touch()
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
        require_current_baseline(row)
        validate_again(row)
        release, patch_hash = create_worktree(row)
        update_deployment(
            proposal_id, "testing", release_path=str(release), patch_sha256=patch_hash
        )
        test_release(release)
        restore_pristine_release(row, release)
        release_commit = commit_release(row, release)
        signature = sign_release(row, patch_hash, release)
        update_deployment(proposal_id, "activating", manifest_signature=signature)
        activate_release(release, proposal_id)
        ok, detail = healthy()
        if not ok:
            raise RuntimeError(f"Health check failed: {detail}")
        promote_release(release_commit)
        update_deployment(
            proposal_id, "active", detail=detail, activated_at=now_iso(), finished_at=now_iso()
        )
        public_name = proposal_name(row, proposal_id)
        notify_owner(row["owner_id"], f"✅ Multivac code proposal {public_name} is active and healthy.")
        prune_releases()
    except Exception as exc:
        detail = str(exc)[:3000]
        try:
            activate_release(previous, previous_state.get("proposal_id"))
            healthy(timeout=45)
        finally:
            update_deployment(proposal_id, "failed", detail=detail, finished_at=now_iso())
            notify_owner(
                row["owner_id"],
                f"❌ Multivac proposal {proposal_name(row, proposal_id)} failed activation and was rolled back.\n{detail[:1200]}",
            )
        raise


def reconcile() -> None:
    process_control_requests()
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


def rollback() -> int:
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
    return int(proposal_id)


def process_control_requests() -> None:
    with db_connect() as conn:
        requests = conn.execute(
            """
            SELECT id, proposal_id, owner_id FROM code_control_requests
            WHERE action='rollback' AND status='pending' ORDER BY id
            """
        ).fetchall()
    for request in requests:
        try:
            active_id = rollback()
            if active_id != request["proposal_id"]:
                raise RuntimeError("Active release changed before rollback was processed")
            status, detail = "completed", "Previous release restored and healthy"
            notify_owner(request["owner_id"], f"↩️ Multivac proposal #{active_id} was rolled back successfully.")
        except Exception as exc:
            status, detail = "failed", str(exc)[:2000]
            notify_owner(request["owner_id"], f"❌ Rollback request failed: {detail[:1200]}")
        with db_connect() as conn:
            conn.execute(
                """
                UPDATE code_control_requests SET status=?, detail=?, finished_at=? WHERE id=?
                """,
                (status, detail, now_iso(), request["id"]),
            )


def prune_releases() -> None:
    state = read_state()
    active = Path(state["active_release"]).resolve()
    with db_connect() as conn:
        keep_rows = conn.execute(
            """
            SELECT release_path FROM code_deployments
            WHERE status IN ('active', 'rolled_back') ORDER BY id DESC LIMIT ?
            """,
            (RELEASE_RETENTION,),
        ).fetchall()
    keep = {active, *(Path(row[0]).resolve() for row in keep_rows if row[0])}
    for release in RELEASES_DIR.glob("proposal-*"):
        resolved = release.resolve()
        if resolved in keep or resolved.parent != RELEASES_DIR:
            continue
        run(["git", "worktree", "remove", "--force", str(resolved)], check=False)
    run(["git", "worktree", "prune"], check=False)


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
