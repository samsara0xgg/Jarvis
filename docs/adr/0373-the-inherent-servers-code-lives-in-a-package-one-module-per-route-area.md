# ADR 0373 — The inherent server's code lives in a package, one module per route area

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- `jarvis/surface/inherent_server.py` held the whole HTTP and WebSocket face of the daemon: the
  Host and token checks, the body caps, the request bodies, the `InherentDeps` object the runtime
  fills, and 125 route decorators inside `create_app`, a function of about 960 lines. The file was
  3,463 lines and took 18 commits in the eight days to 2026-10-21, so any two route changes
  collided in it.
- Most routes read one or two fields of `InherentDeps` and nothing from one another. The few
  things areas share are small: the mapping of a failed call to 404, 400 or 502, the check of who
  is asking, the body cap, and `start_turn` (submit, ask and share). One object is built inside
  `create_app` and read by non-adjacent routes: the Claude Code board with its hook state.
- A route is tried in the order it was registered and listed in that order in the OpenAPI
  document, and the v2 input gate joins the middleware stack at the point the v2 routes register.
  The local-key middleware is added by the runtime after `create_app` returns, so it sits outside
  whatever the factory adds. The order is part of the app's behaviour.
- A test that drives a route patches the module attribute the code reads (`_ASK_WAIT_S`,
  `time.time` for the Codex board), so where a name lives is part of its test seam. 73 files
  construct `InherentDeps`, and the runtime builds it from about 96 arguments in
  `runtime/daemon/deps.py`.
- The six layers are enforced by `lint-imports`; a split inside `surface/` changes no layer
  contract. ADR 0339 did the same for the daemon and ADR 0371 for the turn.

## Decision

The inherent server's code lives in `jarvis/surface/inherent_server/`, one module per route
area, and a new route goes in the module that owns its area, not in the app factory.

- `app.py` holds only `create_app`: the order the areas register in, which is the order routes are
  tried and listed. Each area module exposes `register_*(app, deps)`, and a route exists only
  when the deps it needs are wired, as before.
- `deps.py` holds `InherentDeps` with the shape it had. `guards.py` holds the Host check, the
  local key, a paired device's token and the body cap. A request body lives in the module of the
  route that reads it, and a Protocol the runtime fills lives with the area that serves it.
- The package takes the module's name. Nothing in `__init__.py` imports a module, so an import
  written for the single file fails when it runs.
- A name another module imports is public. No module re-exports another's names, and every caller
  imports from the module that defines the name. A module's logger is
  `logging.getLogger(__name__)`.
- A move changes no behaviour: the code is moved verbatim, a route keeps its registration
  position, and the tests that exercise it patch the module that now reads the name.

## Alternatives rejected

- **Per-area deps dataclasses in the same change** — it changes how `runtime/daemon/deps.py` builds
  the object and how 73 files construct it, so a green run no longer shows that the move changed
  nothing; the unchanged tests, imports apart, are the acceptance evidence for a pure move.
- **Keep `inherent_server.py` and put the routes in a package beside it (the ADR 0339 shape)** —
  what would remain is `create_app` and the guards, under the name every old import resolves, so
  a branch written before the split would keep importing the thin module without a failure;
  the package takes the file's name and that import fails.
- **One module per route** — 125 decorators would be about 100 modules, and routes of one area
  share a body model and a deps field (`mail_read` and `mail_archive` are nested), so the
  modules would import one another for what they share.
- **Build the Claude board and the hook state in the runtime in the same change** — two readers
  would then share an instance the app builds today, and who owns it is a behaviour a test of the
  board can see; that is a wiring change, not a move.
- **Re-export the moved names from `jarvis.surface.inherent_server`** — a test that patches a
  re-exported name patches only the alias, and the moved code reads its own module's attribute, so
  the patch is silent; CLAUDE.md also rules out a compatibility layer.

## Consequences

- Log lines from moved code carry the new module's logger name, under
  `jarvis.surface.inherent_server`: a level set on that logger still applies by inheritance, a
  filter attached to it no longer sees them. The tests that read the lifelog and the refused-key
  lines name `...inherent_server.lifelog` and `...inherent_server.guards`.
- The private helpers several areas share are public in the module that owns them: `bearer_token`
  (formerly `_v2_presented_token`, which was never v2-specific), `is_loopback_peer`,
  `capped_body`, `paired_device`, `reader_of`, `for_reader`, `notices_for`, `no_notices`,
  `home_call`, `start_turn` and `checked_attachments`.
- `/inherent/waiting` reads the Claude board that `register_claude_routes` builds, and sits
  between two of that function's routes, so `agents.py` calls `waiting.py` in the middle of its
  registration. Splitting that apart needs the board built elsewhere first.
- `deps.py` and the area modules import each other for annotations only (under
  `TYPE_CHECKING`); nothing cycles when the package loads.
- FastAPI reads a route's annotations from the module's globals when the route is registered, so
  an annotation-only import it needs stays a runtime import; four imports carry a justified
  `noqa: TC001` or `TC002` for that.
