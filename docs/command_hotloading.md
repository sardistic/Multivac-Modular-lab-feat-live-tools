# Live Command Cogs

Multivac can add, replace, remove, and roll back reviewed Discord command Cogs
without recreating the bot container. Command activation uses the same signed,
read-only artifact channel as live tools and a separate persistent control
stream beneath `/state/tool-control`.

## Module contract

Each standalone module beneath `live_commands/` exports the standard discord.py
extension entry point `async setup(bot)`. Setup must add one or more Cogs and may
add commands or application commands only through those Cogs.

```python
from discord.ext import commands

COMMAND_VERSION = "1"


class GreetingCommands(commands.Cog):
    @commands.hybrid_command(name="hello", description="Say hello")
    async def hello(self, ctx):
        await ctx.reply(f"Hello, {ctx.author.display_name}!")


async def setup(bot):
    await bot.add_cog(GreetingCommands())
```

`COMMAND_VERSION` is optional. A module that acquires resources or starts tasks
must also export `async teardown(bot)` or implement cleanup in `cog_unload`.
By default Cog and command names may not replace active names. A reviewed module
may replace checked-in, non-Cog commands by declaring exact primary names with
`COMMAND_OVERRIDES = ("ping",)`. The loader restores the original command
objects on unload, failed activation, or rollback. Aliases and commands owned by
another Cog cannot be displaced. Direct mutation of the bot's command tables or
tree remains rejected because it cannot be unloaded reliably.

## Reviewed activation

A command-hotload proposal must contain only standalone
`live_commands/*.py` modules. It cannot mix command modules with tools,
documentation, tests, providers, or core code.

The host supervisor revalidates the patch, runs the full networkless suite, and
loads every changed module against the real bot wiring in a second restricted
container. It then commits the patch, publishes the modules in a signed
content-addressed artifact, and sends an exact path and SHA-256 batch to the
running bot.

The bot removes the prior Cog version, installs the new version, and performs
one global application-command sync after the whole batch. If setup or Discord
tree synchronization fails, every mutated source is rolled back and the prior
tree is synchronized again. Existing invocations retain their old callback
objects while new invocations use the replacement Cog.

Active command sources are stored in `active-commands.json` and restored and
synchronized after future restarts. Proposal-specific rollback restores the
previous signed artifact or unload state. Deleting a live command module unloads
its Cog and synchronizes its removal.

## Local development

When `MULTIVAC_TOOL_HOTLOAD_DIR` is configured without supervisor control, the
owner can use the prefix-only administrative command:

```text
/command_hotload status
/command_hotload load hello.py
/command_hotload reload hello.py
/command_hotload unload hello.py
/command_hotload rollback hello.py
```

Direct mutation is disabled when `MULTIVAC_TOOL_CONTROL_DIR` is configured. In
local mode, an optional final `true` authorizes declared overrides.

## Limitations

Command modules are trusted in-process Python, not sandboxed workers. Event,
intent, provider, and runtime-setting changes use the separate managed behavior
component channel. Bootstrap, dependency, and database changes remain on the
full release-and-restart path. The infrastructure was deployed to production on
2026-07-22; a harmless owner-approved command proposal is still needed to verify
live global tree synchronization and unload against Discord.
