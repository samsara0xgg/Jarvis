# ADR 0360 — A page that did not load or is walled is an error, a read page names its title, address and date, and a long page is read where his question is

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0011, `web_fetch` taking every response that is not a redirect as the page, and a page's title and stripped text being all it says about the page

## Context

- `web_fetch` takes every response that is not a redirect as the page: the text of a 403 or a 404
  page comes back as content (`_fetch_url_backend`).
- With a Tavily key, Tavily's extract reads the page first. It answers with an address and the text:
  no title, no final address, no date, no status. A paywall's teaser passes as the article.
- 2026-10-16: she set out to check an Anthropic news item and read a Seeking Alpha page that was a
  different article. Nothing in the result showed it.
- A page longer than the result's limit is cut to its first three quarters and last quarter of
  8,192 characters, so the middle of an article is dropped. `docs/spec.html#tools` forbids cutting
  text in the middle and asks for paging.
- A direct read sees the status, the address it ends at and the page's own markup: its title, its
  canonical link, its published time, and schema.org's `datePublished` and `isAccessibleForFree`.
- The description names the backend, never says "he", and says nothing of when not to use it.
- On 2026-10-21 Allen approved the plan for this tool (「就按你说的去吧」): a page that is not 200,
  or is behind a paywall, is an error that says the next step; the result gives the title, the real
  address and the published date, checked against the address asked; and the paragraphs are taken
  by what he is looking for.

## Decision

Read a page directly first, report a page that did not load or is walled as an error, and return a
read page with its title, address and date, at the part his question is about.

- **Did it load.** A page that did not load is an error that says which of these happened and what
  to do next, with its kind from ADR 0319:
  - not found (404, 410);
  - refused (401, 403, 429, 451);
  - the site failed (5xx);
  - a wall: a paywall, a bot check or a consent page, known by the page's own marks (schema.org's
    `isAccessibleForFree: false`, a challenge page, a teaser that asks him to subscribe).
- **Direct first.** The brain reads the page itself first. The extract service reads it only when
  the site refused the direct read, the page has no readable text without scripts, or it is a PDF.
  The result says which read it is.
- **Which page.** A read page carries its title, the address it ended at (its canonical address
  when it names one) and its published date when it gives one. When that address is not the one
  asked for, on another host or another path, the first line says so.
- **Where his question is.** `looking_for` takes what he wants from the page, in his words. A page
  longer than the result's limit returns its opening and then the paragraphs that match best, in
  page order, and says how many it left out. Without `looking_for` the page comes in parts
  (`page`), each cut at a paragraph.

## Alternatives rejected

- **Keep the extract service first and read the page again for its status and marks.** That is two
  requests per page, and the text and the marks can come from two different responses: a paywalled
  site can serve the extractor a teaser and the brain a refusal.
- **Judge a page by its length.** A refusal page or a paywall's teaser can run to thousands of
  characters, and a short page can be a whole note.
- **Keep the head and tail of a long page.** A question about one paragraph lands in the middle,
  which is what the cut drops; `docs/spec.html#tools` forbids it.

## Consequences

- Most pages are now read from the brain's own address, which the site sees, where Tavily saw it
  before. The egress row changes with the code.
- A wall found by mistake turns a readable page into an error. The marks are the page's own, and
  the teaser test runs against recorded pages.
- A paragraph that says what he asked in other words can be left out; the count says how many were.
