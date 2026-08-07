"""Load reviewed command Cogs against the real bot wiring without connecting."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from discord_bot import bot
from services.command_runtime import CommandModuleLoader


async def _validate(modules: list[str]) -> None:
    loader = CommandModuleLoader(bot, Path.cwd())
    try:
        for module in modules:
            result = await loader.activate(module, allow_overrides=True)
            print(
                f"validated {module}: version={result['version']} "
                f"cogs={','.join(result['cogs'])} commands={','.join(result['commands'])}"
            )
    finally:
        await bot.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("modules", nargs="+")
    args = parser.parse_args()
    asyncio.run(_validate(args.modules))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
