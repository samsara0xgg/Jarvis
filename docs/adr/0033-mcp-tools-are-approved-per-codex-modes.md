# ADR 0033 — MCP tools are approved per Codex's modes

**Status:** Accepted
**Date:** 2026-09-23
**Supersedes:** 0031

## Context

- ADR 0031 entered every MCP server once at boot on a private asyncio thread
  owned by L4 and registered each listed tool as a flat `Tool` named
  `mcp__<server>__<tool>`; that part stands. It also mapped `readOnlyHint`
  alone to L0/L1, both below the `L3` confirmation threshold, so no MCP call
  ever asked Allen, including a server's delete, send or merge.
- Spec I7 requires confirmation before deleting, sending, pushing, merging or
  changing secrets/config. GitHub's hosted server lists 45 tools, 18 of them
  writes, 13 of its catalogued tools marked `destructiveHint`; Notion's has no
  delete tool but many writes.
- The confirmation path (ADR-0012: `confirmation.requested`, exact-sentence
  grammar, single-use lease, full gate re-run) is built and wired but no tool
  reached it since `write_file` left the menu on 2026-09-12; its staging,
  question line and success line were written for `write_file` only.
- Allen, 2026-09-23: follow Codex's `auto` mode — read-only tools run, the
  rest ask first. Codex decides per call from the tool's MCP annotations and a
  mode set per server (`default_tools_approval_mode`) or per tool
  (`tools.<tool>.approval_mode`): `auto`, `prompt`, `writes`, `approve`.

## Decision

Keep ADR 0031's connection model and naming, and register an MCP tool at
`L3` with `requires_confirmation` whenever Codex's rule for its configured
mode (default `auto`) says the call needs approval, so the existing
confirmation path asks Allen before it runs; otherwise `L0` when the server
marks it read-only, else `L1`. A confirmed call re-proposes its frozen
arguments unchanged; only `write_file` stages content.

## Alternatives rejected

- **Keep L0/L1 for every MCP tool** — GitHub's `delete_file`,
  `merge_pull_request` and `push_files` would run on the model's word alone,
  breaking I7 for 18 of 45 GitHub tools on the first boot with the plugin on.
- **A per-server risk level in config** — the confirmation threshold is a
  level, so every value below `L3` changes nothing at the gate (0031's reason
  still holds) and `L3` for a whole server makes all 27 GitHub reads ask too;
  the annotation rule separates the two sets without configuration.
- **Codex's "allow for this session" / "don't ask again" answers** — the
  answer path matches exact sentences and mints a lease for one call;
  a remembered approval is a config line (`approval_mode: approve`), which
  Codex's "don't ask again" also writes.

## Consequences

A server that omits annotations has every tool ask, as in Codex. Only the
first call per turn that needs approval becomes a question; later ones in the
same turn are refused. The question line shows the tool and a 160-character
preview of its arguments, which a voice surface reads aloud. A GPT-Live turn
sees only read-only tools, so it never asks.
