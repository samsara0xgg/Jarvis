"""L4 Capability Execution — hands.

Owns:     tool registry, adapters, worker execution, device calls, sandbox,
          AuthorizationLease enforcement, RawResult, ActionLifecycle 8-state,
          artifact spill.
Does not: decide permission (policy is L3), own task truth, self-verify
          completion, render natural-language response.
Imports:  state, constitution, shared. NEVER decision or surface.
"""
