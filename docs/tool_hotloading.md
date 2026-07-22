# Live Tool Modules

Multivac can replace its model-callable tool schemas and handlers without
disconnecting the Discord bot. Hotloading is explicit: the bot does not watch a
writable directory or automatically execute files that appear on disk.

## Security boundary

A live tool module is trusted Python running inside the bot process. It can see
the bot's credentials, databases, network, and imported application modules.
The loader validates the tool contract and confines paths to one configured
directory, but it is not a sandbox.

`MULTIVAC_TOOL_HOTLOAD_DIR` points at `/tool-artifacts`, a supervisor-controlled
directory mounted read-only into the bot container. User uploads, generated
drafts, and other bot-writable paths are never used as the hotload root. The
existing review, isolated test, signing, and owner-approval flow is the only
automatic publisher for that directory.

## Module contract

Each module exports `TOOL_SPECS` and `TOOL_HANDLERS`. Their names must match
exactly. `TOOL_VERSION` is optional; the content hash is used when it is absent.

```python
TOOL_VERSION = "1"

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "say_hello",
            "description": "Return a greeting.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    }
]


async def say_hello(args):
    return {"ok": True, "greeting": f"Hello, {args['name']}"}


TOOL_HANDLERS = {"say_hello": say_hello}
```

Handlers may be synchronous or asynchronous. They should be import-safe and
must not start unmanaged background tasks. Lifecycle hooks and isolated worker
execution are intentionally deferred to a later phase.

Supervisor-hotloaded modules must be standalone `.py` files beneath
`live_tools/`. A proposal containing live-tool files cannot contain any other
changes. Tools do not replace existing names by default. A reviewed module may
declare exact replacements with `TOOL_OVERRIDES = ("tool_name",)`. Declarations
must match active conflicts exactly, and unload or rollback restores the prior
handler. An existing live source can update its own names because its logical
source ID remains stable across artifacts.

## Activation

The release Compose override configures the artifact and control mounts:

```text
MULTIVAC_TOOL_HOTLOAD_DIR=/tool-artifacts
MULTIVAC_TOOL_CONTROL_DIR=/state/tool-control
```

For local development without `MULTIVAC_TOOL_CONTROL_DIR`, the owner-only text
command can operate exact paths relative to a configured development directory:

```text
/tool_hotload status
/tool_hotload load say_hello.py
/tool_hotload unload say_hello.py
/tool_hotload rollback say_hello.py
```

Direct development activation can replace another source only with an explicit
final `true` argument. Direct mutation is disabled when supervisor control is
configured, and supervisor activation never overrides checked-in tools.

Every activation creates a new immutable registry generation. A model request
captures its schema and handler generation before calling the provider. If a
reload occurs while that request is running, its eventual tool calls finish on
the old handlers; subsequent requests see the new version.

## Reviewed proposal activation

For an approved patch that changes only valid `live_tools/*.py` modules, the
host supervisor:

1. Revalidates the patch and runs the complete networkless test suite.
2. Imports each changed module in a second restricted container to validate its
   runtime contract and conflicts with checked-in tools.
3. Creates the audited Git commit.
4. Copies only the tool modules into a content-addressed artifact directory,
   records each SHA-256, and signs the canonical manifest with the host key.
5. Writes a bounded activation request into `/state/tool-control` and waits for
   the running bot to report the matching registry generation.
6. Verifies Discord remains healthy, promotes the canonical branch, and records
   both the new and previous tool state for proposal-specific rollback.

Activation batches are atomic. If any module fails to load or its digest does
not match, earlier mutations in that batch are rolled back. Active sources are
persisted in `active-tools.json` and restored after future container restarts.
Artifacts required by active versions and recent rollback history are retained.

Live command Cogs and behavior components have equivalent isolated channels in
`docs/command_hotloading.md` and `docs/behavior_hotloading.md`. Mixed and core
proposals continue through the existing release worktree and container
recreation path.
Deploying this infrastructure itself requires that normal one-time restart;
later tool-only and command-only proposals do not disconnect Discord.

## Current deployment status

The runtime, supervisor publisher, read-only mount, persistent control protocol,
and rollback path were deployed to production on 2026-07-22. Startup, mount
permissions, supervisor automation, and the isolated production-image suite are
verified. A harmless owner-approved tool proposal is still needed to exercise
live activation and unload end to end.
