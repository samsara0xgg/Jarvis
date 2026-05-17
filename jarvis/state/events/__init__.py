"""Event Log — single source of persistent truth.

All event type schemas live in ``types/`` and are registered in ``registry.py``.
Adding a new event = (1) add Pydantic model under types/<family>.py,
(2) register in registry.py, (3) write a migration if storage shape changes.
No other layer may define event payload schemas.
"""
