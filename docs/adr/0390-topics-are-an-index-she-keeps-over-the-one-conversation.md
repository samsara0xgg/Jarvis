# ADR 0390 — Topics are an index she keeps over the one conversation

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- Allen, 2026-10-23 23:56Z: the chat is one conversation that goes on, with no days and no
  side talk; he wants to talk everything over with her because his Claude Code sessions each
  remember only themselves. 2026-10-24 00:25Z–00:27Z: a topic must not split her memory;
  going into a topic starts from a short recap she wrote, not from every earlier line, because
  which line belongs to which matter is often ambiguous; the lines he says inside a topic carry
  its tag; she opens the topics herself, by a rule. He said 「动手」 at 00:46Z.
- The phone's chat page (cross-device UI standard §5) lists her topics with a name, where each
  stands, how many places it was talked about and when last, opens a recap card, and steps
  through the places with ‹ ›. He was told one topic counts one place per day, and that talk
  not said inside a topic joins its topic the next morning.
- `records` in memory.db is the conversation and is never rewritten; memory.db is append-only
  except `places` (ADR 0234). Nothing today ties a matter to the lines where it was talked
  about: a day summary's `### Topics` (ADR 0142) is per day with no identity across days, and
  core memory's 生活里的几件事 (ADR 0258) holds no positions.
- The day summaries run once a day after 05:00, one call per day on `sonnet-medium`. A model
  answer citing 32-hex record ids lost a whole day to one mistyped id on 2026-10-03; core
  memory cites short labels (`r1`) that the program maps back (ADR 0146).
- The API credit is tight this month (ADR 0311), and a model call on every turn would also
  sit in front of her answer.

## Decision

Topics are an index over `records`, kept in memory.db: a topic has a short id (`t1`), a name,
a state (open or done), a one-line "where it stands" and a recap, and the records filed under
it. A message sent from a topic is filed under it at once, with her answer to it; every past
day, after its summary, one call on the day-summary preset files that day's lines, merges
duplicates, opens new topics, and rewrites the line and recap of every topic it touched.

- A record may be under several topics or none; filing never changes a record.
- The night reads the topics, the day's records with the topics they were said in, and the
  unfiled talk of the 14 days before (the day summaries' `### Topics` lines); it answers with
  short labels and the program refuses an answer that cites anything else, as core memory's
  does. A new topic needs its matter on two days or six lines of one day, and must be a thing
  (a person, a plan, a project, an event); the program drops a new topic below that bar.
- The recap is rewritten from the day's raw words and the recap before, never from a summary
  of a summary alone.
- A turn sent from a topic carries the topic's name and recap in its state block; the earlier
  lines stay in the conversation and her `topics` tool reads them.
- He renames, merges, marks done, reopens or drops a topic by telling her; a merged id still
  resolves to the topic it went into. Every change is a new version row.
- Days pass oldest first and the first run covers every day that has a summary; a day the
  night cannot answer twice stops the chain until the next run, since later days build on it.

### The `topics` tool

The need is his, in his words of 2026-10-23/24: 「我会希望以后这些全都跟 Jarvis 聊」,
「话题不是我自己创建出来的，而是 Jarvis 创建出来的」 and 「如果一开始是他自己总结好的一小段前情，
然后我们再在那个基础上聊下去」. The topics are hers, so she is the one he asks what they hold and
the one who changes them when he says so; no line of his yet asks for a rename or a merge, and
the exam questions below are to be taken from the first ones he says. The state block carries
only the topic a turn was sent from, and
`conversation` searches lines, not topics; nothing else on the menu lists, reads or changes
them. One tool, on the menu (not deferred: a rename is said in passing): `list` (the default)
and `read` read; `rename`, `merge`, `done`, `reopen` and `drop` write one topic each at his
word, L1, each answer saying what changed and the call that puts it back. A topic is named by
its id or its name; a name that fits several is an error that asks him which. The night never
renames a topic.

## Alternatives rejected

- **A conversation per topic** (side talk, as Muse and Claude Code sessions do) — splits her
  memory into pieces that cannot see each other, the one thing he named as the reason to move
  his talk to her.
- **Hand her every line of a topic when he goes into it** — the lines grow without bound and
  the lines that are ambiguous are exactly the ones a reader cannot place; he chose the recap.
- **Classify every turn as it happens with its own model call** — one more request on every
  turn, while the case where the topic is known (he sent it from the topic) needs no model.
- **Build the index from the day summaries alone, with no call** — their `### Topics` lines
  carry no identity across days, so "the same matter as on Tuesday" has nothing to match on.
- **Cite record ids in the night's answer** — one mistyped 32-hex id rejected a whole day of
  the 2026-10-03 summary backfill; labels are not mistyped.
- **One place per filed line** — a long exchange would be forty stops of ‹ ›; he was told one
  place per day.

## Consequences

- Talk not sent from a topic shows under it only the next morning.
- How well topics are cut depends on the night's prompt, which has to be tuned on his real
  conversation; a single misfiled line cannot be taken out, only its topic dropped or merged.
- The first run pays for every past day once, one call each.
- A turn sent from a topic carries about one more paragraph of state.
- `contracts/topics/` holds the list and the page around a message; the say contract takes
  `topic`.
- The tool's five tool-layer exam questions come from his real talk in the private case file on
  the Mac (`~/.jarvis/evals/tool-exam/cases.json`), which this change could not reach; they are
  written there before it lands on main.
