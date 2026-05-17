"""Projections — read-only derived beliefs folded from events.

Each projection: rebuildable from event log; never directly mutated by
LLM / tool / surface. Mutation is always emit_event → fold.
"""
