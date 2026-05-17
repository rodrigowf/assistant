"""Tool registry for the orchestrator agent.

Tools register themselves when imported. The registry provides definitions
in Anthropic API format and executes tools by name.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


SchemaBuilder = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class ToolDef:
    """A registered tool definition.

    ``schema_builder`` lets a tool refresh its declared schema at
    serialization time — e.g. populate an ``enum:`` from live state that
    isn't known at import time (the MCP server list, available agent
    sessions, etc.). It receives the registered ``input_schema`` and must
    return a (possibly new) schema dict. When ``None``, the static
    schema is used verbatim.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    schema_builder: SchemaBuilder | None = None


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
        schema_builder: SchemaBuilder | None = None,
    ) -> Callable:
        """Decorator to register an async tool handler.

        ``schema_builder`` is invoked every time the registry serializes
        tool definitions, so it can inject live state (e.g. the current
        MCP server list as an ``enum``) into the declared schema.
        """

        def decorator(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
            self._tools[name] = ToolDef(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=fn,
                schema_builder=schema_builder,
            )
            return fn

        return decorator

    def _resolve_schema(self, tool: ToolDef) -> dict[str, Any]:
        """Return the schema to advertise, applying ``schema_builder`` if set."""
        if tool.schema_builder is None:
            return tool.input_schema
        try:
            return tool.schema_builder(tool.input_schema)
        except Exception:
            logger.exception(
                "schema_builder for tool %r failed; falling back to static schema",
                tool.name,
            )
            return tool.input_schema

    def get_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in Anthropic API format."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": self._resolve_schema(tool),
            }
            for tool in self._tools.values()
        ]

    def get_openai_definitions(self) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI function calling format (for Realtime API)."""
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": self._resolve_schema(tool),
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
