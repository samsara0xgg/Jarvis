"""A stdio stand-in for Playwright MCP: its tool names and its snapshot format.

The snapshot lines follow real `@playwright/mcp@0.0.82` output: a filled field
shows its value after the ref, or on a child line when it has its own
placeholder; iframe refs carry an `f<N>` prefix. Every call that reaches the
page is appended to the file named by argv[1], so a test can prove a refused
call never arrived.
"""

from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

LOG = Path(sys.argv[1])
PAGES = {
    "http://shop.test/cart": """### Page
- Page URL: http://shop.test/cart
### Snapshot
```yaml
- generic [ref=e1]:
  - textbox "Email" [ref=e4]: allen@example.com
  - textbox "Password" [ref=e6]:
    - /placeholder: Enter your password
  - button "Sign in" [ref=e7]
  - textbox "Note" [ref=e8]: "[ref=e13]"
  - textbox "Promo code" [active] [ref=e9]
  - button "Place your order" [ref=e13]:
    - img [ref=e17]
  - button [ref=e14]:
    - generic [ref=e15]: Buy Now
  - button "Continue" [ref=e16]
```""",
    "http://shop.test/signed-in": """### Page
- Page URL: http://shop.test/signed-in
### Snapshot
```yaml
- generic [ref=e1]:
  - textbox "Password" [ref=e6]:
    - /placeholder: Enter your password
    - text: hunter2secret
```""",
    "http://shop.test/pay": """### Page
- Page URL: http://shop.test/pay
### Snapshot
```yaml
- generic [ref=e1]:
  - button "Continue" [ref=e2]
  - iframe [ref=e3]:
    - generic [ref=f1e1]:
      - textbox "Card number" [ref=f1e3]
```""",
}
page = ["http://shop.test/cart"]
server = MCPServer("browser")


def _reached(line: str) -> str:
    with LOG.open("a") as f:
        f.write(line + "\n")
    return f"### Ran Playwright code\n{line}"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def browser_snapshot() -> str:
    """Capture accessibility snapshot of the current page."""
    return PAGES[page[0]]


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def browser_find(text: str) -> str:
    """Search the snapshot of the current page for text."""
    hits = [line for line in PAGES[page[0]].splitlines() if text.lower() in line.lower()]
    return f"Found {len(hits)} matches" if hits else "No matches"


@server.tool()
def browser_navigate(url: str) -> str:
    """Navigate to a URL."""
    page[0] = url
    return _reached(f"goto {url}")


@server.tool()
def browser_click(target: str, element: str = "") -> str:
    """Click an element."""
    return _reached(f"click {target} {element}")


@server.tool()
def browser_type(target: str, text: str, element: str = "", submit: bool = False) -> str:  # noqa: FBT001, FBT002 — Playwright's own argument shape.
    """Type text into an element."""
    return _reached(f"type {target} {text} {element} submit={submit}")


@server.tool()
def browser_press_key(key: str) -> str:
    """Press a key."""
    return _reached(f"press {key}")


@server.tool()
def browser_evaluate(function: str) -> str:
    """Evaluate JavaScript on the page."""
    return _reached(f"evaluate {function}")


if __name__ == "__main__":
    server.run("stdio")
