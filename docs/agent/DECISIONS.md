# Architectural Decisions

## 2026-08-01: Apply one durable, conversation-scoped Mistake Not… identity

User-facing prose uses one checked-in `Mistake Not…` persona fragment owned by
`bot/persona.py`. The shared style builder emits an application-level
personalization-priority rule, the current user's awareness/profile block, and
their active behavioral instruction before the persona. The rule explicitly
makes individual profile preferences and behavioral instructions authoritative
over every conflicting persona trait; the persona is only a compatible fallback
voice. All of this follows provider, application, and task instructions and
precedes conversation history. The prompt is rebuilt once per applicable
request and is never copied into stored history, retrieved memory, tool output,
or user messages.

The applicable paths are normal OpenAI chat, its Gemini fallback and tool
follow-ups, direct Claude and Gemini chat, URL summaries, and the prose-producing
vision explanation and repair passes. Each path obtains awareness from the same
requesting user ID; it does not use another user's profile. Machine-only intent
and draft classifiers, OCR extraction, reflection, code generation, image/video
generation, and strict execution paths remain persona-free. Gemini now promotes
context system messages into its dedicated `system_instruction` field instead
of representing them as prior model turns; ordinary user and assistant history
remains content history.

The default is enabled. Explicit standalone requests to disable or resume the
identity are handled before model routing, preventing the generic behavioral
memory tool from accidentally turning a conversation-local choice into a global
user preference. State is persisted in the forward-compatible
`conversation_personas` SQLite table at guild/channel/user scope (DMs use their
channel and user), so it survives restarts while remaining isolated from other
conversations. Missing rows safely mean enabled.

## 2026-07-29: Use a bounded post-draft quality gate for textual chat

Normal textual chat responses receive one structured post-draft review after
generation. The reviewer returns exactly one of `accept`, `revise`, or
`research`. It judges whether the answer's length is warranted by both the
request and the underlying task, whether the voice subtly reflects the user's
saved preferences, and whether a claim needs fresher evidence. A short prompt
does not automatically require a short answer, and a long prompt does not
automatically justify a long one.

Explicit user instructions are strong constraints. The distilled user profile
is only a soft style preference: it must not be quoted, exposed, or used to
stereotype the user. A direct revision may reorganize or shorten the draft but
must preserve its facts, caveats, links, and citations and may not introduce new
facts. If evidence is needed, the reviewer supplies one targeted query and the
provider gets one search-enabled regeneration; there is no recursive review or
unbounded research loop.

The existing reflection loop contributes a second soft signal. The reviewer may
receive up to four recent, confidence-filtered derived observations associated
with the current consented user. Repetition and confidence add weight when a
signal is relevant to the current request, such as avoiding a known interaction
pain point or preserving a successful response pattern. The latest request,
factual integrity, and explicit behavioral instruction remain authoritative.
Reflection signals are never treated as facts or instructions and must not be
mentioned, quoted, or exposed in the answer.

The request-time reflection view is metadata-only and user-scoped. It excludes
raw messages, private reasoning, evidence and session IDs, actor hashes, runtime
errors, dismissed observations, global ideas, and other users' signals. It is
empty when reflection is disabled or the user has withdrawn consent.

The verdict uses a strict Structured Outputs schema and is charged to the
`chat_draft_verify` usage label. Review failures accept the original draft so an
auxiliary quality pass cannot make chat unavailable. Exact code execution and
test-result paths bypass the prose reviewer, while ordinary OpenAI, Gemini, and
Claude chat paths use it. Deterministic personality transformations remain the
last presentation step.

## 2026-07-29: Force evidence collection for high-confidence fresh-fact queries

Model-directed research remains the default, but `tool_choice: auto` is not a
freshness guarantee because a model may answer from memory without calling a
tool. Intent classification now has a semantic `chat_research` route for
questions whose answers are likely to have changed, supplemented by a narrow
deterministic safety net for unmistakable freshness, current-role, live-result,
and recurring-winner query shapes.

