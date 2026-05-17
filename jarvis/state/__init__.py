"""L2 State Object — Event Log + Projections. The spine.

Owns:     event log (append-only, immutable), all projections (read-only fold),
          claim/evidence records (event-derived), entity registry, memory projection.
Does not: situation packet assembly, effective policy, tool execution,
          surface rendering, deployment topology.
Imports:  constitution, shared.
"""
