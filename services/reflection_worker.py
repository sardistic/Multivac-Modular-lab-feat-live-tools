"""Ongoing, invocation-scoped reflection orchestration.

The worker performs local scheduling, Elasticsearch retrieval, deduplication,
and code lookup. Model calls are optional, delayed, and budget-governed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from services.code_changes import get_baseline_sha
from services.code_generator import select_code_context
from services.memory_client import search_raw
from services.reflection_models import ReflectionModels
from services.reflection_store import ReflectionStore

logger = logging.getLogger("discord_bot")

_SECRET_RE = re.compile(
    r"(?i)((?:api[_-]?key|token|password|secret|authorization)\s*[=:]\s*)\S{6,}"
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|<#\d+>")
_SNOWFLAKE_RE = re.compile(r"\b\d{15,22}\b")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_WINDOWS_USER_PATH_RE = re.compile(r"(?i)\b([a-z]:\\users\\)[^\\\s]+")


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1[REDACTED]", text or "")


def _sanitize_error_summary(text: str) -> str:
    value = _redact(text or "")
    value = _URL_RE.sub("[URL]", value)
    value = _EMAIL_RE.sub("[EMAIL]", value)
    value = _MENTION_RE.sub("[MENTION]", value)
    value = _SNOWFLAKE_RE.sub("[ID]", value)
    value = _UUID_RE.sub("[UUID]", value)
    value = _WINDOWS_USER_PATH_RE.sub(r"\1[USER]", value)
    return " ".join(value.split())[:800]


class ReflectionWorker:
    def __init__(
        self,
        store: ReflectionStore | None = None,
        *,
        history_fetcher: Callable[[dict], Awaitable[list[dict]]] | None = None,
    ) -> None:
        self.store = store or ReflectionStore()
        self.models = ReflectionModels(self.store)
        self.history_fetcher = history_fetcher
        self.enabled = _truthy(os.getenv("REFLECTION_ENABLED"), False)
        self.idle_minutes = max(1, int(os.getenv("REFLECTION_IDLE_MINUTES", "5")))
        self.lookback_minutes = max(0, int(os.getenv("REFLECTION_LOOKBACK_MINUTES", "10")))
        self.poll_seconds = max(30, int(os.getenv("REFLECTION_POLL_SECONDS", "120")))
        self.pulse_workers = max(1, int(os.getenv("REFLECTION_PULSE_WORKERS", "2")))
        self.pulse_queue_max = max(
            25, int(os.getenv("REFLECTION_PULSE_QUEUE_MAX", "500"))
        )
        self.max_sessions_per_tick = max(
            1, int(os.getenv("REFLECTION_MAX_SESSIONS_PER_TICK", "4"))
        )
        self.plan_interval_hours = max(
            1, int(os.getenv("REFLECTION_PLAN_INTERVAL_HOURS", "24"))
        )
        self.cleanup_interval_hours = max(
            24, int(os.getenv("REFLECTION_CLEANUP_INTERVAL_HOURS", "168"))
        )
        self.retry_interval_minutes = max(
            15, int(os.getenv("REFLECTION_RETRY_INTERVAL_MINUTES", "60"))
        )
        self.signal_window_days = max(
            1, int(os.getenv("REFLECTION_SIGNAL_WINDOW_DAYS", "7"))
        )
        self.session_retention_days = max(
            1, int(os.getenv("REFLECTION_SESSION_RETENTION_DAYS", "30"))
        )
        self.audit_retention_days = max(
            self.signal_window_days,
            int(os.getenv("REFLECTION_AUDIT_RETENTION_DAYS", "90")),
        )
        self._pulse_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=self.pulse_queue_max
        )
        self._stop = asyncio.Event()

    def set_user_enabled(self, user_id: str, enabled: bool) -> None:
        if enabled:
            self.store.set_user_enabled(str(user_id), True)
        else:
            self.store.forget_user(str(user_id))

    def user_enabled(self, user_id: str) -> bool:
        return self.store.user_enabled(str(user_id))

    def note_invocation(
        self,
        *,
        guild_id: str,
        channel_id: str,
        user_id: str,
        message_id: str,
    ) -> int | None:
        if not self.enabled:
            return None
        # The explicit mention/reply is consent for this bounded session. The
        # preference row doubles as a revocation latch for in-flight work;
        # invoking again reopens it for the new session.
        self.store.set_user_enabled(str(user_id), True)
        return self.store.record_invocation(
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            user_id=str(user_id),
            message_id=str(message_id),
            idle_minutes=self.idle_minutes,
            lookback_minutes=self.lookback_minutes,
        )

    async def _enqueue_pulse(
        self,
        session: dict[str, Any],
        *,
        message_id: str,
        role: str,
        content: str,
    ) -> None:
        safe_role = role if role in {"requester", "participant", "assistant"} else "participant"
        safe_content = _redact(str(content or "").strip())[:1200]
        if not safe_content:
            return
        await self._pulse_queue.put(
            {
                "session": session,
                "message": {
                    "message_id": str(message_id),
                    "role": safe_role,
                    "content": safe_content,
                },
            }
        )

    async def observe_message(
        self,
        *,
        guild_id: str,
        channel_id: str,
        author_id: str,
        message_id: str,
        content: str,
        role: str | None = None,
    ) -> set[int]:
        """Extend active channel sessions and queue one tiny reflection per session."""
        if not self.enabled:
            return set()
        message_content = str(content or "").strip() or "[non-text message]"
        sessions = await asyncio.to_thread(
            self.store.record_channel_activity,
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            message_id=str(message_id),
            idle_minutes=self.idle_minutes,
        )
        observed: set[int] = set()
        for session in sessions:
            session_id = int(session["id"])
            observed.add(session_id)
            message_role = role or (
                "requester"
                if str(author_id) == str(session["user_id"])
                else "participant"
            )
            await self._enqueue_pulse(
                session,
                message_id=str(message_id),
                role=message_role,
                content=message_content,
            )
        return observed

    async def observe_invocation(
        self,
        *,
        guild_id: str,
        channel_id: str,
        user_id: str,
        message_id: str,
        content: str,
        already_observed: set[int] | None = None,
    ) -> int | None:
        """Record invocation consent and ensure its triggering message gets one pulse."""
        session_id = await asyncio.to_thread(
            self.note_invocation,
            guild_id=str(guild_id),
            channel_id=str(channel_id),
            user_id=str(user_id),
            message_id=str(message_id),
        )
        if session_id is None or session_id in (already_observed or set()):
            return session_id
        session = await asyncio.to_thread(self.store.get_session, session_id)
        if session is not None:
            await self._enqueue_pulse(
                session,
                message_id=str(message_id),
                role="requester",
                content=content,
            )
        return session_id

    async def _process_pulse(self, pulse: dict[str, Any]) -> None:
        session = pulse["session"]
        if not self.store.user_enabled(str(session["user_id"])):
            return
        try:
            result = await self.models.pulse(dict(pulse["message"]))
            if not result.get("useful"):
                return
            summary = " ".join(str(result.get("summary") or "").split())
            kind = str(result.get("kind") or "behavior_pattern")
            allowed_kinds = {
                "pain_point", "behavior_pattern", "feature_request", "success",
            }
            if not summary or kind not in allowed_kinds:
                return
            self.store.add_insight(
                session=session,
                kind=kind,
                summary=summary,
                confidence=float(result.get("confidence") or 0),
                evidence_ids=[str(pulse["message"]["message_id"])],
                require_user_enabled=True,
            )
        except RuntimeError:
            # Budget exhaustion and Flex unavailability are already recorded.
            logger.warning("reflection pulse skipped because model capacity is unavailable")
        except Exception:
            logger.warning("reflection pulse failed", exc_info=True)

    async def _consume_pulses(self) -> None:
        while True:
            pulse = await self._pulse_queue.get()
            try:
                await self._process_pulse(pulse)
            finally:
                self._pulse_queue.task_done()

    @staticmethod
    def _fetch_indexed_evidence(session: dict) -> list[dict]:
        message_ids = [str(value) for value in session.get("message_ids") or []]
        if not message_ids:
            return []
        query = {
            "bool": {
                "filter": [
                    {"term": {"guild_id": str(session["guild_id"])}},
                    {"term": {"channel_id": str(session["channel_id"])}},
                    {"term": {"user_id": str(session["user_id"])}},
                    {
                        "range": {
                            "timestamp": {
                                "gte": session["started_at"],
                                "lte": session["expires_at"],
                            }
                        }
                    },
                ],
                "should": [
                    {"ids": {"values": message_ids}},
                    {
                        "bool": {
                            "filter": [
                                {"term": {"role": "assistant"}},
                                {"terms": {"reply_to_id": message_ids}},
                            ]
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }
        response = search_raw(
            query,
            size=40,
            source=["message_id", "reply_to_id", "role", "content", "timestamp"],
            sort=[{"timestamp": {"order": "asc"}}],
        )
        transcript = []
        for hit in response.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            message_id = str(source.get("message_id") or hit.get("_id") or "")
            role = "assistant" if source.get("role") == "assistant" else "requester"
            content = _redact(str(source.get("content") or "").strip())[:1200]
            if message_id and content:
                transcript.append(
                    {"message_id": message_id, "role": role, "content": content}
                )
        return transcript

    @staticmethod
    def _sanitize_transcript(transcript: list[dict]) -> list[dict]:
        cleaned = []
        total_chars = 0
        for item in transcript[:100]:
            message_id = str(item.get("message_id") or "")
            role = str(item.get("role") or "participant")
            if role not in {"requester", "participant", "assistant"}:
                role = "participant"
            content = _redact(str(item.get("content") or "").strip())[:1200]
            if not message_id or not content:
                continue
            if total_chars + len(content) > 16_000:
                break
            cleaned.append({"message_id": message_id, "role": role, "content": content})
            total_chars += len(content)
        return cleaned

    def note_runtime_error(
        self,
        *,
        component: str,
        error_type: str,
        summary: str,
        evidence_id: str | None = None,
    ) -> int | None:
        if not self.enabled:
            return None
        safe_component = re.sub(r"[^a-zA-Z0-9_.-]", "_", component or "unknown")[:120]
        safe_type = re.sub(r"[^a-zA-Z0-9_.-]", "_", error_type or "Error")[:120]
        safe_summary = _sanitize_error_summary(summary)
        if not safe_summary:
            return None
        return self.store.record_runtime_error(
            component=safe_component,
            error_type=safe_type,
            summary=safe_summary,
            evidence_id=evidence_id,
        )

    async def _process_session(self, session: dict) -> None:
        if not self.store.claim_session(session["id"]):
            return
        try:
            if not self.store.user_enabled(session["user_id"]):
                self.store.finish_session(
                    session["id"], status="complete", detail="consent_withdrawn"
                )
                return
            if self.history_fetcher is not None:
                transcript = await self.history_fetcher(session)
            else:
                transcript = await asyncio.to_thread(self._fetch_indexed_evidence, session)
            transcript = self._sanitize_transcript(transcript)
            if len(transcript) < 2 or sum(len(item["content"]) for item in transcript) < 40:
                self.store.finish_session(session["id"], status="complete", detail="low_signal")
                return
            result = await self.models.extract(transcript)
            allowed_ids = {item["message_id"] for item in transcript}
            added = 0
            if result.get("useful"):
                for insight in list(result.get("insights") or [])[:5]:
                    evidence_ids = [
                        str(value)
                        for value in insight.get("evidence_message_ids") or []
                        if str(value) in allowed_ids
                    ]
                    summary = " ".join(str(insight.get("summary") or "").split())
                    if not summary or not evidence_ids:
                        continue
                    insight_id = self.store.add_insight(
                        session=session,
                        kind=str(insight.get("kind") or "behavior_pattern"),
                        summary=summary,
                        confidence=float(insight.get("confidence") or 0),
                        evidence_ids=evidence_ids,
                        require_user_enabled=True,
                    )
                    if insight_id is not None:
                        added += 1
            self.store.finish_session(
                session["id"], status="complete", detail=f"insights={added}"
            )
        except RuntimeError as exc:
            # Budget exhaustion and Flex unavailability are retryable and never
            # fall back to more expensive standard processing.
            self.store.retry_session(session["id"], str(exc))
        except Exception as exc:
            logger.warning("reflection session %s failed", session["id"], exc_info=True)
            self.store.retry_session(session["id"], str(exc))

    @staticmethod
    def _code_context(insights: list[dict]) -> str:
        request = "\n".join(item["summary"] for item in insights)[:12_000]
        baseline = get_baseline_sha()
        selected = select_code_context(request, baseline)[:6]
        blocks = [
            f"===== {path} =====\n{content[:5000]}" for path, content in selected
        ]
        return f"BASELINE {baseline}\n" + "\n\n".join(blocks)

    def _stage_due(self, stage: str, interval_hours: int) -> bool:
        now = datetime.now(timezone.utc)
        last_success = self.store.last_run(stage)
        if last_success is not None and (
            last_success.tzinfo is None
            or last_success + timedelta(hours=interval_hours) > now
        ):
            return False
        last_attempt = self.store.last_run(stage, status=None)
        return last_attempt is None or (
            last_attempt.tzinfo is not None
            and last_attempt + timedelta(minutes=self.retry_interval_minutes) <= now
        )

    def _planning_due(self) -> bool:
        return self._stage_due("plan", self.plan_interval_hours)

    def _cleanup_due(self) -> bool:
        return self._stage_due("cleanup", self.cleanup_interval_hours)

    def _next_stage_eligible(self, stage: str, interval_hours: int) -> datetime:
        now = datetime.now(timezone.utc)
        candidates = [now]
        last_success = self.store.last_run(stage)
        if last_success is not None and last_success.tzinfo is not None:
            candidates.append(last_success + timedelta(hours=interval_hours))
        last_attempt = self.store.last_run(stage, status=None)
        if last_attempt is not None and last_attempt.tzinfo is not None:
            candidates.append(
                last_attempt + timedelta(minutes=self.retry_interval_minutes)
            )
        return max(candidates)

    async def _maybe_plan(self) -> None:
        insights = [
            item
            for item in self.store.pending_insights(
                40, recent_days=self.signal_window_days
            )
            if int(item.get("recent_occurrences", 0)) > 0
        ]
        signal_strength = sum(
            max(0, min(3, int(item.get("recent_occurrences", 0))))
            for item in insights
        )
        if signal_strength < 3 or not self._planning_due():
            return
        try:
            code_context = await asyncio.to_thread(self._code_context, insights)
            result = await self.models.plan(insights, code_context)
            allowed_ids = {int(item["id"]) for item in insights}
            allowed_paths = {
                line.removeprefix("===== ").removesuffix(" =====")
                for line in code_context.splitlines()
                if line.startswith("===== ") and line.endswith(" =====")
            }
            for idea in list(result.get("ideas") or [])[:5]:
                evidence_ids = [
                    int(value) for value in idea.get("insight_ids") or []
                    if int(value) in allowed_ids
                ]
                if not evidence_ids:
                    continue
                idea["code_paths"] = [
                    str(path) for path in idea.get("code_paths") or []
                    if str(path) in allowed_paths
                ]
                self.store.save_idea(idea, evidence_ids)
        except Exception:
            logger.warning("reflection planning failed", exc_info=True)

    async def _maybe_cleanup(self) -> None:
        ideas = self.store.list_ideas(50)
        if len(ideas) < 12 or not self._cleanup_due():
            return
        try:
            result = await self.models.cleanup(ideas)
            allowed = {int(item["id"]) for item in ideas}
            supersede = [
                int(value) for value in result.get("supersede_ids") or []
                if int(value) in allowed
            ]
            self.store.supersede_ideas(supersede)
        except Exception:
            logger.warning("reflection cleanup failed", exc_info=True)

    async def _maybe_prune(self) -> None:
        if not self._stage_due("prune", 24):
            return
        try:
            await asyncio.to_thread(
                self.store.prune,
                session_days=self.session_retention_days,
                audit_days=self.audit_retention_days,
            )
            self.store.record_run("prune", "ok")
        except Exception as exc:
            self.store.record_run("prune", "failed", detail=str(exc))
            logger.warning("reflection metadata pruning failed", exc_info=True)

    async def run_once(self) -> None:
        if not self.enabled:
            return
        sessions = await asyncio.to_thread(
            self.store.due_sessions, limit=self.max_sessions_per_tick
        )
        for session in sessions:
            await self._process_session(session)
        await self._maybe_plan()
        await self._maybe_cleanup()
        await self._maybe_prune()

    async def run_forever(self) -> None:
        consumers = [
            asyncio.create_task(
                self._consume_pulses(), name=f"multivac-reflection-pulse-{index + 1}"
            )
            for index in range(self.pulse_workers)
        ]
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("reflection worker tick failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            for consumer in consumers:
                consumer.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "idle_minutes": self.idle_minutes,
            "lookback_minutes": self.lookback_minutes,
            "pulse_queue": self._pulse_queue.qsize(),
            "pulse_workers": self.pulse_workers,
            "models": {
                "pulse": self.models.extract_model,
                "extract": self.models.extract_model,
                "plan": self.models.plan_model,
                "cleanup": self.models.cleanup_model,
            },
            "budget": self.store.budget_status(self.models.daily_cap),
            **self.store.counts(),
        }

    def activity(self, limit: int = 5) -> dict[str, Any]:
        """Owner-safe activity view: derived records only, never transcripts or reasoning."""
        bounded_limit = max(1, min(10, int(limit)))
        observations = self.store.recent_insights(
            bounded_limit, recent_days=self.signal_window_days
        )
        safe_observations = [
            {
                "id": int(item["id"]),
                "kind": str(item["kind"]),
                "summary": _sanitize_error_summary(str(item["summary"])),
                "confidence": float(item["confidence"]),
                "occurrences": int(item["occurrences"]),
                "recent_occurrences": int(item.get("recent_occurrences", 0)),
                "actor_count": int(item["actor_count"]),
                "status": str(item["status"]),
                "last_seen_at": str(item["last_seen_at"]),
            }
            for item in observations
        ]
        safe_runs = []
        for run in self.store.recent_runs(bounded_limit):
            detail = _sanitize_error_summary(str(run.get("detail") or ""))
            safe_runs.append(
                {
                    "stage": str(run["stage"]),
                    "status": str(run["status"]),
                    "model": str(run.get("model") or "local"),
                    "detail": detail,
                    "finished_at": str(run["finished_at"]),
                }
            )
        pending = self.store.pending_insights(
            40, recent_days=self.signal_window_days
        )
        signal_strength = sum(
            max(0, min(3, int(item.get("recent_occurrences", 0))))
            for item in pending
        )
        active_ideas = len(self.store.list_ideas(50))
        return {
            "observations": safe_observations,
            "runs": safe_runs,
            "signal_strength": signal_strength,
            "signal_threshold": 3,
            "active_ideas": active_ideas,
            "cleanup_threshold": 12,
            "idle_minutes": self.idle_minutes,
            "pending_sessions": self.store.counts()["pending_sessions"],
            "pulse_queue": self._pulse_queue.qsize(),
            "next_plan_at": self._next_stage_eligible(
                "plan", self.plan_interval_hours
            ).isoformat(),
            "next_cleanup_at": self._next_stage_eligible(
                "cleanup", self.cleanup_interval_hours
            ).isoformat(),
            "budget": self.store.budget_status(self.models.daily_cap),
        }


class ReflectionErrorHandler(logging.Handler):
    """Convert repeated ERROR/CRITICAL log records into sanitized counters."""

    def __init__(self, worker: ReflectionWorker) -> None:
        super().__init__(level=logging.ERROR)
        self.worker = worker

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("reflection") or str(record.msg).lower().startswith("reflection"):
            return
        try:
            error_type = (
                record.exc_info[0].__name__
                if record.exc_info and record.exc_info[0]
                else "LoggedError"
            )
            template = str(record.msg)
            if not record.args and not record.exc_info and ":" in template:
                # A formatted f-string has no separate args to discard. Keep
                # its stable category and drop the likely dynamic suffix.
                template = f"{template.split(':', 1)[0]}: [DETAIL]"
            self.worker.note_runtime_error(
                component=record.name,
                error_type=error_type,
                # Keep the stable log template, not potentially sensitive
                # runtime arguments. It also gives recurring failures a stable
                # fingerprint across users and request IDs.
                summary=template,
            )
        except Exception:
            # Logging must never fail recursively because reflection storage is unavailable.
            return


__all__ = ["ReflectionErrorHandler", "ReflectionWorker"]
