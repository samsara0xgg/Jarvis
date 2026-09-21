# Jarvis

Jarvis is Allen's private state-centric runtime.

## Working Contract

- For changes that alter cross-layer contracts, persistent state semantics,
  public interfaces, or architectural ownership, settle the design before
  implementation. Record durable architectural decisions as ADRs, in the
  format `docs/adr/README.md` defines and `scripts/check_adrs.py` asserts.
- For local changes whose behavior and boundaries are already clear, implement
  directly without creating design ceremony.
- Keep changes scoped to the requested behavior.
- Investigate the actual code path before making claims about repository behavior.
- Do not introduce abstractions or compatibility layers for hypothetical needs.
- New behavior needs observable acceptance evidence before it is considered done.
- When a change alters a documented contract, invariant, ownership boundary, or
  externally relevant behavior, update the canonical document that owns that
  fact: `docs/spec.html` for what the system must satisfy, `docs/adr/` for why
  it is designed that way. Do not document what the code already makes clear,
  and do not duplicate a fact across documents.

## Architecture

Jarvis has six layers:

L1 constitution
L2 state
L3 decision
L4 execution
L5 surface
L6 deployment

`runtime/` is the only place allowed to wire across layers.

Do not violate layer boundaries. `lint-imports` is an architectural gate.

The full architecture specification is in `docs/spec.html`. §3 is the source
of truth when sections disagree. Read the relevant section when architecture
details are needed rather than preloading the entire specification.

## Verification

- Verification must demonstrate the requested behavior, not merely make an
  existing test suite green.
- New capabilities require a real live run when the behavior depends on an LLM
  or external runtime interaction.
- Small fixes and refactors use risk-appropriate acceptance checks.
- Language-specific testing rules load from `.claude/rules/`.

## Git

- Commits are allowed.
- Never push unless the user explicitly asks.
- Never force-push `main`.
- Never bypass hooks.
- Never add `Co-Authored-By`.
- Stage explicit paths only; never use `git add .`, `git add -A`, or
  `git commit -am`.
- One logical change per commit.
- Use Conventional Commits: `type(scope): English description`.
- When creating a commit, use the project commit skill.
- `docs/git-guide.md` §2 is the detailed source of truth for unusual cases.
