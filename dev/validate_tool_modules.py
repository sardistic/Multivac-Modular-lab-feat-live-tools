"""Import and contract-check trusted live tool modules in an isolated test container."""

from __future__ import annotations

import argparse
from pathlib import Path

from services.tool_runtime import ToolModuleLoader
from services.tools_registry import TOOL_REGISTRY


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("modules", nargs="+")
    args = parser.parse_args()
    root = Path.cwd()
    loader = ToolModuleLoader(TOOL_REGISTRY, root)
    for module in args.modules:
        result = loader.activate(module, allow_overrides=True)
        print(
            f"validated {module}: version={result['version']} "
            f"tools={','.join(result['tools'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
