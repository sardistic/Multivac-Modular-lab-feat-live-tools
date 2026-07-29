"""Persistent state for Multivac's bounded, owner-reviewed reflection loop.

Surrounding chat is fetched ephemerally from Discord (or from already-indexed,
strictly scoped evidence). This store keeps only invocation windows, short
derived observations, proposal candidates, and budget reservations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).replace(microsecond=0).isoformat()


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


class ReflectionStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        root = base_dir or Path(
            os.environ.get("MULTIVAC_STATE_DIR", Path(__file__).resolve().parent.parent)
        )
        root.mkdir(parents=True, exist_ok=True)
        self.path = Path(os.environ.get("REFLECTION_DB_PATH", root / "reflection_state.db"))
        self._lock = RLock()
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reflection_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reflection_preferences (
                    user_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reflection_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    message_ids_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','processing','complete','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_reflection_sessions_due
                    ON reflection_sessions(status, expires_at);
                CREATE TABLE IF NOT EXISTS reflection_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    actor_hashes_json TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    session_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new'
                        CHECK(status IN ('new','planned','dismissed')),
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reflection_insights_status
                    ON reflection_insights(status, last_seen_at);
                CREATE TABLE IF NOT EXISTS reflection_insight_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    insight_id INTEGER NOT NULL
                        REFERENCES reflection_insights(id) ON DELETE CASCADE,
                    seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reflection_insight_events_recent
                    ON reflection_insight_events(insight_id, seen_at);
                CREATE TABLE IF NOT EXISTS reflection_ideas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    problem TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    expected_impact TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    hotload_kind TEXT NOT NULL,
                    code_paths_json TEXT NOT NULL,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','proposed','dismissed','superseded')),
                    code_proposal_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reflection_idea_evidence (
                    idea_id INTEGER NOT NULL REFERENCES reflection_ideas(id) ON DELETE CASCADE,
                    insight_id INTEGER NOT NULL REFERENCES reflection_insights(id) ON DELETE CASCADE,
                    PRIMARY KEY (idea_id, insight_id)
                );
                CREATE TABLE IF NOT EXISTS reflection_budget (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    estimated_usd REAL NOT NULL,
                    actual_usd REAL,
                    status TEXT NOT NULL
                        CHECK(status IN ('reserved','settled','released')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reflection_budget_day
                    ON reflection_budget(day, status);
                CREATE TABLE IF NOT EXISTS reflection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    model TEXT,
                    detail TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reflection_runs_stage
                    ON reflection_runs(stage, finished_at);
                """
            )
            if conn.execute(
                "SELECT 1 FROM reflection_meta WHERE key='actor_salt'"
            ).fetchone() is None:
                conn.execute(
                    "INSERT INTO reflection_meta(key,value) VALUES('actor_salt',?)",
                    (secrets.token_hex(32),),
                )

    def _actor_hash(self, user_id: str, conn: sqlite3.Connection | None = None) -> str:
        if conn is None:
            with self.connect() as owned:
                return self._actor_hash(user_id, owned)
        row = conn.execute(
            "SELECT value FROM reflection_meta WHERE key='actor_salt'"
        ).fetchone()
        salt = bytes.fromhex(row[0])
        return hmac.new(salt, str(user_id).encode(), hashlib.sha256).hexdigest()[:24]

    def set_user_enabled(self, user_id: str, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reflection_preferences(user_id,enabled,updated_at)
                VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled=excluded.enabled, updated_at=excluded.updated_at
                """,
                (str(user_id), int(enabled), _iso()),
            )

    def user_enabled(self, user_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM reflection_preferences WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
        return bool(row and row[0])

    def forget_user(self, user_id: str) -> None:
        """Disable reflection and remove derived state involving this user."""
        with self.connect() as conn:
            actor_hash = self._actor_hash(str(user_id), conn)
            insight_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM reflection_insights WHERE actor_hashes_json LIKE ?",
                    (f'%"{actor_hash}"%',),
                )
            ]
            if insight_ids:
                marks = ",".join("?" for _ in insight_ids)
                conn.execute(f"DELETE FROM reflection_insights WHERE id IN ({marks})", insight_ids)
                conn.execute(
                    """
                    UPDATE reflection_ideas
                    SET evidence_count=(
                        SELECT COUNT(*) FROM reflection_idea_evidence evidence
                        WHERE evidence.idea_id=reflection_ideas.id
                    ), updated_at=?
                    """,
                    (_iso(),),
                )
                conn.execute(
                    """
                    UPDATE reflection_ideas SET status='superseded',updated_at=?
                    WHERE status='active' AND evidence_count=0
                    """,
                    (_iso(),),
                )
            conn.execute("DELETE FROM reflection_sessions WHERE user_id=?", (str(user_id),))
            conn.execute(
                """
                INSERT INTO reflection_preferences(user_id,enabled,updated_at)
                VALUES(?,0,?)
                ON CONFLICT(user_id) DO UPDATE SET enabled=0,updated_at=excluded.updated_at
                """,
                (str(user_id), _iso()),
            )

    def record_invocation(
        self,
        *,
        guild_id: str,
        channel_id: str,
        user_id: str,
        message_id: str,
        idle_minutes: int,
        lookback_minutes: int = 10,
        at: datetime | None = None,
    ) -> int:
        current = at or _now()
        observation_start = current - timedelta(minutes=max(0, int(lookback_minutes)))
        expires = current + timedelta(minutes=max(1, int(idle_minutes)))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id,message_ids_json FROM reflection_sessions
                WHERE guild_id=? AND channel_id=? AND user_id=?
                  AND status='pending' AND attempts=0 AND expires_at>?
                ORDER BY id DESC LIMIT 1
                """,
                (str(guild_id), str(channel_id), str(user_id), _iso(current)),
            ).fetchone()
            if row:
                message_ids = _json_list(row["message_ids_json"])
                if str(message_id) not in message_ids:
                    message_ids.append(str(message_id))
                conn.execute(
                    """
                    UPDATE reflection_sessions
                    SET message_ids_json=?,last_activity_at=?,expires_at=? WHERE id=?
                    """,
                    (json.dumps(message_ids), _iso(current), _iso(expires), row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                """
                INSERT INTO reflection_sessions(
                    guild_id,channel_id,user_id,message_ids_json,started_at,
                    last_activity_at,expires_at,status
                ) VALUES(?,?,?,?,?,?,?,'pending')
                """,
                (
                    str(guild_id), str(channel_id), str(user_id),
                    json.dumps([str(message_id)]), _iso(observation_start), _iso(current), _iso(expires),
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _session(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["message_ids"] = _json_list(item.pop("message_ids_json", None))
        return item

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM reflection_sessions WHERE id=?",
                (int(session_id),),
            ).fetchone()
        return self._session(row) if row else None

    def record_channel_activity(
        self,
        *,
        guild_id: str,
        channel_id: str,
        message_id: str,
        idle_minutes: int,
        at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Extend every active session in a channel and attach one evidence ID."""
        current = at or _now()
        expires = current + timedelta(minutes=max(1, int(idle_minutes)))
        updated_ids: list[int] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM reflection_sessions
                WHERE guild_id=? AND channel_id=? AND status='pending'
                  AND attempts=0 AND expires_at>?
                ORDER BY id
                """,
                (str(guild_id), str(channel_id), _iso(current)),
            ).fetchall()
            for row in rows:
                message_ids = _json_list(row["message_ids_json"])
                if str(message_id) not in message_ids:
                    message_ids.append(str(message_id))
                conn.execute(
                    """
                    UPDATE reflection_sessions
                    SET message_ids_json=?,last_activity_at=?,expires_at=?
                    WHERE id=? AND status='pending'
                    """,
                    (
                        json.dumps(message_ids[-200:]),
                        _iso(current),
                        _iso(expires),
                        int(row["id"]),
                    ),
                )
                updated_ids.append(int(row["id"]))
            if not updated_ids:
                return []
            placeholders = ",".join("?" for _ in updated_ids)
            updated = conn.execute(
                f"SELECT * FROM reflection_sessions WHERE id IN ({placeholders}) ORDER BY id",
                updated_ids,
            ).fetchall()
        return [self._session(row) for row in updated]

    def due_sessions(self, *, limit: int = 8, now: datetime | None = None) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM reflection_sessions
                WHERE status='pending' AND expires_at<=?
                ORDER BY expires_at LIMIT ?
                """,
                (_iso(now), max(1, int(limit))),
            ).fetchall()
        return [self._session(row) for row in rows]

    def claim_session(self, session_id: int) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE reflection_sessions SET status='processing' WHERE id=? AND status='pending'",
                (int(session_id),),
            )
            return cur.rowcount == 1

    def finish_session(self, session_id: int, *, status: str, detail: str = "") -> None:
        if status not in {"complete", "failed"}:
            raise ValueError("invalid terminal reflection session status")
        with self.connect() as conn:
            conn.execute(
                "UPDATE reflection_sessions SET status=?,detail=? WHERE id=?",
                (status, detail[:1000], int(session_id)),
            )

    def retry_session(self, session_id: int, detail: str, *, delay_minutes: int = 15) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM reflection_sessions WHERE id=?", (int(session_id),)
            ).fetchone()
            attempts = int(row[0] if row else 0) + 1
            status = "failed" if attempts >= 3 else "pending"
            conn.execute(
                """
                UPDATE reflection_sessions
                SET status=?,attempts=?,detail=?,expires_at=? WHERE id=?
                """,
                (
                    status, attempts, detail[:1000],
                    _iso(_now() + timedelta(minutes=max(1, delay_minutes))), int(session_id),
                ),
            )

    def add_insight(
        self,
        *,
        session: dict,
        kind: str,
        summary: str,
        confidence: float,
        evidence_ids: list[str],
        require_user_enabled: bool = False,
    ) -> int | None:
        normalized = " ".join(summary.lower().split())
        fingerprint = hashlib.sha256(f"{kind}:{normalized}".encode()).hexdigest()
        now = _iso()
        with self.connect() as conn:
            if require_user_enabled:
                preference = conn.execute(
                    "SELECT enabled FROM reflection_preferences WHERE user_id=?",
                    (str(session["user_id"]),),
                ).fetchone()
                if not preference or not bool(preference[0]):
                    return None
            actor_hash = self._actor_hash(session["user_id"], conn)
            row = conn.execute(
                "SELECT * FROM reflection_insights WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if row:
                actors = _json_list(row["actor_hashes_json"])
                evidence = _json_list(row["evidence_ids_json"])
                sessions = _json_list(row["session_ids_json"])
                if actor_hash not in actors:
                    actors.append(actor_hash)
                for value in evidence_ids:
                    if str(value) not in evidence:
                        evidence.append(str(value))
                if int(session["id"]) not in sessions:
                    sessions.append(int(session["id"]))
                conn.execute(
                    """
                    UPDATE reflection_insights SET occurrences=occurrences+1,
                        confidence=?,actor_hashes_json=?,evidence_ids_json=?,
                        session_ids_json=?,status='new',last_seen_at=? WHERE id=?
                    """,
                    (
                        max(float(confidence), float(row["confidence"])),
                        json.dumps(actors[-50:]), json.dumps(evidence[-40:]),
                        json.dumps(sessions[-40:]), now, row["id"],
                    ),
                )
                insight_id = int(row["id"])
                conn.execute(
                    "INSERT INTO reflection_insight_events(insight_id,seen_at) VALUES(?,?)",
                    (insight_id, now),
                )
                return insight_id
            cur = conn.execute(
                """
                INSERT INTO reflection_insights(
                    fingerprint,kind,summary,confidence,actor_hashes_json,
                    evidence_ids_json,session_ids_json,created_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    fingerprint, kind, summary[:1200], max(0.0, min(1.0, float(confidence))),
                    json.dumps([actor_hash]), json.dumps([str(v) for v in evidence_ids][-40:]),
                    json.dumps([int(session["id"])]), now, now,
                ),
            )
            insight_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO reflection_insight_events(insight_id,seen_at) VALUES(?,?)",
                (insight_id, now),
            )
            return insight_id

    def record_runtime_error(
        self,
        *,
        component: str,
        error_type: str,
        summary: str,
        evidence_id: str | None = None,
    ) -> int:
        """Count a sanitized error as a system observation without user identity."""
        normalized = re.sub(r"\b\d+\b", "#", " ".join(summary.lower().split()))
        fingerprint = hashlib.sha256(
            f"runtime_error:{component}:{error_type}:{normalized}".encode()
        ).hexdigest()
        now = _iso()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id,evidence_ids_json FROM reflection_insights WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            evidence = _json_list(row["evidence_ids_json"]) if row else []
            if evidence_id and str(evidence_id) not in evidence:
                evidence.append(str(evidence_id))
            safe_summary = f"{component} {error_type}: {summary}"[:1200]
            if row:
                conn.execute(
                    """
                    UPDATE reflection_insights SET occurrences=occurrences+1,
                        evidence_ids_json=?,status='new',last_seen_at=? WHERE id=?
                    """,
                    (json.dumps(evidence[-40:]), now, row["id"]),
                )
                insight_id = int(row["id"])
                conn.execute(
                    "INSERT INTO reflection_insight_events(insight_id,seen_at) VALUES(?,?)",
                    (insight_id, now),
                )
                return insight_id
            cur = conn.execute(
                """
                INSERT INTO reflection_insights(
                    fingerprint,kind,summary,confidence,actor_hashes_json,
                    evidence_ids_json,session_ids_json,created_at,last_seen_at
                ) VALUES(?,'runtime_error',?,1.0,'[]',?,'[]',?,?)
                """,
                (fingerprint, safe_summary, json.dumps(evidence[-40:]), now, now),
            )
            insight_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO reflection_insight_events(insight_id,seen_at) VALUES(?,?)",
                (insight_id, now),
            )
            return insight_id

    @staticmethod
    def _insight(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["actor_hashes"] = _json_list(item.pop("actor_hashes_json", None))
        item["evidence_ids"] = _json_list(item.pop("evidence_ids_json", None))
        item["session_ids"] = _json_list(item.pop("session_ids_json", None))
        item["actor_count"] = len(item["actor_hashes"])
        return item

    def pending_insights(self, limit: int = 40, *, recent_days: int = 7) -> list[dict]:
        recent_since = _iso(_now() - timedelta(days=max(1, int(recent_days))))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT insights.*,
                    (SELECT COUNT(*) FROM reflection_insight_events events
                     WHERE events.insight_id=insights.id AND events.seen_at>=?)
                    AS recent_occurrences
                FROM reflection_insights insights WHERE status='new'
                ORDER BY recent_occurrences DESC,occurrences DESC,
                         confidence DESC,last_seen_at DESC LIMIT ?
                """,
                (recent_since, max(1, int(limit))),
            ).fetchall()
        return [self._insight(row) for row in rows]

    def recent_insights(self, limit: int = 10, *, recent_days: int = 7) -> list[dict]:
        """Return structured observations without joining raw message content."""
        recent_since = _iso(_now() - timedelta(days=max(1, int(recent_days))))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT insights.*,
                    (SELECT COUNT(*) FROM reflection_insight_events events
                     WHERE events.insight_id=insights.id AND events.seen_at>=?)
                    AS recent_occurrences
                FROM reflection_insights insights
                ORDER BY last_seen_at DESC, id DESC LIMIT ?
                """,
                (recent_since, max(1, int(limit))),
            ).fetchall()
        return [self._insight(row) for row in rows]

    def recent_user_signals(
        self,
        user_id: str,
        *,
        limit: int = 4,
        recent_days: int = 7,
        min_confidence: float = 0.55,
    ) -> list[dict[str, Any]]:
        """Return bounded derived interaction signals for one consented user.

        This deliberately excludes evidence IDs, session IDs, actor hashes,
        runtime errors, dismissed observations, and signals belonging only to
        other users. It never joins raw message content.
        """
        recent_since = _iso(_now() - timedelta(days=max(1, int(recent_days))))
        confidence_floor = max(0.0, min(1.0, float(min_confidence)))
        with self.connect() as conn:
            preference = conn.execute(
                "SELECT enabled FROM reflection_preferences WHERE user_id=?",
                (str(user_id),),
            ).fetchone()
            if not preference or not bool(preference[0]):
                return []
            actor_hash = self._actor_hash(str(user_id), conn)
            rows = conn.execute(
                """
                SELECT kind,summary,confidence,occurrences,last_seen_at
                FROM reflection_insights
                WHERE status IN ('new','planned')
                  AND kind IN ('pain_point','behavior_pattern','feature_request','success')
                  AND confidence>=?
                  AND last_seen_at>=?
                  AND actor_hashes_json LIKE ?
                ORDER BY last_seen_at DESC,confidence DESC,occurrences DESC,id DESC
                LIMIT ?
                """,
                (
                    confidence_floor,
                    recent_since,
                    f'%"{actor_hash}"%',
                    max(1, min(8, int(limit))),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_idea(self, idea: dict, insight_ids: list[int]) -> int:
        fingerprint = hashlib.sha256(
            " ".join(f"{idea.get('title','')} {idea.get('proposal','')}".lower().split()).encode()
        ).hexdigest()
        now = _iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM reflection_ideas WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing:
                idea_id = int(existing[0])
                conn.execute(
                    """
                    UPDATE reflection_ideas SET problem=?,proposal=?,expected_impact=?,risk=?,
                        hotload_kind=?,code_paths_json=?,status='active',updated_at=? WHERE id=?
                    """,
                    (
                        str(idea.get("problem", ""))[:2000], str(idea.get("proposal", ""))[:4000],
                        str(idea.get("expected_impact", ""))[:2000], str(idea.get("risk", ""))[:2000],
                        str(idea.get("hotload_kind", "release"))[:32],
                        json.dumps(list(idea.get("code_paths") or [])[:14]), now, idea_id,
                    ),
                )
            else:
                cur = conn.execute(
                    """
                    INSERT INTO reflection_ideas(
                        fingerprint,title,problem,proposal,expected_impact,risk,
                        hotload_kind,code_paths_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fingerprint, str(idea.get("title", "Untitled improvement"))[:200],
                        str(idea.get("problem", ""))[:2000], str(idea.get("proposal", ""))[:4000],
                        str(idea.get("expected_impact", ""))[:2000], str(idea.get("risk", ""))[:2000],
                        str(idea.get("hotload_kind", "release"))[:32],
                        json.dumps(list(idea.get("code_paths") or [])[:14]), now, now,
                    ),
                )
                idea_id = int(cur.lastrowid)
            for insight_id in {int(value) for value in insight_ids}:
                conn.execute(
                    "INSERT OR IGNORE INTO reflection_idea_evidence(idea_id,insight_id) VALUES(?,?)",
                    (idea_id, insight_id),
                )
                conn.execute(
                    "UPDATE reflection_insights SET status='planned' WHERE id=?",
                    (insight_id,),
                )
            count = conn.execute(
                "SELECT COUNT(*) FROM reflection_idea_evidence WHERE idea_id=?", (idea_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE reflection_ideas SET evidence_count=? WHERE id=?", (int(count), idea_id)
            )
            return idea_id

    @staticmethod
    def _idea(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["code_paths"] = _json_list(item.pop("code_paths_json", None))
        return item

    def list_ideas(self, limit: int = 10, *, active_only: bool = True) -> list[dict]:
        where = "WHERE status='active'" if active_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM reflection_ideas {where}
                ORDER BY evidence_count DESC,updated_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [self._idea(row) for row in rows]

    def get_idea(self, idea_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM reflection_ideas WHERE id=?", (int(idea_id),)
            ).fetchone()
        return self._idea(row) if row else None

    def mark_idea_proposed(self, idea_id: int, proposal_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reflection_ideas SET status='proposed',code_proposal_id=?,updated_at=?
                WHERE id=? AND status='active'
                """,
                (int(proposal_id), _iso(), int(idea_id)),
            )

    def supersede_ideas(self, idea_ids: list[int]) -> None:
        if not idea_ids:
            return
        with self.connect() as conn:
            marks = ",".join("?" for _ in idea_ids)
            conn.execute(
                f"UPDATE reflection_ideas SET status='superseded',updated_at=? WHERE id IN ({marks})",
                (_iso(), *[int(value) for value in idea_ids]),
            )

    @staticmethod
    def _day_key() -> str:
        try:
            from services.usage_costs import REPORT_TZ

            return _now().astimezone(REPORT_TZ).date().isoformat()
        except Exception:
            return _now().date().isoformat()

    def reserve_budget(self, stage: str, estimated_usd: float, daily_cap: float) -> int | None:
        estimate = max(0.0, float(estimated_usd))
        day = self._day_key()
        now = _iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            used = conn.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN status='settled' THEN actual_usd
                                         WHEN status='reserved' THEN estimated_usd ELSE 0 END),0)
                FROM reflection_budget WHERE day=?
                """,
                (day,),
            ).fetchone()[0]
            if float(used) + estimate > max(0.0, float(daily_cap)):
                return None
            cur = conn.execute(
                """
                INSERT INTO reflection_budget(
                    day,stage,estimated_usd,status,created_at,updated_at
                ) VALUES(?,?,?,'reserved',?,?)
                """,
                (day, stage, estimate, now, now),
            )
            return int(cur.lastrowid)

    def settle_budget(self, reservation_id: int, actual_usd: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reflection_budget SET status='settled',actual_usd=?,updated_at=?
                WHERE id=? AND status='reserved'
                """,
                (max(0.0, float(actual_usd)), _iso(), int(reservation_id)),
            )

    def release_budget(self, reservation_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE reflection_budget SET status='released',actual_usd=0,updated_at=?
                WHERE id=? AND status='reserved'
                """,
                (_iso(), int(reservation_id)),
            )

    def budget_status(self, daily_cap: float) -> dict[str, float]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status='settled' THEN actual_usd ELSE 0 END),0),
                    COALESCE(SUM(CASE WHEN status='reserved' THEN estimated_usd ELSE 0 END),0)
                FROM reflection_budget WHERE day=?
                """,
                (self._day_key(),),
            ).fetchone()
        spent, reserved = float(row[0]), float(row[1])
        return {
            "cap": float(daily_cap),
            "spent": spent,
            "reserved": reserved,
            "remaining": max(0.0, float(daily_cap) - spent - reserved),
        }

    def record_run(self, stage: str, status: str, *, model: str = "", detail: str = "") -> None:
        now = _iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reflection_runs(stage,status,model,detail,started_at,finished_at)
                VALUES(?,?,?,?,?,?)
                """,
                (stage, status, model or None, detail[:1000], now, now),
            )

    def recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id,stage,status,model,detail,finished_at
                FROM reflection_runs ORDER BY id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_run(self, stage: str, *, status: str | None = "ok") -> datetime | None:
        with self.connect() as conn:
            if status is None:
                row = conn.execute(
                    """
                    SELECT finished_at FROM reflection_runs
                    WHERE stage=? ORDER BY id DESC LIMIT 1
                    """,
                    (stage,),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT finished_at FROM reflection_runs
                    WHERE stage=? AND status=? ORDER BY id DESC LIMIT 1
                    """,
                    (stage, status),
                ).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row[0])
        except (TypeError, ValueError):
            return None

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "pending_sessions": int(conn.execute(
                    "SELECT COUNT(*) FROM reflection_sessions WHERE status='pending'"
                ).fetchone()[0]),
                "new_insights": int(conn.execute(
                    "SELECT COUNT(*) FROM reflection_insights WHERE status='new'"
                ).fetchone()[0]),
                "active_ideas": int(conn.execute(
                    "SELECT COUNT(*) FROM reflection_ideas WHERE status='active'"
                ).fetchone()[0]),
            }

    def prune(self, *, session_days: int = 30, audit_days: int = 90) -> None:
        """Bound operational metadata without deleting active evidence or ideas."""
        session_cutoff = _iso(_now() - timedelta(days=max(1, int(session_days))))
        audit_cutoff = _iso(_now() - timedelta(days=max(1, int(audit_days))))
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM reflection_sessions
                WHERE status IN ('complete','failed') AND expires_at<?
                """,
                (session_cutoff,),
            )
            conn.execute(
                "DELETE FROM reflection_insight_events WHERE seen_at<?",
                (audit_cutoff,),
            )
            conn.execute(
                "DELETE FROM reflection_runs WHERE finished_at<?",
                (audit_cutoff,),
            )
            conn.execute(
                "DELETE FROM reflection_budget WHERE created_at<?",
                (audit_cutoff,),
            )


__all__ = ["ReflectionStore"]
