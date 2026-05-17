"""Cross-layer primitives — keep MINIMAL.

Allowed: ID generation, time helpers, EntityRef / ArtifactRef types,
exception hierarchy. Anything else belongs in its owning layer.

Red line: if you're about to add a Pydantic model that represents a domain
concept (event, claim, packet, action), it does NOT belong here.
"""
