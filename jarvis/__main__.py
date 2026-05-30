"""Package entry — ``python -m jarvis`` dispatches to :func:`jarvis.cli.main`.

Per ADR 0001 § Module map row for ``jarvis/cli``: the user-facing
invocation is ``python -m jarvis "<utterance>"``, so a top-level
``__main__`` is required (in addition to ``jarvis/cli/__main__.py``
which keeps ``python -m jarvis.cli`` working).
"""

from __future__ import annotations

import logging
import os

_LEVEL = os.environ.get("JARVIS_LOG_LEVEL", "").upper()
if _LEVEL:
    logging.basicConfig(
        level=getattr(logging, _LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

from jarvis.cli import main  # noqa: E402 — log setup must run before importing jarvis.cli

raise SystemExit(main())
