"""Tool registry for the orchestrator agent.

Tools register themselves when imported. The registry provides definitions
in Anthropic API format and executes tools by name.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class ToolDef:
    """A registered tool definition."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[str]]


class ToolRegistry:
    """Registry of tools available to the orchestrator agent.

    Usage::

        registry = ToolRegistry()

        @registry.register(
            name="my_tool",
            description="Does something useful",
            input_schema={
                "type": "object",
                "properties": {
                    "arg": {"type": "string", "description": "An argument"}
                },
                "required": ["arg"],
            },
        )
        async def my_tool(context: dict, arg: str) -> str:
            return "result"
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> Callable:
        """Decorator to register an async tool handler."""

        def decorator(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
            self._tools[name] = ToolDef(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=fn,
            )
            return fn

        return decorator

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in Anthropic API format."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]

    async def execute(
        self, name: str, tool_input: dict[str, Any], context: dict[str, Any]
    ) -> str:
        """Execute a registered tool by name. Returns the result as a string."""
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"Unknown tool: {name}"})

        try:
            # Filter tool_input to only pass params the handler accepts
            sig = inspect.signature(tool.handler)
            params = set(sig.parameters.keys()) - {"context"}
            filtered = {k: v for k, v in tool_input.items() if k in params}
            result = await tool.handler(context=context, **filtered)
            return result
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return json.dumps({"error": str(e)})

    @property
    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)


# Global registry — tools register themselves on import
registry = ToolRegistry()
