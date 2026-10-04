# ADR 0148 — The Dashboard Mail Page Tells Jarvis What Is Open and Shows His Draft

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- Allen reads mail on the Dashboard and talks to Jarvis beside it. "Reply to this
  email" names a letter only the screen knows; the model sees the list through
  `gmail_search` and cannot tell which one he means.
- The state block at the head of the live message is the one place per-turn facts ride
  without moving the cached prompt prefix (spec 10.5); more than one producer will add
  lines to it (what is open, what is heard).
- A letter's body is third-party text. ADR 0123 and 0124 send Jev only the sender's name
  and the subject; the page must not widen that. The model reads a body only through its
  own `gmail_get` (ADR 0063, 0127).
- A reply is sent only through the confirmed `gmail_send` (ADR 0033). A draft on screen
  must therefore be text Allen can read and change, not a send.
- A crashed or hidden window must not leave "letter open" true for the model.

## Decision

Behind one switch, `dashboard.mail.enabled` (off: every route below answers 404, no
line reaches the model, the tool is not registered): the page posts what it has open to
`POST /inherent/focus` every 20 s and the daemon keeps it in memory for 60 s; a state-block
line names it by subject, sender and Gmail id, never a body. That line is the first
producer of `live_context`, a list of one-line `str | None` producers built in
`jarvis.runtime` and rendered at the end of the state block, each capped at 200
characters and skipped when None or raising. The page reads a letter whole
(`GET /inherent/mail/{id}`, text capped at 4000 characters, never sent to Jev), marks it
read or unread, archives it or moves it to Gmail's Trash and takes either back
(`POST /inherent/mail/{read,unread,archive,unarchive,trash,untrash}`), each only for ids
the last listing returned or the open letter. The reply draft is one in-memory record per
letter (revision, subject, body, by Jarvis or by Allen): Jarvis writes the whole body with
`write_mail_draft`, Allen edits it on the page, and while it exists under the open letter
a second live-context line carries its body (the one line allowed past 200 characters,
2000 of them), so Jarvis revises from Allen's edits. The page's Send saves the draft and
raises the confirmed `gmail_send` card itself, with no model turn (`request_confirmation`
in L3 freezes the arguments the way a model's call would); only his button sends.

## Alternatives rejected

- **A `read_open_letter` tool for the model** — `gmail_get` already returns the body and
  `threadId` inside the turn and shows the ADR 0063 mail record; a second path to the
  same bytes doubles what must be kept consistent.
- **Let the model infer the letter from "the last one I asked about"** — a session
  history cannot say which letter is on screen now, because Allen can open another
  without saying a word; only the page knows.
- **Push the focus into the system prompt** — it changes every few seconds and would
  invalidate the cached prefix on each change; the state block sits after it.
- **Permanent delete as a second button** — Allen's own tap would destroy the one copy;
  Trash keeps it 30 days and the label is the same call as archive.
- **Send by a typed turn telling Jarvis to send the draft** — a model turn costs a round
  trip and lets the model retype the body; the card is raised from the saved text.
- **Persist drafts** — a draft is the model's rewrite of the whole text each time; the
  event log already keeps every version Jarvis wrote as a `write_mail_draft` result.

## Consequences

- With replay-as-sent (`record_sent`) the focus line is stored with the turn and replayed;
  the header already labels replayed state as past, but older turns show a stale "letter
  open".
- The open letter's subject and sender, third-party text, reach the model in the state
  block, capped and flattened to one line; the body does not.
- A draft is lost on restart and a draft for a letter that is not open is refused; a
  window that stops posting for 60 s makes the model's draft call fail until it posts
  again.
- The page's archive now acts on any listed letter, not only Jev's junk offers; with the
  switch off it keeps acting only on junk. Trash has no undo here; Gmail's Trash is it.
- The draft body, Allen's own words or Jarvis's, rides every turn while its letter is open,
  up to 2000 characters.
- `request_confirmation` is a second way a card comes to exist, outside the decide loop:
  it creates the same `confirmation.requested` row, so the answer path is unchanged, but a
  card no model proposed now appears whenever the page asks.
- Each `live_context` producer runs at the start of every turn.

## Addendum: the letter's layout and a one-line summary

Still behind `dashboard.mail.enabled` (off: both are 404 or absent).

- `GET /inherent/mail/{id}` gains `layout`: `html` or `text`. `gmail_get` hands over one body,
  the text part when the letter has one, so a body with markup is an HTML-only letter. Only
  such a letter is `html`, and only when it holds a `<table>` or `<img>`, or its stripped text
  (links kept as `text (url)`) is mostly links: link characters over half of it, or links and
  under 200 other characters. Everything else is `text`. The page decides how to draw `html`;
  the daemon only labels it.
- `GET /inherent/mail/{id}/summary` answers `{summary}`: one short sentence in the UI language
  (`lang.language_name()`), written on demand from subject, sender and the same capped text
  (4000 characters), kept per id in memory (50 ids). It runs off the loop like the other
  mail routes; an unknown id or no configured model is 404, a failed call 502 and not kept.
- The call goes through the analysis path the work-state, project and daily-report jobs use
  (`LLMAnalyst`, cost accounted as `mail_summary`), on `work_state.preset` (gpt6-luna,
  gpt-6-luna at $0.10 / $0.50 per million tokens, the cheapest configured preset). It does
  not go to Jev: a body is third-party text and ADR 0123 and 0124 send OpenRouter only
  names and subjects, so the body leaves the machine only to OpenAI, as with the work state
  and `gmail_get` in a turn.
- Rejected: the surrogate route (Jev) for the summary, which would put bodies on OpenRouter;
  persisting summaries, which would keep third-party text on disk for a one-line gloss that a
  restart can write again.
