"""Package entry — ``python -m jarvis`` dispatches to :func:`jarvis.cli.main`.

Per ADR 0001 § Module map row for ``jarvis/cli``: the user-facing
invocation is ``python -m jarvis "<utterance>"``, so a top-level
``__main__`` is required (in addition to ``jarvis/cli/__main__.py``
which keeps ``python -m jarvis.cli`` working).
"""

from __future__ import annotations

from jarvis.cli import main

raise SystemExit(main())
