"""Load proposed behavior components against the real bot composition root."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path


async def validate(paths: list[str]) -> None:
    from discord_bot import bot
    from services.behavior_runtime import BehaviorModuleLoader, BehaviorRegistry

    registry = BehaviorRegistry()
    loader = BehaviorModuleLoader(registry, Path.cwd(), bot=bot, drain_timeout=1)
    try:
        for raw in paths:
            await loader.activate(raw, allow_overrides=True)
    finally:
        for source in list(registry.status()["sources"]):
            if source["active"]:
                await loader.unload_source(source["source_id"])
        await bot.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    asyncio.run(validate(args.paths))


if __name__ == "__main__":
    main()
