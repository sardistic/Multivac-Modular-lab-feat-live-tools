# Bounded Reflection and Improvement Proposals

## Purpose

Multivac can run a low-frequency background improvement loop without silently
joining every conversation or autonomously changing itself. The loop observes a
bounded slice of a channel after a user explicitly invokes the bot, extracts
product-level conclusions, counts recurring operational errors, reads relevant
repository source, and prepares evidence-backed ideas for the owner.

This is structured reflection, not stored chain-of-thought. Model outputs are
strict JSON conclusions and never become code, approval, or deployment authority.

## Observation boundary

An explicit mention or reply to Multivac is consent for that bounded reflection
session. No separate opt-in command is required. `/reflection_off` deletes
pending sessions and derived observations involving that user; explicitly
invoking Multivac later consents to a new session. If deletion removes an idea's
final evidence, that idea is superseded. The operator can disable the subsystem
entirely with `REFLECTION_ENABLED=false`.

An explicit Discord mention or reply to Multivac opens one channel-scoped
session for that requester:

- ten minutes before the invocation by default;
- twenty minutes after the invocation by default;
- up to 100 non-empty messages and 16,000 characters;
- only the same guild and channel;
- later explicit invocations by the same user extend the existing pending window.

During the open tail, ordinary channel messages can provide context without
repeatedly mentioning the bot. Other bots are excluded. Roles are reduced to
`requester`, `participant`, and `assistant`, and the extractor is instructed to
use participant messages only to assess the bot interaction—not to profile
participants or infer protected traits.

Discord history is fetched when the window closes and processed ephemerally. If
that fetch is unavailable, the fallback queries Elasticsearch/OpenSearch only
for the requester's already-indexed trigger messages and the bot replies tied to
them. The reflection SQLite database does not retain the surrounding message
text. It retains short derived observations, HMAC-hashed actor references,
bounded message/session IDs, counts, proposal ideas, run status, and budget rows.
Completed session rows are retained for 30 days by default; run, budget, and
frequency-event metadata is retained for 90 days. Active derived evidence and
ideas remain until superseded or removed through consent deletion.

## Error recognition

When reflection is enabled, a root logging handler turns `ERROR` and `CRITICAL`
records into local operational observations. It stores the stable logging
template rather than formatted runtime arguments, redacts credentials, URLs,
emails, mentions, Discord snowflakes, UUIDs, and local username path segments,
then fingerprints the component, exception type, and normalized template.

Repeated matches increment an occurrence counter and a timestamped event. The
planner considers only the recent signal window (seven days by default), so an
old isolated failure does not remain "frequent" forever. Three recent signals,
including three occurrences of one error, are enough to make planning eligible.

## Local and model work split

Host-based code performs scheduling, strict scope checks, transcript limits,
redaction, hashing, deduplication, frequency tracking, SQLite persistence,
Elasticsearch fallback retrieval, budget reservations, and read-only repository
context selection. No local language model service is required.

Model work uses the Responses API with `store=false`, strict JSON schemas, and
Flex processing. Defaults are:

- extraction: `gpt-5.4-nano`, low reasoning, once per completed useful window;
- planning: `gpt-5.6-sol`, high reasoning, at most once per 24 hours after enough signals;
- cleanup: `gpt-5.6-luna`, medium reasoning, weekly and only with at least 12 active ideas;
- owner-requested code draft: `gpt-5.4-mini`, using the already-reviewed plan and bounded source context.

Flex unavailability fails closed. Automatic stages do not fall back to a more
expensive service tier. Failed planning and cleanup attempts are rate-limited to
one per hour by default.

## Cost controls

Automatic extraction, planning, and cleanup share an atomic daily reservation
ledger. The default cap is `$1.50` per reporting day. Each call reserves a
conservative maximum before contacting the provider, records actual token cost,
and releases unused reservation. Once the cap is unavailable, work is delayed;
the worker cannot spend through the gate by launching concurrent calls.

Owner-triggered `/reflection_propose` calls are not background spend. They use
the low-cost coding model and appear in the ordinary usage ledger as
`reflection_code`; the owner controls their frequency.

## Review and hotload boundary

`/reflection_activity` shows the owner sanitized structured observations,
recent-versus-lifetime frequency, confidence, anonymized actor counts, recent
model/local run outcomes, planning and cleanup thresholds, next time-eligible
runs, and current automatic budget. It intentionally omits raw messages,
message IDs, actor hashes, user identities, source transcripts, and private
model reasoning. Provider/run details pass through the same credential and
identity redaction used for error fingerprints.

`/reflection_ideas` lists active ideas and their evidence counts.
`/reflection_propose <idea_id>` asks the cheap coding tier to draft a patch from
the reviewed idea and policy-allowed source context. The result becomes an
ordinary code proposal on the canonical baseline and must pass static policy
validation and explicit owner approval.

The reflection worker cannot approve, sign, activate, deploy, restart, or roll
back code. A standalone eligible `live_tools/`, `live_commands/`, or
`live_components/` proposal can use its existing signed hotload channel after
approval. Core, dependency, schema, bootstrap, and mixed changes remain normal
release-and-restart work.

## Operator configuration

```text
REFLECTION_ENABLED=false
REFLECTION_DAILY_BUDGET_USD=1.50
REFLECTION_WINDOW_MINUTES=20
REFLECTION_LOOKBACK_MINUTES=10
REFLECTION_POLL_SECONDS=120
REFLECTION_MAX_SESSIONS_PER_TICK=4
REFLECTION_SIGNAL_WINDOW_DAYS=7
REFLECTION_SESSION_RETENTION_DAYS=30
REFLECTION_AUDIT_RETENTION_DAYS=90
REFLECTION_PLAN_INTERVAL_HOURS=24
REFLECTION_CLEANUP_INTERVAL_HOURS=168
REFLECTION_RETRY_INTERVAL_MINUTES=60
REFLECTION_EXTRACT_MODEL=gpt-5.4-nano
REFLECTION_PLAN_MODEL=gpt-5.6-sol
REFLECTION_CLEANUP_MODEL=gpt-5.6-luna
OPENAI_IDEA_CODE_MODEL=gpt-5.4-mini
REFLECTION_DB_PATH=/state/reflection_state.db
```

The release Compose contract enables the service and sets the `$1.50` automatic
cap unless the host explicitly overrides those values. Direct local runs remain
disabled unless `REFLECTION_ENABLED` is set.

Use `/reflection_status` to inspect the active scope, model tiers, queue sizes,
and today's spent, reserved, and remaining automatic budget. Use
`/reflection_activity` for the owner-safe activity and schedule feed.
