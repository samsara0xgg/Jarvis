"""Composition root — the only place allowed to wire across layers.

If you find yourself importing from multiple layers anywhere else, you have
likely placed code in the wrong layer. Bring the wiring here instead.
"""
