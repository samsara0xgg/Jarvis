# Jarvis — Allen 的私人 state-centric runtime

## Rules

- Discuss first, write code later.
- Commit OK, **never push** unless explicitly asked.
- **No Co-Authored-By** in commit messages.
- Don't violate layer boundaries — `lint-imports` enforces them.

## Architecture

6 layers (see `docs/spec.html` for the full spec):

```
L1 constitution   L2 state      L3 decision
L4 execution      L5 surface    L6 deployment
```

`runtime/` is the only place allowed to wire across layers.
