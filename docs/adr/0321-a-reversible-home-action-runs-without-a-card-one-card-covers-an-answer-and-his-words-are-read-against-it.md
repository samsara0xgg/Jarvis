# ADR 0321 — A reversible home action runs without a card, one card covers an answer, and his words are read against it

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0062, the clauses that a newer ask always replaces the waiting card, that the model revises only by proposing again, and that the ask names only the tool and the result is the tool's ack line; in ADR 0033, taking a plugin tool's hints only from its server. The rest of both stands, as does ADR 0012's lease.

## Context

- 2026-10-17 09:40: he asked her to turn the lights off, and the lights stayed on.
  - She made three `set_light` calls. The first made a card. The other two got "gate refused",
    because only a turn's first ask makes a card.
  - He said 「都关掉快点」. The grammar (`config/confirm_grammar.yaml`) matches exact sentences
    only: 「是」 but not 「是的」, 「执行吧」 but not 「执行」. His words went to the model.
  - The model's pending note says to call the same tool again, which replaces the card and asks
    once more (`packet.py`). The old card then answered stale.
  - The clearer his yes, the more often he was asked.
- Hue's write tools carry no MCP hints. Under ADR 0033 a missing hint counts as the risky value, so
  they are L3 cards (`mcp_tools.py`, `needs_approval`), though they are local and undone by the next
  call. The Hue plugin's manifest approves only its six reads.
- On 2026-09-26 Allen decided that a reversible small action runs without a card (ADR 0062,
  Context). ADR 0062 left Hue to a per-plugin mode. On 2026-10-21 he approved the plan that turns
  reversible home actions into actions without a card (「就按你说的去吧」).
- The card's question names only the tool: 「要{action}吗？」 (`confirm.ask_tool`). It names neither
  the lights nor the change.
- After a card is accepted she never sees the result:
  - On success she says a fixed ack line (`confirm.tool_ran`). A new issue's number or a page's
    link has to be looked up again.
  - On failure she says the raw error code (`confirm.dispatch_error`).
- L4 dispatch checks a lease only when one is present (`_emit_dispatched` in
  `jarvis/execution/tools.py`). A confirm-required call that reached dispatch without one would run.
  L2's outbox validates the leases it is given.
- ADR 0039's hazard still holds: 「好」, said to a later question, once fired an earlier ask. Words
  answer a card only on the first utterance after it, within `confirmation.ttl_ms` (ADR 0062).
- ADR 0012's invariant: no model output, under any phrasing, can cause an L3 dispatch. Only the
  grammar and his button mint a lease.

## Decision

Decide whether a call needs a card from its declared effect. Put every call in one answer that needs
one on a single card. Read the words the grammar cannot match with a closed-choice classifier. Give
her the real result of an accepted call.

Its limits:

- **The effect decides.**
  - Local and reversible: no card (L1). Outward or irreversible: a card (L3).
  - A plugin's manifest declares the MCP hints its server leaves out. Hue's writes are not
    destructive and touch nothing outside his home.
  - His per-plugin and per-tool approval modes still override (ADR 0033).
- **One card per answer.**
  - Every call in one model response that needs consent goes on one card, listed one by one.
  - Accepting mints one lease per call (ADR 0012: tool and target, one use, an expiry). Dismissing
    rejects them all.
  - A later ask replaces the waiting card only if it differs. Proposing the same calls again leaves
    the card as it is.
  - The pending note's "call the same tool again" sentence goes.
- **His words.**
  - The grammar still answers first, as ADR 0012 D6 says, on the first utterance after the card
    appears or changes, within `confirmation.ttl_ms`.
  - Words it does not match go to a classifier on a fast preset (haiku today). It sees only his words
    and the card's question lines, and picks one of: accept, reject, change, unrelated, not sure.
  - Only accept mints the leases, and code mints them from the frozen card: words never change an
    argument. A change or an unrelated answer runs as an ordinary turn, where she may propose anew.
    Not sure, a timeout or an error leaves the card waiting for the button.
  - Every classification is an event.
- **The question** comes from the tool's card template and names the objects and the change
  (「关掉客厅和卧室的灯？」). A plugin tool with no template names its arguments in plain words.
- **After accept**, the calls' results go back to the model for one more request, and she says what
  happened.
- **L4 refuses** a call to a tool that requires confirmation when it carries no valid lease (I7).
- ADR 0012's invariant becomes: no output of the conversation model can cause an L3 dispatch. A lease
  comes only from his button, his exact words in the grammar, or the classifier's accept of his
  words.

## Alternatives rejected

- **Widen the grammar.** 「都关掉快点」, 「是的」 and 「执行」 are three misses from one morning. Each
  miss puts the card back in front of him.
- **Ship Hue with `approval_mode: approve` on its writes.** The approval mode is his preference
  (ADR 0033), and shipping it in his place fixes one plugin. The next local plugin is wrong again
  under the same rule. A declared effect is a fact about the tool, and his mode still overrides it.
- **Let the conversation model read his answer.** That is today's path: it re-proposed and replaced
  the card. It also costs a full turn. The classifier sees only his words and the card.
- **Keep one card per turn and refuse the rest.** That is the 10-17 run: the refused calls were
  retried, and each retry was a new ask.

## Consequences

- A wrong effect in a manifest runs an outward action with no card. Manifests are reviewed as code.
- Words the grammar misses cost one classifier request, which is a fraction of a cent, and add a
  wait before the card answers.
- An accepted card costs one more request of the conversation model.
- A model call can now help mint a lease. It sees only his own words and the card, never outside
  material. As with the grammar, anyone who speaks near the microphone counts as him (ADR 0090).
