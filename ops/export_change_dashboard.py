#!/usr/bin/env python3
"""Export a public, sanitized snapshot of Multivac's change audit database."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path("/srv/multivac")
STATE_DIR = Path("/srv/multivac/state")

_REDACTIONS = (
    (re.compile(r"<@!?\d+>|<@&\d+>"), "@user"),
    (re.compile(r"https?://\S+", re.I), "[link]"),
    (re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[email]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[address]"),
    (re.compile(r"(?:[A-Za-z]:\\|/(?:home|srv|root|etc|var|opt)/)\S+"), "[path]"),
    (re.compile(r"\b(?:sk|ghp|ghu|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b", re.I), "[redacted]"),
    (re.compile(r"(?i)\b(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+"), "[redacted]"),
)


def sanitize_summary(text: str, limit: int = 220) -> str:
    clean = " ".join((text or "").split())
    for pattern, replacement in _REDACTIONS:
        clean = pattern.sub(replacement, clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) > limit:
        clean = clean[: limit - 1].rstrip() + "…"
    return clean or "Change proposal"


def safe_files(validation_json: str | None) -> list[str]:
    try:
        values = json.loads(validation_json or "{}").get("files", [])
    except (ValueError, TypeError):
        return []
    result = []
    for value in values:
        path = str(value).replace("\\", "/")
        if re.fullmatch(r"[A-Za-z0-9_.\-/]{1,180}", path) and ".." not in path.split("/"):
            result.append(path)
    return sorted(set(result))[:20]


def coarse_result(status: str | None, detail: str | None) -> str | None:
    if not status:
        return None
    if status == "active":
        return "Healthy"
    if status == "rolled_back":
        return "Rolled back"
    if status == "failed":
        return "Failed safely"
    return status.replace("_", " ").title()


def export_snapshot(db_path: Path, base_dir: Path = BASE_DIR) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    proposal_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(code_proposals)")
    }
    public_id_field = "p.public_id" if "public_id" in proposal_columns else "NULL AS public_id"
    deployment_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_deployments'"
    ).fetchone()
    deployment_join = (
        "LEFT JOIN code_deployments d ON d.proposal_id=p.id" if deployment_table else ""
    )
    deployment_fields = (
        "d.status AS deployment_status, d.patch_sha256, d.activated_at, d.finished_at, d.detail"
        if deployment_table else
        "NULL AS deployment_status, NULL AS patch_sha256, NULL AS activated_at, NULL AS finished_at, NULL AS detail"
    )
    rows = conn.execute(
        f"""
        SELECT p.id, {public_id_field}, p.request, p.baseline_sha, p.status, p.validation_json,
               p.created_at, p.updated_at, p.reviewed_at, {deployment_fields}
        FROM code_proposals p {deployment_join}
        ORDER BY p.id DESC
        """
    ).fetchall()
    proposals = []
    for row in rows:
        proposals.append(
            {
                "id": row["public_id"] or f"legacy-{row['id']}",
                "summary": sanitize_summary(row["request"]),
                "status": row["status"],
                "files": safe_files(row["validation_json"]),
                "baseline": (row["baseline_sha"] or "")[:12],
                "patch": (row["patch_sha256"] or "")[:12] or None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "reviewed_at": row["reviewed_at"],
                "deployment_status": row["deployment_status"],
                "deployment_result": coarse_result(row["deployment_status"], row["detail"]),
                "activated_at": row["activated_at"],
                "finished_at": row["finished_at"],
            }
        )
    conn.close()
    head = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=base_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline": head.stdout.strip() if head.returncode == 0 else "unknown",
        "proposals": proposals,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=STATE_DIR / "conversation_history.db")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = export_snapshot(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(".tmp")
    temp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