Research requests force `web_search` only on the first model turn. Follow-up
turns return to automatic tool selection so the model can open relevant pages,
gather enough evidence, and synthesize a normal conversational answer with
source links instead of exposing a raw search-results list. Explicitly dated
historical questions are excluded from the safety net unless the user also asks
for current verification. Gemini's native search fallback follows the same
policy.

The mandatory first search does not trust the model to preserve freshness in
its query arguments. Core derives a date-aware query from the user's wording and
the current UTC date, then overrides only that first `web_search` call. Recurring
event questions add the current year plus final-result/winner terms; current
role and other dynamic questions add an explicit as-of date. Later searches
remain model-directed.

## 2026-07-28: Route web lookup through the model's research tool loop

Explicit search wording is no longer a deterministic Discord fast path that
returns provider snippets. Search and URL access are evidence-gathering tools
inside the normal chat generation loop. The model decides when fresh information
is necessary, may search without special trigger words, opens a relevant page
when snippets are insufficient, and synthesizes the final response.

Dedicated URL-summary intent routing remains for direct summary requests, while
URL-bearing analytical questions use the same model-directed page reader as
search follow-ups. Blocking search, fetch, and extraction work runs outside the
Discord event loop.

## 2026-07-21: Hotload model-callable tools through immutable registry snapshots

Tool hotloading is limited to explicit, trusted module activation. Each model
request captures one immutable registry generation containing both schemas and
handlers, preventing a reload from exposing one schema version and executing a
different handler version. Registry mutations use copy-on-write replacement,
bounded per-source rollback history, and explicit override permission.

There is no filesystem watcher. The runtime accepts only relative `.py` paths
beneath `MULTIVAC_TOOL_HOTLOAD_DIR`; the external review/test/signing supervisor
populates that directory and Compose mounts it read-only. This preserves the
rule that bot-writable or user-supplied code is never implicitly executed in
the credential-bearing Discord process.

General core-code hotloading, tool lifecycle hooks, and isolated generated-tool
workers remain separate future decisions.

## 2026-07-21: Publish reviewed tools through a signed host artifact channel

Pure `live_tools/*.py` proposals use the same baseline revalidation, complete
networkless test suite, audited commit, owner approval, and host signing key as
normal releases. The supervisor then publishes only those modules into an
immutable artifact directory mounted read-only at `/tool-artifacts` and sends a
bounded activation batch through `/state/tool-control`.

The bot verifies every requested SHA-256, applies the batch with registry
rollback on failure, and persists active logical source IDs for restoration
after restarts. The supervisor records both previous and activated source state
so proposal-specific rollback remains possible. Live-tool changes must be
standalone proposals; checked-in replacement requires an exact reviewed
`TOOL_OVERRIDES` declaration.

Mixed or non-tool changes retain the existing container recreation path. This
keeps host publication authority outside the Discord process without requiring
a bot disconnect for subsequent reviewed tool updates.

## 2026-07-21: Hotload Discord commands only through managed Cogs

Pure `live_commands/*.py` proposals share the signed host artifact channel but
use a separate persistent request/result stream and active-source projection.
Modules must implement the standard asynchronous discord.py `setup(bot)` entry
point and add commands only through newly registered Cogs. This gives the
runtime an enumerable lifecycle boundary for listeners, prefix commands,
hybrid commands, and application commands.

Command batches perform one global tree sync after all local mutations. Any
setup or sync failure rolls each source back in reverse order and attempts to
resynchronize the restored tree. Active Cogs are restored and synchronized
after process restarts, and proposal audit rows retain exact previous/current
artifact records for rollback.

Direct command-table mutation is not accepted through this channel. A reviewed
module may replace exact checked-in, non-Cog primary command names declared in
`COMMAND_OVERRIDES`; the original command objects are restored on unload or
rollback. Event, intent, and provider changes use managed behavior components.

## 2026-07-22: Hotload behavior through request-scoped component generations

