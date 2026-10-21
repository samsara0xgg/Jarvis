"""L1 Constitution — the product identity label.

The constitution itself (C1..C6, docs/spec.html#constitution; C1 per ADR 0090) and the
product's non-goals (docs/spec.html#positioning) are written in the spec, their only copy:
no code read the constants this module once mirrored them in. Every higher layer may read
the label; the layer imports nothing from this codebase.
"""

from __future__ import annotations

from typing import Final

JARVIS_IDENTITY: Final[str] = "Jarvis"
"""Product identity label. Owned by L1 per ADR § Identity."""
