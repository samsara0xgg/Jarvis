"""A real stdio MCP server the acceptance test launches as a subprocess."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("echo")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@server.tool()
def add(a: int, b: int) -> dict[str, int]:
    """Add two integers; answered as structured content."""
    return {"sum": a + b}


@server.tool()
def send(to: str, subject: str, body: str) -> dict[str, str]:
    """Send a letter; answers with what it sent (a card's letter, ADR 0061)."""
    return {"to": to, "subject": subject, "body": body}


@server.tool()
def boom() -> str:
    """Always fail."""
    msg = "boom went off"
    raise ValueError(msg)


if __name__ == "__main__":
    server.run("stdio")