Event, intent, provider, and runtime-setting changes use standalone
`live_components/*.py` modules rather than Python module reload. The permanent
Discord shell captures one immutable registry generation per event; nested
intent, provider, and setting lookups reuse that generation. New events switch
atomically while in-flight work continues with its original callbacks.

Components have an explicit async setup, optional health check and teardown,
tracked task/resource scope, stop signal, bounded drain period, persistent
active-state projection, and per-source rollback history. The supervisor uses a
separate signed behavior request/result stream and accepts behavior-only
proposals. Dependency state, schema migrations, Discord gateway bootstrap,
credentials, and the activation authority remain restart-only.

Reviewed tool and command modules may now replace checked-in symbols only when
they declare exact `TOOL_OVERRIDES` or `COMMAND_OVERRIDES`. This preserves a
recoverable original object for unload and rollback while preventing a module
from expanding its replacement scope implicitly.

## 2026-07-22: Resolve proposal baselines through the canonical branch

The running bot may execute from an immutable detached release while the host
control checkout and canonical branch continue advancing. Code proposals must
therefore record `refs/heads/main` (or the explicitly configured canonical
branch), never the running release's `HEAD`.

The supervisor validates against the same canonical ref and advances its local
copy after every successful promotion. Shared read-only Git metadata makes that
ref immediately visible to the running release without weakening code-mount
immutability. This prevents harmless documentation promotions and earlier
hotloads from making every later proposal stale.

## 2026-07-22: Share the control channel through a private setgid group

The bot runtime and host supervisor use different Unix users. Their control
directory is therefore group-owned by the dedicated runtime state group with
mode `2770`, not a single-owner `0700` directory. Both principals can traverse
and atomically replace channel files, setgid preserves the shared group on new
files, and unrelated host users retain no access.

Supervisor initialization now enforces usable read/write/traverse access and
fails closed for production paths. Restricted validation containers may retain
their disposable `/tmp` ownership exception.

## 2026-07-22: Bound reflection to invocation-consented windows and owner-reviewed proposals

Ongoing product reflection is a local scheduled subsystem, not an always-on chat
participant or autonomous code agent. A user's explicit mention or reply is
consent for a channel-scoped lookback and post-invocation tail. Discord history
is processed ephemerally; persistent reflection state contains only derived
observations, keyed actor hashes, bounded evidence IDs, recent-frequency events,
ideas, and cost/audit records. Operational errors are fingerprinted from
sanitized logging templates so recurrence can be measured without retaining
formatted request arguments.

The host handles scoping, redaction, storage, deduplication, frequency tracking,
budgeting, and read-only code selection. Structured model calls use Flex with a
cheap extractor, strong daily planner, and medium weekly cleanup pass. Automatic
work shares an atomic `$1.50` default daily ceiling and never falls back to a
more expensive tier. Provider failures are retry-throttled.

Ideas are visible only when the owner asks. A separate cheap coding pass may
turn a selected idea into an ordinary reviewable proposal, but reflection has no
approval, signing, activation, deployment, or rollback authority. Eligible
standalone changes retain the existing signed hotload path; all other changes
retain the normal release boundary.

Owner observability exposes only structured, sanitized activity: observation
summaries and counts, anonymized actor totals, run outcomes, thresholds,
schedules, and budget. Raw chat, message/evidence IDs, actor hashes, identities,
source transcripts, and private model reasoning are not part of that interface.

## 2026-07-22: Make reflection sessions idle-driven and incrementally active

An invocation now starts a channel session that closes after five quiet minutes
instead of using a fixed post-invocation tail. Every new human message and each
completed Multivac reply atomically moves that idle deadline and queues one
small, low-reasoning nano-model pulse. This makes the loop responsive throughout
the conversation while preserving the explicit invocation boundary.

Pulse text exists only in a bounded in-memory queue. The model returns a strict,
small structured conclusion; ordinary conversation is discarded, and only a
useful derived observation may be persisted. A final bounded history synthesis
still runs after idle close. Pulses share the existing atomic `$1.50` daily
automatic budget, use Flex only, and stop when the budget is unavailable.
