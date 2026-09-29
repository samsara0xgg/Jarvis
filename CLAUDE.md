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

The specification is `docs/spec.html` (v2): what a public build must satisfy,
cited by anchor (`docs/spec.html#egress`), never by `§`. `docs/spec-v1.html`
is the archived v1: the `§` numbers in code written before ADR 0089 point
there, as background, not contract. Read the relevant chapter when
architecture details are needed rather than preloading the entire
specification.

## Verification

- Verification must demonstrate the requested behavior, not merely make an
  existing test suite green.
- New capabilities require a real live run when the behavior depends on an LLM
  or external runtime interaction.
- Small fixes and refactors use risk-appropriate acceptance checks.
- Language-specific testing rules load from `.claude/rules/`.
- In a fresh checkout or worktree, run `bash scripts/init.sh` first (`--fix`
  repairs); until it is green, a red gate or a deaf daemon may be environment
  drift, not code.

## Git

- When a task is done and verified, land it without asking first: commit,
  integrate into `main` without new merge commits, restart what it touches, and push `main`
  (`docs/git-guide.md` §3). A permission prompt on the way is fine; stopping
  to ask in chat is not. Stop only on a red gate, a merge conflict you cannot
  resolve mechanically, or work Allen said to keep off `main`.
- Push only as plain `git push origin main`: its permission prompt is
  Allen's approval, and a declined push stays local. Never force-push `main`.
- Never bypass hooks.
- Do not add an AI assistant, tool, or model as an author, co-author, or
  contributor unless Allen explicitly requests it for that commit.
- Stage explicit paths only; never use `git add .`, `git add -A`, or
  `git commit -am`.
- One logical change per commit.
- Use Conventional Commits: `type(scope): English description`.
- When creating a commit, use the project commit skill.
- `docs/git-guide.md` §2 is the detailed source of truth for unusual cases.
