"""Jarvis tool framework — @jarvis_tool decorator + global registry."""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, get_type_hints

_TOOL_REGISTRY: dict[str, dict[str, Any]] = {}

# Execution context — set by ToolRegistry.execute before each call.
# Provides user_id/user_role to tool functions without polluting their signatures.
_EXECUTION_CONTEXT: dict[str, Any] = {}

_TYPE_MAP = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
}


def jarvis_tool(
    func: Callable | None = None,
    *,
    read_only: bool = True,
    destructive: bool = False,
    required_role: str = "guest",
) -> Callable:
    """Register a function as a Jarvis tool.

    Supports both ``@jarvis_tool`` and ``@jarvis_tool(...)`` syntax.
    Reflects type hints to build an OpenAI-compatible tool definition.

    The tool definition uses ``input_schema`` key (matching existing Jarvis
    conventions).  Function parameters with defaults are optional; without
    defaults are required.  Parameters named ``self`` or ``cls`` are excluded.

    The ``_execute`` wrapper:
    - Receives ``(tool_name, tool_input, **context)`` from ToolRegistry
    - Extracts matching params from *tool_input*
    - Does basic type coercion (str -> int, str -> float)
    - Calls the original function with extracted kwargs
    """

    def _decorator(fn: Callable) -> Callable:
        hints = get_type_hints(fn)
        sig = inspect.signature(fn)
        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []

        for name, param in sig.parameters.items():
            if name in ("self", "cls"):
                continue
            ptype = _TYPE_MAP.get(hints.get(name, str).__name__, "string")
            properties[name] = {"type": ptype, "description": ""}
            if param.default is inspect.Parameter.empty:
                required.append(name)

        definition = {
            "name": fn.__name__,
            "description": fn.__doc__ or "",
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

        def _execute(tool_name: str, tool_input: dict[str, Any], **context: Any) -> Any:
            kwargs: dict[str, Any] = {}
            for pname, param in sig.parameters.items():
                if pname in ("self", "cls"):
                    continue
                if pname in tool_input:
                    val = tool_input[pname]
                    expected = hints.get(pname, str)
                    if expected is int and not isinstance(val, int):
                        val = int(val)
                    elif expected is float and not isinstance(val, float):
                        val = float(val)
                    kwargs[pname] = val
                elif param.default is not inspect.Parameter.empty:
                    kwargs[pname] = param.default
            return fn(**kwargs)

        _TOOL_REGISTRY[fn.__name__] = {
            "definition": definition,
            "execute": _execute,
            "read_only": read_only,
            "destructive": destructive,
            "required_role": required_role,
        }

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return wrapper

    if func is not None:
        return _decorator(func)
    return _decorator
