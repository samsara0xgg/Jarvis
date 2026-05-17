"""L5 Surface / IO Adapters — world ↔ Jarvis boundary.

Owns:     input capture & canonicalisation, output rendering, channel-specific
          UX, local UI state, surface fallback, multimodal staging.
Does not: own truth, decide policy, verify claims, grant tool permission,
          decide agent completion.
Imports:  state (read-only), constitution, shared, surface.contracts.
          NEVER decision or execution directly.
"""
