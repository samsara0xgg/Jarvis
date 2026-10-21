"""The model's text under the structured spoken/written schema."""

from __future__ import annotations

import json


def structured_reply(spoken: str, written: str, *, ascii_only: bool = True) -> str:
    """The model's text under the schema: the literal ``{"spoken":...,"written":...}``."""
    return json.dumps({"spoken": spoken, "written": written}, ensure_ascii=ascii_only)
