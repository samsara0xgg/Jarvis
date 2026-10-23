"""A stdio MCP server that lists Gmail's send and draft tools, as Google's own server does."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("gmail")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def gmail_search(query: str) -> dict[str, list[str]]:
    """Search the mailbox."""
    return {"messages": [query]}


@server.tool()
def gmail_send(to: str, subject: str, body: str) -> dict[str, str]:
    """Send a letter."""
    return {"to": to, "subject": subject, "body": body}


@server.tool()
def gmail_createDraft(to: str, subject: str, body: str, threadId: str = "") -> dict[str, str]:  # noqa: N802, N803 — Google's names.
    """Save a draft."""
    return {"id": "d1", "to": to, "subject": subject, "body": body, "threadId": threadId}


@server.tool()
def gmail_sendDraft(draftId: str) -> dict[str, str]:  # noqa: N802, N803 — Google's names.
    """Send a saved draft."""
    return {"id": draftId}


if __name__ == "__main__":
    server.run("stdio")
