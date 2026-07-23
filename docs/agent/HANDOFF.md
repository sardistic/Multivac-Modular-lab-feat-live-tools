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
- Fixed proposal baseline resolution so a detached running release reads the
  shared canonical branch, and successful promotion advances that local ref.
- Corrected the control channel from runtime-only `0700` ownership to a private
  `2770` setgid directory shared by the deploy supervisor and runtime group.
- Activated the first owner-approved production tool proposal, `falcon-juniper`,
  through the signed no-restart channel as registry generation 2.
- Invoked its `hotload_deployment_smoke` handler through Discord, received the
  exact expected JSON result, then rolled proposal 11 back as generation 3.

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
- Production-image gate: 160 unittest cases passed with 7 expected skips in a
  networkless, read-only, capability-dropped container.
- Live verification: the container runs commit `3f71745` with zero restarts;
  Discord reached ready state and synchronized 11 application commands.
- `/app` and `/tool-artifacts` are read-only mounts, artifact content is readable
  by UID 65532, and `/state/tool-control` is writable by UID/GID 65532.
- The proposal-supervisor timer is enabled and active; its latest service result
  and the dashboard service result are successful. Both run as `sardistic` with
  supplementary state group 65532.
- Production has zero approved proposals awaiting deployment. Existing approved
  proposals all have recorded release deployments, so no live channel can be
  exercised without a new owner-reviewed proposal.
- Regression coverage creates a detached release whose `HEAD` differs from
  `main` and proves proposal creation still records the canonical commit. It also
  verifies supervisor validation and promotion use and advance that same ref.
- Expanded full suite: 156 passed, 7 skipped, 84 subtests passed.
- Corrected production-image gate: 163 unittest cases passed with 7 expected
  skips in the networkless, read-only, capability-dropped container.
- Live verification at `290599d`: Discord reached ready state, synchronized 11
  commands, and retained zero restarts. From inside the detached container,
  `get_baseline_sha()`, `HEAD`, and `refs/heads/main` all resolved to `290599d`.
- After the second tool smoke proposal failed closed on control-directory access,
  production permissions were repaired without restarting the bot and verified
  writable by both the `sardistic` supervisor identity and runtime UID 65532.
- Expanded suite after the shared-control regression test: 157 passed, 7
  skipped, 84 subtests passed.
- Shared-control production-image gate: 164 unittest cases passed with 7
  expected skips. An explicit systemd reconcile then succeeded as `sardistic`,
  preserved mode `2770`, and both supervisor and runtime access checks passed.
- Promoting the host-only fix did not recreate the Discord container: its start
  timestamp and zero restart count remained unchanged while its canonical
  baseline advanced to `b9839c0`.
- `falcon-juniper` passed revalidation, the networkless suite, artifact signing,
  digest verification, registry activation, health checking, and canonical Git
  promotion. The active SHA-256 matches the immutable artifact and audit row.
- Tool activation did not recreate or restart the container. Its original start
  timestamp remains unchanged, and no new Discord gateway connection appeared;
  the only activation log records generation 2.
- Proposal-specific rollback emptied `active-tools.json`, recorded an explicit
  generation-3 unload, marked the deployment `rolled_back`, retained the signed
  immutable artifact, and caused no restart or gateway reconnect.
- `git diff --check` passed; Git emitted only LF/CRLF normalization warnings.
- Existing dependency-version and Python `audioop` warnings remain.

## Implementation details

The complete tool, command, and behavior hotload stack, deployment-gate fixes,
canonical-baseline fix, and shared-control fix are pushed through `b9839c0`. The
supervisor keeps SQLite/control state under `/tmp`
inside networkless, capability-dropped validation containers while retaining
fail-closed ownership enforcement for production state. The production-image
gate then exposed shared outer state in the dynamically imported supervisor test
fixture; its state root is now explicitly isolated per test.
The tool, command, and behavior guides now accurately distinguish the deployed
infrastructure from the still-pending end-to-end proposal smoke tests.
The canonical-baseline fix and its regression tests are deployed.
The supervisor's persistent initialization contract now preserves the shared
setgid control-directory permissions; this host-side fix does not require a bot
restart.
That persistence fix and its regression test are deployed in the host control
checkout; the live directory is repaired and verified.
Canonical commit `177311a` adds `live_tools/deployment_smoke.py`; its handler
returned the requested static smoke result while active. The source is now
unloaded, while its signed artifact remains retained for audit and restoration.

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
- Live command and behavior activation paths have not yet been exercised against
  harmless production proposals. The complete tool path—review, validation,
  signing, activation, invocation, promotion, unload, and rollback—is proven.
- Database schema/data changes remain restart-only and require forward-compatible
  migrations; code rollback alone cannot reverse persisted mutations safely.

## Next concrete action

Activate a harmless command proposal, verify global tree sync and invocation,
then unload it without a reconnect. Repeat with a harmless behavior component,
including lifecycle teardown and rollback.

## Deployment/status impact

Deployed on 2026-07-22. Production runs commit `290599d` from
`/srv/multivac-releases/manual-baseline-fix-290599d`. The immediately prior
`3f71745` release remains at `/srv/multivac-releases/manual-hotload-0c5e723`,
with `27f18e7` also preserved at the earlier rollback worktree. The host
supervisor includes the `b9839c0` permission fix, and the active tool proposal
promoted canonical commit `177311a`. Proposal 11 is now rolled back at runtime;
all promotions and its unload left the Discord container on immutable core
release `290599d` without a restart.
