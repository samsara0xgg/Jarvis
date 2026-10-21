"""A real stdio MCP server that stands in for a smart-home bridge in the consent tests."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("lights")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_lights() -> dict[str, list[str]]:
    """List the lights."""
    return {"lights": ["living room", "bedroom", "kitchen"]}


@server.tool()
def set_light(light: str, on: bool) -> dict[str, object]:  # noqa: FBT001 — the tool's own schema.
    """Switch one light on or off; answers with what it did. The server declares no hints."""
    return {"light": light, "on": on}


@server.tool(annotations=ToolAnnotations(open_world_hint=True))
def send_to_cloud(text: str) -> dict[str, str]:
    """Send a text to a cloud service; the server says it touches the outside world."""
    return {"sent": text}


@server.tool(annotations=ToolAnnotations(destructive_hint=True))
def wipe_bridge() -> dict[str, bool]:
    """Forget every light; the server says it is destructive."""
    return {"wiped": True}


if __name__ == "__main__":
    server.run("stdio")
