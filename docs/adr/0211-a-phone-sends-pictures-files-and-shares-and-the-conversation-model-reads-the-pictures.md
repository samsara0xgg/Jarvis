# ADR 0211 — A phone sends pictures, files and shares, and the conversation model reads the pictures itself

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- On 2026-10-10 Allen asked for every phone feature at once (「全要」). The app's composer gets a
  panel with camera, photos and files, and the iOS share sheet sends her links, text and images
  from any other app.
- A turn carries text only. `POST /inherent/image-submit` is a 501 stub, the v2 capability
  `image_input` is false, and the decision model never sees pixels: `screen_look` sends the
  image to a second preset and returns text.
- The model that answers and the model that reads screenshots are the same one. The conversation
  preset `luna` and the vision preset `gpt6-luna` both name `gpt-6-luna` (`config/jarvis.yaml`),
  and the file's comments show it reading screenshots as well as the dearer models.
- The two presets differ in how they are called:
  - `luna`: chat/completions, no reasoning, first token about 1 s (replay of 2026-09-25);
  - `gpt6-luna`: `/v1/responses` with medium reasoning, about 5 s to read one screenshot.
- `gpt-6-luna` costs $0.10 per 1M input tokens, $0.01 cached, $0.50 per 1M output
  (`data/pricing.json`). A turn makes up to 40 model requests (8 when he spoke), and each one
  carries everything on the turn's user message again.
- The request has four parts and nothing else, and whatever is read into a conversation goes to
  the model's provider with the next request (`docs/spec.html#request`, `#no-telemetry`).
- The event log keeps references to big content, not the content (`docs/spec.html#event-log`).
- `pyproject.toml` and `uv.lock` hold no PDF reader and no image library (0 matches), so nothing
  here can resize a picture or read a PDF without a new dependency. The model's image input
  takes PNG, JPEG, WebP and GIF, not the phone's HEIC.
- The guard every route already has opens the daemon to the local key on loopback and to a
  paired device's token from anywhere else (`docs/spec.html#local-endpoints`).

## Decision

Give an attached picture to the conversation model itself, as image parts of that turn's user
message, and describe it to nobody first. Its limits:

- **What is accepted.** The kind is read from the file's first bytes, never from the type the
  sender claims.
  - Images: PNG, JPEG, WebP, GIF, up to 4 MiB each, the cap a terminal's screenshot has
    (ADR 0170). The app shrinks a photo to 1568 px wide first, as `screen_look` does.
  - Text: UTF-8 without control characters, up to 256 KiB. The model gets its first 20,000
    characters, marked as his material and not as instructions.
  - Everything else, PDF and HEIC included, is refused with a message that says why. No
    extractor is added: none is in the dependencies.
- **Per turn.** At most 4 attachments and 8 MiB of images. A turn with an attachment always goes
  to the model, never to Tier 0 or Jev.
- **The model must take images.** Images are accepted only while the default conversation
  preset is OpenAI's and declares `images: true`; otherwise they are refused when uploaded.
  Text attachments do not depend on it.
- **Later turns.** Pixels ride only the turn they arrive with. After it, history holds his words,
  one marker line naming each attachment, and her answer; the memory record carries the same
  marker, so search and the day summary know a picture was sent.
- **Storage.** Files live in `artifacts/attachments/` under the runtime root, never the repo,
  and are kept like recordings and screenshots: deleted after `attachments.retention_days`
  (30) by the same hourly sweep, in the export, and cleared with them. The directory holds at
  most 512 MiB; an upload past that is refused.
- **Upload.** `POST /inherent/attachments`, multipart, one file per request, answers an id.
  The body is capped before it is read. `POST /inherent/submit` takes an optional
  `attachments: [id]`. The v2 submit and socket carry none.
- **A share from another app** is `POST /inherent/share`: a link, text or an image, each with an
  optional note, and `ask`.
  - `ask` true: it is one ordinary turn whose words are the note and what was shared.
  - Otherwise it is kept as one `user.shared` event. Every later turn's state block carries one
    line with the latest five shares of the last three days, folded from the log, saying they
    are his saved material and not a request. Nothing else is built: no list, no reader.
  - Text over 4,000 characters is refused, so the event stays small.
- **Egress.** The picture, or the head of a text file, goes to the conversation provider in the
  turn's user message and nowhere else. `docs/spec.html#egress-table` gains the row.

## Alternatives rejected

- **Describe it first with the vision preset, as `screen_look` does.** It loses on three
  counts, none of them price.
  - The vision preset is the same `gpt-6-luna`, so the description adds no better reader, only
    a lossy step between the pixels and the answer.
  - It is serial: about 5 s for the read before the turn's first request can start, against
    about 1 s from the request to the first token.
  - The description is written before the question is known, so "what does the sign on the left
    say" in the same turn's tool loop cannot be answered from it.
  - Price is a tie and is not the reason: even 5,000 image tokens on all 40 requests of a turn
    cost $0.02 uncached and $0.002 cached at the rates above, and (b) pays for a second
    request on top.
- **Keep the pixels in history for later turns.** History is text built from the records so its
  front stays byte-identical for the provider's cache. A 4 MiB file is about 5.6 MB of base64,
  which every later request would send again.
- **Add a PDF library now.** `grep -i pdf uv.lock pyproject.toml` finds nothing, and none is added
  speculatively. Refusing with a clear message costs nothing that exists today.
- **Make every share a turn.** A link saved to read later would start a spoken answer. The
  share sheet already says which he means.

## Consequences

- A later turn cannot look at the picture again. "And the sign on the left?" a turn later gets
  only her earlier words. A tool that re-reads the stored file through the vision preset is the
  next step and needs its own decision.
- A saved image share is a marker: she can say one exists, not what it shows. Sent again with
  `ask` she can see it.
- Every request of an image turn sends the image again; the provider caches it, and a 4 MiB
  file costs about 5.6 MB of uplink per request when it does not.
- Pictures leave the machine the way screenshots do. A default preset without `images: true`
  turns picture upload off until the setting says otherwise.
- A stolen device token can fill the 512 MiB store and start turns; it could already start
  turns.
- The stub `POST /inherent/image-submit` is gone, and `image_input` in the v2 hello now says the
  upload route works, not that the v2 submit takes images.
