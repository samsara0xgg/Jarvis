"""Step 5 smoke: confirm ADR-0003 deps installed and import cleanly."""

from __future__ import annotations

import fastapi
import uvicorn


def test_fastapi_importable() -> None:
    """FastAPI must import for Step 7's create_app to compile."""
    assert hasattr(fastapi, "FastAPI")


def test_uvicorn_importable() -> None:
    """Uvicorn[standard] must import for Step 8's serve_inherent to compile."""
    assert hasattr(uvicorn, "Config")
    assert hasattr(uvicorn, "Server")
