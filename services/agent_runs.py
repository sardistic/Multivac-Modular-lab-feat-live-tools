"""Durable, privacy-bounded state for model tool runs.

This records orchestration facts—not private chain of thought or conversation
transcripts. By default arguments are reduced to field names and evidence is
limited to public URLs/provider metadata. Payload tracing can be disabled as a
whole with ``AGENT_TRACE_ENABLED=false``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


logger = logging.getLogger("agent_runs")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_SENSITIVE_KEY_RE = re.compile(
    r"(?:content|message|prompt|instruction|memory|image|base64|token|secret|password|api.?key)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)((?:api[_-]?key|token|password|secret|key)\s*[=:]\s*)[^\s&,]+"
)


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _state_db_path() -> Path:
    root = Path(os.getenv("MULTIVAC_STATE_DIR", Path(__file__).resolve().parent.parent))
    root.mkdir(parents=True, exist_ok=True)
    return root / "conversation_history.db"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _redact(value: Any, *, limit: int = 500) -> str:
    return _SECRET_VALUE_RE.sub(r"\1[REDACTED]", str(value or ""))[:limit]


def _public_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        safe_query = urlencode(
            [
                (key, val)
                for key, val in parse_qsl(parts.query, keep_blank_values=True)
                if not _SENSITIVE_KEY_RE.search(key)
            ]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, safe_query, ""))[:2000]
    except Exception:
        return value.split("?", 1)[0][:2000]


def _argument_shape(args: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key == "_context" or _SENSITIVE_KEY_RE.search(str(key)):
            continue
        if isinstance(value, bool) or value is None:
            out[str(key)] = value
        elif isinstance(value, (int, float)):
            out[str(key)] = value
        elif isinstance(value, str):
            out[str(key)] = f"<str:{len(value)}>"
        else:
            out[str(key)] = f"<{type(value).__name__}>"
    return out


def _urls(value: Any, *, limit: int = 20) -> list[str]:
    found: list[str] = []

    def walk(item: Any) -> None:
        if len(found) >= limit:
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if str(key).lower() in {"url", "link", "source"} and isinstance(child, str):
                    if child.startswith(("http://", "https://")) and child not in found:
                        found.append(_public_url(child))
                elif not _SENSITIVE_KEY_RE.search(str(key)):
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str) and not item.startswith("data:"):
            for match in _URL_RE.findall(item):
                clean = match.rstrip(".,);]")
                if clean not in found:
                    found.append(_public_url(clean))
                    if len(found) >= limit:
                        break

    walk(value)
    return found


def _result_summary(result: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"evidence_urls": _urls(result)}
    if isinstance(result, dict):
        for key in ("ok", "provider", "lookup_type", "match_found", "result_counts", "status"):
            if key in result:
                summary[key] = result[key]
        if result.get("error"):
            summary["error"] = _redact(result.get("error"), limit=240)
    elif isinstance(result, list):
        summary["result_count"] = len(result)
    elif isinstance(result, str):
        summary["length"] = len(result)
        if result.lower().startswith(("error", "tool_error", "❌")):
            summary["error"] = _redact(result, limit=240)
    return summary


_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    intent TEXT,
    scope_key TEXT,
    user_id TEXT,
    status TEXT NOT NULL,
    max_steps INTEGER NOT NULL,
    step_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'not_required',
    metadata_json TEXT
);
CREATE TABLE IF NOT EXISTS agent_run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    phase TEXT NOT NULL,
    tool_name TEXT,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    args_json TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES agent_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_scope ON agent_runs(scope_key, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_run_steps(run_id, step_index);
"""


class AgentRunStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _state_db_path()
        self._lock = RLock()
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(_SCHEMA)
            stale_before = (datetime.now(timezone.utc) - timedelta(hours=6)).replace(microsecond=0).isoformat()
            conn.execute(
                """
                UPDATE agent_runs SET status='interrupted', updated_at=?
                WHERE status='running' AND updated_at<?
                """,
                (_now(), stale_before),
            )

    def start(
        self,
        *,
        provider: str,
        model: str,
        context: dict[str, Any] | None,
        max_steps: int,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        ctx = context or {}
        guild = str(ctx.get("guild_id") or "DM")
        channel = str(ctx.get("channel_id") or ctx.get("conversation_id") or "unknown")
        user_id = str(ctx.get("user_id") or "") or None
        scope_key = f"{guild}:{channel}:{user_id or 'unknown'}"
        now = _now()
        meta = dict(metadata or {})
        if ctx.get("request_text"):
            meta["request_sha256"] = hashlib.sha256(
                str(ctx["request_text"]).encode("utf-8", errors="replace")
            ).hexdigest()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs(
                    run_id,created_at,updated_at,provider,model,intent,scope_key,user_id,
                    status,max_steps,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    now,
                    now,
                    provider,
                    model,
                    str(ctx.get("intent") or "") or None,
                    scope_key,
                    user_id,
                    "running",
                    max(1, int(max_steps)),
                    _json(meta),
                ),
            )
        return run_id

    def add_step(
        self,
        run_id: str,
        *,
        step_index: int,
        phase: str,
        status: str,
        tool_name: str | None = None,
        attempt: int = 1,
        duration_ms: int = 0,
        args: dict[str, Any] | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        now = _now()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO agent_run_steps(
                    run_id,step_index,phase,tool_name,status,attempt,duration_ms,
                    args_json,result_json,error,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    step_index,
                    phase,
                    tool_name,
                    status,
                    attempt,
                    max(0, int(duration_ms)),
                    _json(_argument_shape(args)),
                    _json(_result_summary(result)),
                    _redact(error, limit=500) or None,
                    now,
                ),
            )
            conn.execute(
                "UPDATE agent_runs SET step_count=MAX(step_count,?), updated_at=? WHERE run_id=?",
                (step_index, now, run_id),
            )

    def add_retry(self, run_id: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE agent_runs SET retry_count=retry_count+1, updated_at=? WHERE run_id=?",
                (_now(), run_id),
            )

    def set_approval(self, run_id: str, state: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE agent_runs SET approval_state=?, updated_at=? WHERE run_id=?",
                (state[:64], _now(), run_id),
            )

    def finish(self, run_id: str, status: str) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE agent_runs SET status=?, updated_at=? WHERE run_id=?",
                (status[:64], _now(), run_id),
            )

    def recent(self, *, scope_key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        where = "WHERE scope_key=?" if scope_key else ""
        params: tuple[Any, ...] = (scope_key, max(1, min(int(limit), 100))) if scope_key else (max(1, min(int(limit), 100)),)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id,created_at,updated_at,provider,model,intent,scope_key,user_id,
                       status,max_steps,step_count,retry_count,approval_state,metadata_json
                FROM agent_runs {where} ORDER BY created_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_run_steps WHERE run_id=? ORDER BY step_index,id",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]


class AgentRunRecorder:
    """Failure-isolated convenience wrapper used by provider loops."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        context: dict[str, Any] | None,
        max_steps: int,
        metadata: dict[str, Any] | None = None,
        store: AgentRunStore | None = None,
    ) -> None:
        self.enabled = _truthy(os.getenv("AGENT_TRACE_ENABLED"), True)
        self.store = store
        self.run_id: str | None = None
        if not self.enabled:
            return
        try:
            self.store = self.store or AgentRunStore()
            self.run_id = self.store.start(
                provider=provider,
                model=model,
                context=context,
                max_steps=max_steps,
                metadata=metadata,
            )
        except Exception:
            logger.warning("Unable to start durable agent trace", exc_info=True)
            self.enabled = False

    def step(self, **kwargs: Any) -> None:
        if not (self.enabled and self.store and self.run_id):
            return
        try:
            self.store.add_step(self.run_id, **kwargs)
        except Exception:
            logger.warning("Unable to persist agent step", exc_info=True)

    def retry(self) -> None:
        if self.enabled and self.store and self.run_id:
            try:
                self.store.add_retry(self.run_id)
            except Exception:
                logger.warning("Unable to persist agent retry", exc_info=True)

    def approval(self, state: str) -> None:
        if self.enabled and self.store and self.run_id:
            try:
                self.store.set_approval(self.run_id, state)
            except Exception:
                logger.warning("Unable to persist agent approval state", exc_info=True)

    def finish(self, status: str = "completed") -> None:
        if self.enabled and self.store and self.run_id:
            try:
                self.store.finish(self.run_id, status)
            except Exception:
                logger.warning("Unable to finish durable agent trace", exc_info=True)


__all__ = ["AgentRunRecorder", "AgentRunStore"]
