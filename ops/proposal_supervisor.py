#!/usr/bin/env python3
"""Host-side activation and rollback for approved Multivac proposals.

This program is intentionally not callable by the Discord bot. A systemd timer
runs it as a separate authority on the Debian host.
"""

from __future__ import annotations

import argparse
import contextlib
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
from pathlib import PurePosixPath

BASE_DIR = Path(os.environ.get("MULTIVAC_BASE_DIR", "/srv/multivac")).resolve()
RELEASES_DIR = Path(os.environ.get("MULTIVAC_RELEASES_DIR", "/srv/multivac-releases")).resolve()
TOOL_ARTIFACTS_DIR = Path(
    os.environ.get(
        "MULTIVAC_TOOL_ARTIFACTS_DIR",
        str(BASE_DIR.parent / "multivac-tool-artifacts"),
    )
).resolve()
STATE_DIR = Path(os.environ.get("MULTIVAC_STATE_DIR", str(BASE_DIR))).resolve()
TOOL_CONTROL_DIR = STATE_DIR / "tool-control"
TOOL_CONTROL_GID = int(os.environ.get("MULTIVAC_STATE_GID", "65532"))
DB_PATH = STATE_DIR / "conversation_history.db"
STATE_PATH = BASE_DIR / ".multivac-release.json"
OVERRIDE_PATH = BASE_DIR / "ops" / "docker-compose.release.yml"
IMAGE_NAME = os.environ.get("MULTIVAC_TEST_IMAGE", "multivac-multivac")
HEALTH_TIMEOUT = int(os.environ.get("MULTIVAC_HEALTH_TIMEOUT", "75"))
TOOL_ACTIVATION_TIMEOUT = int(os.environ.get("MULTIVAC_TOOL_ACTIVATION_TIMEOUT", "45"))
SIGNING_KEY_PATH = Path(os.environ.get("MULTIVAC_SIGNING_KEY", "/etc/multivac-supervisor.key"))
RELEASE_RETENTION = int(os.environ.get("MULTIVAC_RELEASE_RETENTION", "5"))
CANONICAL_BRANCH = os.environ.get("MULTIVAC_CANONICAL_BRANCH", "main")
CANONICAL_REF = f"refs/heads/{CANONICAL_BRANCH}"
CHANGE_DASHBOARD_URL = "https://sardistic.github.io/Multivac-Refactored/"


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
    TOOL_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TOOL_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    # The runtime and the unprivileged host supervisor share this directory via
    # group 65532. Setgid keeps supervisor-created request files in that group;
    # no access is granted to users outside the deployment/runtime boundary.
    if os.name != "nt" and hasattr(os, "geteuid"):
        try:
            if hasattr(os, "chown") and os.geteuid() == 0:
                os.chown(TOOL_CONTROL_DIR, -1, TOOL_CONTROL_GID)
            TOOL_CONTROL_DIR.chmod(0o2770)
        except PermissionError:
            # The isolated validation container deliberately drops CAP_CHOWN.
            # Its state root is a disposable /tmp path and never serves the bot.
            # An already-provisioned production directory is also valid when
            # the service has the required shared-group access but is not owner.
            try:
                STATE_DIR.relative_to(Path("/tmp").resolve())
            except ValueError:
                if not os.access(TOOL_CONTROL_DIR, os.R_OK | os.W_OK | os.X_OK):
                    raise
        if not os.access(TOOL_CONTROL_DIR, os.R_OK | os.W_OK | os.X_OK):
            raise PermissionError(
                f"Supervisor cannot access shared control directory: {TOOL_CONTROL_DIR}"
            )
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
        if "deployment_kind" not in columns:
            conn.execute(
                "ALTER TABLE code_deployments ADD COLUMN deployment_kind TEXT NOT NULL DEFAULT 'release'"
            )
        if "hotload_state_json" not in columns:
            conn.execute("ALTER TABLE code_deployments ADD COLUMN hotload_state_json TEXT")
        if "previous_hotload_state_json" not in columns:
            conn.execute("ALTER TABLE code_deployments ADD COLUMN previous_hotload_state_json TEXT")


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
    env["MULTIVAC_TOOL_ARTIFACTS_DIR"] = str(TOOL_ARTIFACTS_DIR)
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
            SELECT id, public_id, owner_id, request, baseline_sha, patch, status, validation_json,
                   approval_channel_id, approval_message_id
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
        "activated_at", "finished_at", "manifest_signature", "deployment_kind",
        "hotload_state_json", "previous_hotload_state_json",
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


