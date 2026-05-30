"""Module entry — ``python -m jarvis`` dispatches to :func:`jarvis.cli.main`."""

from __future__ import annotations

from jarvis.cli import main

raise SystemExit(main())
