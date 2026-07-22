"""Live tool registry initialized with the checked-in built-in tools."""

from __future__ import annotations

from typing import Any, Dict, Optional

from services.tool_handlers import (
    TOOL_HANDLERS as _BUILTIN_TOOL_HANDLERS,
    list_tool_summaries,
)
from services.tool_runtime import ToolModuleLoader, ToolRegistry, ToolSnapshot
from services.tool_specs import TOOL_SPECS as _BUILTIN_TOOL_SPECS


TOOL_REGISTRY = ToolRegistry()
TOOL_REGISTRY.replace_source(
    "builtin",
    _BUILTIN_TOOL_SPECS,
    _BUILTIN_TOOL_HANDLERS,
    version="checked-in",
)

# Compatibility exports for callers that inspect the checked-in definitions.
# Runtime request paths must use get_tool_snapshot()/get_tool_specs().
TOOL_SPECS = _BUILTIN_TOOL_SPECS
TOOL_HANDLERS = _BUILTIN_TOOL_HANDLERS


def get_tool_snapshot() -> ToolSnapshot:
    return TOOL_REGISTRY.snapshot()


def get_tool_specs() -> list[dict[str, Any]]:
    return get_tool_snapshot().tool_specs()


async def execute_tool(
    name: str,
    args: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
    tool_specs=None,
    snapshot: ToolSnapshot | None = None,
):
    # tool_specs remains accepted for compatibility; a ToolSnapshot is what
    # binds a model-visible schema generation to its matching handlers.
    return await TOOL_REGISTRY.execute(
        name,
        args,
        context=context,
        snapshot=snapshot,
    )

__all__ = [
    "TOOL_HANDLERS",
    "TOOL_REGISTRY",
    "TOOL_SPECS",
    "ToolModuleLoader",
    "ToolRegistry",
    "ToolSnapshot",
    "execute_tool",
    "get_tool_snapshot",
    "get_tool_specs",
    "list_tool_summaries",
]