def refresh_dashboard() -> None:
    """Publish terminal deployment state immediately; the timer remains a fallback."""
    run(
        ["systemctl", "start", "--no-block", "multivac-dashboard.service"],
        check=False,
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
    current = run(
        ["git", "rev-parse", "--verify", f"{CANONICAL_REF}^{{commit}}"],
        cwd=BASE_DIR,
    ).stdout.strip().lower()
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
    # The production control checkout may use a deployment branch while the
    # running read-only release resolves proposal baselines through the shared
    # canonical ref. Keep both views on the newly promoted commit.
    run(["git", "update-ref", CANONICAL_REF, commit_sha], cwd=BASE_DIR, timeout=60)


def validate_again(row: sqlite3.Row) -> None:
    sys.path.insert(0, str(BASE_DIR))
    from services.code_changes import validate_patch

    report = validate_patch(row["baseline_sha"], row["patch"])
    if not report.get("ok"):
        raise RuntimeError("Revalidation failed: " + "; ".join(report.get("errors", []))[:1500])


def proposal_paths(row) -> list[str]:
    raw_validation = None
    if isinstance(row, dict):
        raw_validation = row.get("validation_json")
    elif "validation_json" in row.keys():
        raw_validation = row["validation_json"]
    try:
        validation = json.loads(raw_validation or "null")
    except (TypeError, json.JSONDecodeError):
        validation = None
    if isinstance(validation, dict) and isinstance(validation.get("files"), list):
        return sorted({str(path).replace("\\", "/") for path in validation["files"]})

    patch = row.get("patch", "") if isinstance(row, dict) else row["patch"]
    paths = []
    for line in (patch or "").splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            paths.append(line.split(" b/", 1)[1].strip().replace("\\", "/"))
    return sorted(set(paths))


def is_tool_only_proposal(row) -> bool:
    paths = proposal_paths(row)
    if not paths:
        return False
    for raw in paths:
        path = PurePosixPath(raw)
        if (
            not path.parts
            or path.parts[0] != "live_tools"
            or path.suffix.lower() != ".py"
            or path.name == "__init__.py"
            or ".." in path.parts
        ):
            return False
    return True


def is_command_only_proposal(row) -> bool:
    paths = proposal_paths(row)
    if not paths:
        return False
    for raw in paths:
        path = PurePosixPath(raw)
        if (
            not path.parts
            or path.parts[0] != "live_commands"
            or path.suffix.lower() != ".py"
            or path.name == "__init__.py"
            or ".." in path.parts
        ):
            return False
    return True


def is_behavior_only_proposal(row) -> bool:
    paths = proposal_paths(row)
    if not paths:
        return False
    for raw in paths:
        path = PurePosixPath(raw)
        if (
            not path.parts
            or path.parts[0] != "live_components"
            or path.suffix.lower() != ".py"
            or path.name == "__init__.py"
            or ".." in path.parts
        ):
            return False
    return True


def hotload_kind(row) -> str | None:
    if is_tool_only_proposal(row):
        return "tool"
    if is_command_only_proposal(row):
        return "command"
    if is_behavior_only_proposal(row):
        return "behavior"
    return None


def test_release(release: Path) -> None:
    result = run(
        [
            "docker", "run", "--rm", "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "768m", "--cpus", "2", "--pids-limit", "256",
            "-e", "OPENSEARCH_ENABLED=false",
            "-e", "MULTIVAC_STATE_DIR=/tmp/multivac-test-state",
            "-e", "USAGE_DB_PATH=/tmp/multivac-test-usage.db",
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


def test_tool_modules(release: Path, paths: list[str]) -> None:
    modules = [path for path in paths if (release / path).is_file()]
    if not modules:
        return
    result = run(
        [
            "docker", "run", "--rm", "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "-e", "OPENSEARCH_ENABLED=false",
            "-e", "MULTIVAC_STATE_DIR=/tmp/multivac-tool-validation-state",
            "-e", "USAGE_DB_PATH=/tmp/multivac-tool-validation-usage.db",
            "-v", f"{release}:/app:ro", "-w", "/app",
            "--entrypoint", "python", IMAGE_NAME,
            "-m", "dev.validate_tool_modules", *modules,
        ],
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Isolated live-tool validation failed: "
            + (result.stdout + result.stderr)[-3000:]
        )


def test_command_modules(release: Path, paths: list[str]) -> None:
    modules = [path for path in paths if (release / path).is_file()]
    if not modules:
        return
    result = run(
        [
            "docker", "run", "--rm", "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "-e", "OPENSEARCH_ENABLED=false",
            "-e", "MULTIVAC_STATE_DIR=/tmp/multivac-command-validation-state",
            "-e", "USAGE_DB_PATH=/tmp/multivac-command-validation-usage.db",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{release}:/app:ro", "-w", "/app",
            "--entrypoint", "python", IMAGE_NAME,
            "-m", "dev.validate_command_modules", *modules,
        ],
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Isolated live-command validation failed: "
            + (result.stdout + result.stderr)[-3000:]
        )


def test_behavior_modules(release: Path, paths: list[str]) -> None:
    modules = [path for path in paths if (release / path).is_file()]
    if not modules:
        return
    result = run(
        [
            "docker", "run", "--rm", "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
            "-e", "OPENSEARCH_ENABLED=false",
            "-e", "MULTIVAC_STATE_DIR=/tmp/multivac-behavior-validation-state",
            "-e", "USAGE_DB_PATH=/tmp/multivac-behavior-validation-usage.db",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{release}:/app:ro", "-w", "/app",
            "--entrypoint", "python", IMAGE_NAME,
            "-m", "dev.validate_behavior_modules", *modules,
        ],
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Isolated live-behavior validation failed: "
            + (result.stdout + result.stderr)[-3000:]
        )


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


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def read_active_tools() -> dict:
    path = TOOL_CONTROL_DIR / "active-tools.json"
    if not path.is_file():
        return {"version": 1, "sources": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("sources"), dict):
        raise RuntimeError("Active tool state is invalid")
    return state


def read_active_commands() -> dict:
    path = TOOL_CONTROL_DIR / "active-commands.json"
    if not path.is_file():
        return {"version": 1, "sources": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("sources"), dict):
        raise RuntimeError("Active command state is invalid")
    return state


def read_active_behaviors() -> dict:
    path = TOOL_CONTROL_DIR / "active-behaviors.json"
    if not path.is_file():
        return {"version": 1, "sources": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("sources"), dict):
        raise RuntimeError("Active behavior state is invalid")
    return state


def read_active_hotload(kind: str) -> dict:
    if kind == "tool":
        return read_active_tools()
    if kind == "command":
        return read_active_commands()
    if kind == "behavior":
        return read_active_behaviors()
    raise ValueError(f"Unsupported hotload kind: {kind}")


def publish_hotload_artifacts(
    row, release: Path, patch_hash: str, paths: list[str], *, kind: str
) -> tuple[Path, str, list[dict], dict]:
    source_prefix = {
        "tool": "hotload",
        "command": "hotcommand",
        "behavior": "hotbehavior",
    }.get(kind)
    if source_prefix is None:
        raise ValueError(f"Unsupported hotload kind: {kind}")
    if not SIGNING_KEY_PATH.is_file():
        raise RuntimeError("Host release-signing key is missing")
    final = TOOL_ARTIFACTS_DIR / f"proposal-{row['id']}-{patch_hash[:12]}"
    temp = TOOL_ARTIFACTS_DIR / f".proposal-{row['id']}-{patch_hash[:12]}.tmp"
    if final.exists():
        raise RuntimeError(f"Tool artifact already exists: {final}")
    if temp.exists():
        resolved = temp.resolve()
        if resolved.parent != TOOL_ARTIFACTS_DIR or not resolved.name.startswith(".proposal-"):
            raise RuntimeError("Refusing to clean an unexpected tool artifact path")
        shutil.rmtree(resolved)
    temp.mkdir(parents=True)

    active_sources = read_active_hotload(kind)["sources"]
    previous: dict[str, dict] = {}
    operations: list[dict] = []
    files: list[dict] = []
    try:
        for raw in paths:
            source_id = f"{source_prefix}:{raw}"
            if source_id in active_sources:
                previous[source_id] = active_sources[source_id]
            candidate = release / raw
            if candidate.is_symlink():
                raise RuntimeError(f"Symlinked tool source is not accepted: {raw}")
            source = candidate.resolve()
            try:
                source.relative_to(release.resolve())
            except ValueError as exc:
                raise RuntimeError(f"Tool source escapes release: {raw}") from exc
            if not source.is_file():
                operations.append(
                    {"action": "unload", "source_id": source_id, "proposal_id": row["id"]}
                )
                continue
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            destination = temp / raw
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            relative_path = f"{final.name}/{raw}"
            record = {"path": raw, "sha256": digest, "bytes": len(payload)}
            files.append(record)
            operations.append(
                {
                    "action": "activate",
                    "source_id": source_id,
                    "relative_path": relative_path,
                    "sha256": digest,
                    "proposal_id": row["id"],
                }
            )

        unsigned = {
            "version": 1,
            "kind": kind,
            "proposal_id": row["id"],
            "baseline_sha": row["baseline_sha"],
            "patch_sha256": patch_hash,
            "files": files,
            "operations": operations,
        }
        payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(
            SIGNING_KEY_PATH.read_bytes(), payload, hashlib.sha256
        ).hexdigest()
        (temp / "manifest.json").write_text(
            json.dumps({**unsigned, "signature": signature}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        for file_path in temp.rglob("*"):
            if file_path.is_file():
                file_path.chmod(0o444)
        for directory in sorted(
            (path for path in temp.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
        temp.chmod(0o555)
        temp.replace(final)
        return final, signature, operations, previous
    except Exception:
        if temp.exists():
            for path in temp.rglob("*"):
                with contextlib.suppress(OSError):
                    path.chmod(0o755 if path.is_dir() else 0o644)
            temp.chmod(0o755)
            shutil.rmtree(temp)
        raise


def publish_tool_artifacts(
    row, release: Path, patch_hash: str, paths: list[str]
) -> tuple[Path, str, list[dict], dict]:
    return publish_hotload_artifacts(
        row, release, patch_hash, paths, kind="tool"
    )


def publish_command_artifacts(
    row, release: Path, patch_hash: str, paths: list[str]
) -> tuple[Path, str, list[dict], dict]:
    return publish_hotload_artifacts(
        row, release, patch_hash, paths, kind="command"
    )


def publish_behavior_artifacts(
    row, release: Path, patch_hash: str, paths: list[str]
) -> tuple[Path, str, list[dict], dict]:
    return publish_hotload_artifacts(
        row, release, patch_hash, paths, kind="behavior"
    )


def request_hotload_activation(
    proposal_id: int, operations: list[dict], *, kind: str
) -> dict:
    filenames = {
        "tool": ("request.json", "result.json"),
        "command": ("command-request.json", "command-result.json"),
        "behavior": ("behavior-request.json", "behavior-result.json"),
    }
    if kind not in filenames:
        raise ValueError(f"Unsupported hotload kind: {kind}")
    request_name, result_name = filenames[kind]
    request_id = f"{kind}-proposal-{proposal_id}-{time.time_ns()}"
    request = {
        "version": 1,
        "request_id": request_id,
        "proposal_id": proposal_id,
        "operations": operations,
        "created_at": now_iso(),
    }
    _atomic_json(TOOL_CONTROL_DIR / request_name, request)
    deadline = time.monotonic() + TOOL_ACTIVATION_TIMEOUT
    result_path = TOOL_CONTROL_DIR / result_name
    last = f"waiting for bot {kind}-control worker"
    while time.monotonic() < deadline:
        if result_path.is_file():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                last = str(exc)
            else:
                if result.get("request_id") == request_id:
                    if not result.get("ok"):
                        raise RuntimeError(
                            f"Live-{kind} activation failed: "
                            + str(result.get("detail", "unknown"))
                        )
                    return result
        time.sleep(1)
    raise RuntimeError(f"Live-{kind} activation timed out: {last}")


def request_tool_activation(proposal_id: int, operations: list[dict]) -> dict:
    return request_hotload_activation(proposal_id, operations, kind="tool")


def request_command_activation(proposal_id: int, operations: list[dict]) -> dict:
    return request_hotload_activation(proposal_id, operations, kind="command")


def request_behavior_activation(proposal_id: int, operations: list[dict]) -> dict:
    return request_hotload_activation(proposal_id, operations, kind="behavior")


def restore_operations(previous: dict, affected: list[str]) -> list[dict]:
    operations = []
    for source_id in affected:
        record = previous.get(source_id)
        if record:
            operations.append(
                {
                    "action": "activate",
                    "source_id": source_id,
                    "relative_path": record["relative_path"],
                    "sha256": record["sha256"],
                    "proposal_id": record.get("proposal_id"),
                }
            )
        else:
            operations.append({"action": "unload", "source_id": source_id})
    return operations


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


_DEPLOYMENT_STEPS = (
    "Owner approval recorded",
    "Patch revalidated against the current baseline",
    "Isolated release workspace prepared",
    "Networkless test suite passed",
    "Audited Git commit created",
    "Release manifest signed",
    "Release activated on the host",
    "Discord health check passed",
    "Canonical Git branch promoted",
    "Deployment active and healthy",
)


def edit_approval_progress(
    row: sqlite3.Row, completed: int, current: str | None = None, failure: str | None = None
) -> None:
    """Best-effort edit of the original approval response with full live progress."""
    try:
        with db_connect() as conn:
            target = conn.execute(
                "SELECT approval_channel_id, approval_message_id FROM code_proposals WHERE id=?",
                (row["id"],),
            ).fetchone()
        if not target or not target[0] or not target[1]:
            return
        token = _load_bot_token()
        if not token:
            return
        lines = [
            f"✅ Approved proposal `{proposal_name(row, row['id'])}`.",
            "",
            "**Deployment progress**",
        ]
        for index, step in enumerate(_DEPLOYMENT_STEPS):
            if failure and index == completed:
                icon = "❌"
            elif index < completed:
                icon = "✅"
            elif index == completed and current:
                icon = "⏳"
            else:
                icon = "▫️"
            lines.append(f"{icon} {step}")
        if current and not failure:
            lines.extend(("", f"Current: **{current}**"))
        if failure:
            lines.extend(("", "**Deployment failed safely; the previous state was restored.**", failure[:700]))
        lines.extend(("", f"Track public status: {CHANGE_DASHBOARD_URL}"))
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/sardistic/Multivac-Refactored, 1.0)",
        }
        request = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{target[0]}/messages/{target[1]}",
            data=json.dumps({"content": "\n".join(lines)[:1990]}).encode(),
            headers=headers,
            method="PATCH",
        )
        urllib.request.urlopen(request, timeout=10).close()
    except Exception as exc:
        print(f"progress_edit_failed: {type(exc).__name__}: {exc}", file=sys.stderr)


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
    live_kind = hotload_kind(row)
    hotload_only = live_kind is not None
    changed_paths = proposal_paths(row)
    has_live_paths = any(
        path.startswith(("live_tools/", "live_commands/", "live_components/"))
        for path in changed_paths
    )
    hotload_paths = changed_paths if hotload_only else []
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
                 previous_proposal_id, created_at, deployment_kind)
            VALUES (?, '', '', 'building', ?, ?, ?, ?)
            """,
            (
                proposal_id,
                previous_state["active_release"],
                previous_state.get("proposal_id"),
                now_iso(),
                live_kind or "release",
            ),
        )

    previous = Path(previous_state["active_release"])
    release = None
    artifact = None
    hotload_operations: list[dict] = []
    previous_hotload: dict = {}
    hotload_activated = False
    release_activation_attempted = False
    completed_steps = 1
    try:
        if has_live_paths and not hotload_only:
            raise RuntimeError(
                "Live modules must be standalone .py files in one kind-specific proposal"
            )
        edit_approval_progress(row, completed_steps, "Revalidating the approved patch")
        require_current_baseline(row)
        validate_again(row)
        completed_steps = 2
        edit_approval_progress(row, completed_steps, "Preparing an isolated release workspace")
        release, patch_hash = create_worktree(row)
        completed_steps = 3
        update_deployment(
            proposal_id, "testing", release_path=str(release), patch_sha256=patch_hash
        )
        edit_approval_progress(row, completed_steps, "Running the networkless test suite")
        test_release(release)
        if live_kind == "tool":
            test_tool_modules(release, hotload_paths)
        elif live_kind == "command":
            test_command_modules(release, hotload_paths)
        elif live_kind == "behavior":
            test_behavior_modules(release, hotload_paths)
        completed_steps = 4
        edit_approval_progress(row, completed_steps, "Creating the audited Git commit")
        restore_pristine_release(row, release)
        release_commit = commit_release(row, release)
        completed_steps = 5
        edit_approval_progress(row, completed_steps, "Signing the release manifest")
        if live_kind == "tool":
            artifact, signature, hotload_operations, previous_hotload = publish_tool_artifacts(
                row, release, patch_hash, hotload_paths
            )
        elif live_kind == "command":
            artifact, signature, hotload_operations, previous_hotload = publish_command_artifacts(
                row, release, patch_hash, hotload_paths
            )
        elif live_kind == "behavior":
            artifact, signature, hotload_operations, previous_hotload = publish_behavior_artifacts(
                row, release, patch_hash, hotload_paths
            )
        else:
            signature = sign_release(row, patch_hash, release)
        completed_steps = 6
        update_deployment(
            proposal_id,
            "activating",
            manifest_signature=signature,
            release_path=str(artifact if hotload_only else release),
        )
        if live_kind == "tool":
            edit_approval_progress(row, completed_steps, "Hotloading the approved tool artifact")
            activation = request_tool_activation(proposal_id, hotload_operations)
            hotload_activated = True
        elif live_kind == "command":
            edit_approval_progress(row, completed_steps, "Hotloading the approved command Cogs")
            activation = request_command_activation(proposal_id, hotload_operations)
            hotload_activated = True
        elif live_kind == "behavior":
            edit_approval_progress(row, completed_steps, "Hotloading the approved behavior components")
            activation = request_behavior_activation(proposal_id, hotload_operations)
            hotload_activated = True
        else:
            edit_approval_progress(row, completed_steps, "Activating the release on the host")
            release_activation_attempted = True
            activate_release(release, proposal_id)
        completed_steps = 7
        edit_approval_progress(row, completed_steps, "Checking the running Discord bot")
        if hotload_only:
            # The bot processes control requests only while Discord reports
            # ready, so its matching acknowledgement is the health signal.
            ok, health_detail = True, "Discord ready and tool control acknowledged"
        else:
            ok, health_detail = healthy()
        if not ok:
            raise RuntimeError(f"Health check failed: {health_detail}")
        detail = (
            f"Live {live_kind}s active at generation {activation['generation']}; {health_detail}"
            if hotload_only
            else health_detail
        )
        completed_steps = 8
        edit_approval_progress(row, completed_steps, "Promoting the canonical Git branch")
        promote_release(release_commit)
        completed_steps = 10
        state_fields = {}
        if hotload_only:
            affected = [operation["source_id"] for operation in hotload_operations]
            current = read_active_hotload(live_kind)["sources"]
            active_subset = {
                source_id: current[source_id]
                for source_id in affected
                if source_id in current
            }
            state_fields = {
                "hotload_state_json": json.dumps(active_subset, sort_keys=True),
                "previous_hotload_state_json": json.dumps(previous_hotload, sort_keys=True),
            }
        update_deployment(
            proposal_id,
            "active",
            detail=detail,
            activated_at=now_iso(),
            finished_at=now_iso(),
            **state_fields,
        )
        refresh_dashboard()
        edit_approval_progress(row, completed_steps)
        public_name = proposal_name(row, proposal_id)
        notify_owner(
            row["owner_id"],
            f"✅ Multivac code proposal {public_name} is active and healthy.\n{CHANGE_DASHBOARD_URL}",
        )
        prune_releases()
    except Exception as exc:
        detail = str(exc)[:3000]
        try:
            if hotload_only:
                if hotload_activated:
                    affected = [operation["source_id"] for operation in hotload_operations]
                    restore = restore_operations(previous_hotload, affected)
                    request_hotload_activation(proposal_id, restore, kind=live_kind)
            else:
                if release_activation_attempted:
                    activate_release(previous, previous_state.get("proposal_id"))
                    healthy(timeout=45)
        finally:
            update_deployment(proposal_id, "failed", detail=detail, finished_at=now_iso())
            refresh_dashboard()
            edit_approval_progress(row, completed_steps, failure=detail)
            notify_owner(
                row["owner_id"],
                f"❌ Multivac proposal {proposal_name(row, proposal_id)} failed activation and was rolled back.\n"
                f"{detail[:1200]}\n{CHANGE_DASHBOARD_URL}",
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
    refresh_dashboard()
    return int(proposal_id)


def rollback_deployment(proposal_id: int) -> int:
    with db_connect() as conn:
        deployment = conn.execute(
            """
            SELECT status, deployment_kind, hotload_state_json, previous_hotload_state_json
            FROM code_deployments WHERE proposal_id=?
            """,
            (proposal_id,),
        ).fetchone()
    if not deployment:
        raise RuntimeError("Deployment record is missing")
    if deployment["status"] != "active":
        raise RuntimeError(f"Deployment is {deployment['status']}, not active")
    deployment_kind = deployment["deployment_kind"]
    if deployment_kind not in {"tool", "command", "behavior"}:
        state = read_state()
        if state.get("proposal_id") != proposal_id:
            raise RuntimeError("Active release changed before rollback was processed")
        return rollback()

    activated = json.loads(deployment["hotload_state_json"] or "{}")
    previous = json.loads(deployment["previous_hotload_state_json"] or "{}")
    affected = sorted(set(activated).union(previous))
    current_sources = read_active_hotload(deployment_kind)["sources"]
    current_subset = {
        source_id: current_sources[source_id]
        for source_id in affected
        if source_id in current_sources
    }
    if current_subset != activated:
        raise RuntimeError(f"Active {deployment_kind} versions changed before rollback was processed")
    operations = restore_operations(previous, affected)
    if deployment_kind == "tool":
        result = request_tool_activation(proposal_id, operations)
    elif deployment_kind == "command":
        result = request_command_activation(proposal_id, operations)
    else:
        result = request_behavior_activation(proposal_id, operations)
    detail = (
        f"Previous {deployment_kind} versions restored at generation {result['generation']}"
    )
    update_deployment(
        proposal_id,
        "rolled_back",
        detail=detail,
        finished_at=now_iso(),
    )
    refresh_dashboard()
    return proposal_id


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
            active_id = rollback_deployment(int(request["proposal_id"]))
            status, detail = "completed", "Previous deployment state restored and healthy"
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
    prune_tool_artifacts()


def prune_tool_artifacts() -> None:
    keep_names: set[str] = set()

    def retain_records(records: dict) -> None:
        for record in records.values():
            relative = record.get("relative_path") if isinstance(record, dict) else None
            if isinstance(relative, str):
                parts = PurePosixPath(relative).parts
                if parts and parts[0].startswith("proposal-"):
                    keep_names.add(parts[0])

    retain_records(read_active_tools()["sources"])
    retain_records(read_active_commands()["sources"])
    retain_records(read_active_behaviors()["sources"])
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT hotload_state_json, previous_hotload_state_json
            FROM code_deployments
            WHERE deployment_kind IN ('tool', 'command', 'behavior')
            ORDER BY id DESC LIMIT ?
            """,
            (RELEASE_RETENTION,),
        ).fetchall()
    for row in rows:
        for raw in row:
            try:
                records = json.loads(raw or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(records, dict):
                retain_records(records)

    artifacts = sorted(
        TOOL_ARTIFACTS_DIR.glob("proposal-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    keep_names.update(path.name for path in artifacts[:RELEASE_RETENTION])
    for artifact in artifacts:
        resolved = artifact.resolve()
        if resolved.name in keep_names or resolved.parent != TOOL_ARTIFACTS_DIR:
            continue
        for path in resolved.rglob("*"):
            with contextlib.suppress(OSError):
                path.chmod(0o755 if path.is_dir() else 0o644)
        resolved.chmod(0o755)
        shutil.rmtree(resolved)


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
        if args.proposal_id is None:
            rollback()
        else:
            rollback_deployment(args.proposal_id)
    else:
        status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
