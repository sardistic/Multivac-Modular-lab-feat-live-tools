# Agent Handoff

## Active objective

Review and install the completed no-restart runtime for tools, Discord commands,
events, intents, providers, and runtime settings while retaining explicit
restart boundaries for bootstrap, dependencies, and persistent-data migrations.

## Completed work

- Added immutable, request-scoped tool snapshots and digest-verified standalone
  tool loading with atomic activation, persistence, unload, and rollback.
- Added managed Discord Cog activation with one tree sync per batch, sync-failure
  rollback, restart restoration, and original-command restoration.
- Added exact `TOOL_OVERRIDES` and `COMMAND_OVERRIDES` declarations. Reviewed
  modules can replace checked-in symbols only when the declared scope matches;
  originals return on failure, unload, or rollback.
- Added a behavior component runtime for event, intent, provider, and runtime
  setting namespaces. Each Discord event captures one immutable generation, and
  nested routing/provider/setting lookups reuse it.
- Routed the message, raw-reaction, and command-error event shell, exact intent
  dispatch, principal OpenAI/Gemini/Claude chat paths, image/vision paths, and
  Sora/Veo entry points through the behavior registry.
- Added async component setup and health checks, old-generation request draining,
  stop signals, tracked task cancellation, tracked client/View cleanup, teardown,
  bounded history, persistent active state, and rollback.
- Added a third persistent supervisor stream for `live_components/*.py`, plus
  isolated validation, signed content-addressed artifacts, no-restart activation,
  rollback, audit state, and retention alongside tool and command artifacts.
- Protected all tool, command, and behavior activation-authority/control/validator
  files from generated proposals. Each hotload kind must be a standalone proposal;
  mixed hotload kinds and core files are rejected.
- Added owner-only local controls for tools, commands, and behaviors; direct
  mutation is disabled whenever supervisor control is configured.
- Documented all three module contracts, lifecycle rules, override declarations,
  security boundaries, restart-only boundaries, and architectural decisions.

## Current behavior

- `live_tools/*.py` changes model-visible schemas and handlers atomically.
- `live_commands/*.py` changes managed Cogs and synchronizes the Discord command
  tree. Explicit declarations can temporarily displace checked-in direct commands.
- `live_components/*.py` changes exact events, intents, provider entry points,
  and settings. New events see the new generation while prior events finish on
  the old generation before its resources are torn down.
- Activation requests contain exact artifact-relative paths and SHA-256 digests.
  Active sources survive later full releases and process restarts.
- Setup, validation, health, batch activation, or Discord sync failures preserve
  or restore the prior persistent projection.
- Dependencies/native modules, database migrations, Discord token/intents,
  logging/event-loop bootstrap, mounts, the gateway shell, and hotload authority
  remain normal release-and-restart changes by design.

## Validation performed

- Compiled every changed runtime, control worker, validator, routed bot module,
  supervisor, and `discord_bot.py` with `python -m py_compile`.
- Behavior tests cover exact override authorization, immutable settings, reload,
  rollback, state restoration, failed-batch rollback, owned-task shutdown, and an
  in-flight old request draining after new traffic switches generations.
- Command tests cover built-in displacement/restoration and Discord tree-sync
  rollback. Tool tests cover declared built-in replacement and restoration.
- Supervisor/policy tests cover behavior-only detection, isolated branch routing,
  protected authority files, and mixed-kind rejection.
- Full suite: 153 passed, 7 skipped, 84 subtests passed.
- `git diff --check` passed; Git emitted only LF/CRLF normalization warnings.
- Existing dependency-version and Python `audioop` warnings remain.

## Uncommitted implementation details

The complete tool, command, and behavior hotload stack is committed and pushed
at `0c5e723`. A follow-up supervisor fix keeps SQLite/control state under `/tmp`
inside networkless, capability-dropped validation containers while retaining
fail-closed ownership enforcement for production state. The production-image
gate then exposed shared outer state in the dynamically imported supervisor test
fixture; its state root is now explicitly isolated per test.

## Unresolved risks

- All hotloaded modules are trusted code executing inside the credential-bearing
  bot process. Review and isolated validation reduce risk but do not sandbox them.
- Lifecycle cleanup is enforceable only for resources registered through the
  component context; a trusted module can still create unmanaged global state.
- Provider overrides must preserve the documented fallback call signature.
- A component replacing the full `message` event owns command pass-through and
  must call `bot.process_commands` when appropriate.
- Discord command activation depends on external global tree sync availability
  and rate limits; failed sync is locally rolled back and followed by restorative
  sync, but the flow has not been exercised against live Discord.
- Production activation has not occurred. A rollback worktree at the prior
  `27f18e7` release and a candidate worktree at `0c5e723` are staged on the host;
  the candidate must pass the corrected isolated host gate before activation.
- Database schema/data changes remain restart-only and require forward-compatible
  migrations; code rollback alone cannot reverse persisted mutations safely.

## Next concrete action

Commit and publish the isolated-validation fix, rerun the networkless host gate,
then activate the staged candidate and verify startup, mounts, ownership, and
release state. Afterward activate harmless tool, command, and behavior proposals;
confirm no Discord gateway reconnect, exercise one in-flight behavior replacement,
verify command tree sync, unload each source, and confirm all built-ins return.

## Deployment/status impact

Not yet active. Production still runs `27f18e7`; rollback is staged at
`/srv/multivac-releases/manual-rollback-27f18e7` and candidate code is staged at
`/srv/multivac-releases/manual-hotload-0c5e723`.
