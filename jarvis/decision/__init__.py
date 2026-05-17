"""L3 Runtime Decision — judgement layer.

Owns:     Situation Packet assembly, Effective Policy, Intent Routing
          (Tier 0/1/2), Attention Policy, Pre-action / Pre-emit gates,
          Result Interpreter, ResponsePlan.
Does not: own truth (reads projection snapshots), execute tools,
          render surfaces, hold state across invocations.
Imports:  state, constitution, shared. NEVER execution or surface.
"""
