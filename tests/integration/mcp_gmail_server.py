"""A stdio MCP server with Gmail's tool names: enough for Jarvis to treat it as a mail server."""

# ruff: noqa: A002, ARG001, N802, N803 — Gmail's own tool and argument names; bodies are stubs.

from __future__ import annotations

import json

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("gmail")
_READ = ToolAnnotations(read_only_hint=True)


@server.tool(annotations=_READ)
def gmail_search(query: str = "") -> str:
    """Search for emails in Gmail using query parameters."""
    return json.dumps({"messages": [{"id": "m1", "threadId": "t1"}]})


@server.tool(annotations=_READ)
def gmail_get(messageId: str, format: str = "full") -> str:
    """Get the full content of a specific email message."""
    return json.dumps({"id": messageId, "from": "Me <me@example.com>"})


@server.tool()
def gmail_send(to: str, subject: str, body: str) -> str:
    """Send an email message."""
    return "{}"


@server.tool()
def gmail_createDraft(to: str, subject: str, body: str) -> str:
    """Create a draft."""
    return "{}"


@server.tool()
def gmail_sendDraft(draftId: str) -> str:
    """Send a draft."""
    return "{}"


if __name__ == "__main__":
    server.run("stdio")
