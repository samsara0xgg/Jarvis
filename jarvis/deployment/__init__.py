"""L6 Deployment Domains — physical ownership + cross-domain transport.

Owns:     domain ownership, process placement, local durability,
          cross-domain transport, offline behaviour, artifact stores,
          backup / replay / migration, sleep/wake protocol.
Does not: state semantics, policy rules, tool permission,
          claim interpretation, product identity.

Current scope: Mac single domain. transport/ + domains/rpi.py reserved
for future RPi expansion.
"""
