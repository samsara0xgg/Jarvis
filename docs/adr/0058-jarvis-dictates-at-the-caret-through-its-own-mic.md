# ADR 0058 — Jarvis Dictates at the Caret Through Its Own Mic

**Status:** Superseded-by-0077
**Date:** 2026-09-26
**Supersedes:** none

## Context

- Typlus (`~/Projects/typeless-local`) is Allen's dictation app: F5 or a tap
  of the right ⌘ records, local Whisper hears, a model polishes the words
  with a fixed prompt, and ⌘V pastes them into the app in front. On
  2026-09-26 he asked for the same thing as a small tool inside Jarvis,
  with the companion ball carrying it, and for Typlus to stay as it is:
  "保留原来的 typlus，你直接在 jarvis 做一个小工具".
- The look was settled in the 钻过去 lab (artifact GzXa22kH, v3): she
  crouches and slips into the notch while the island folds after her,
  comes up through a hole beside the text caret, listens there, dives back
  in as the words appear, and slides out of the notch again. His picks that
  day: 30 px across, the tilted-head listening face, and the same skin as
  the ball in the notch.
- The daemon is the single owner of the mic (spec §3.6.5). Its audio
  ingress already lets one more capture lane subscribe to the same
  echo-cancelled 16 kHz frames, and its SenseVoice recognizer serialises
  decodes behind its own lock: a 10.6 s clip decoded in 0.15 s warm on
  2026-09-26. The wake word listens to the same mic, so "Jarvis" said while
  dictating would open a voice turn.
- API keys live with the daemon (Keychain, ADR 0009); the companion never
  holds them.
- Typlus's polish on gpt-5.4-mini, 2026-09-12, same 10 s clip: 0.74 s
  median, 10 of 10 on task; gpt-5.6-luna without thinking answered the
  dictation as a question in 4 of 5. Through Jarvis on 2026-09-26 the same
  model took 0.9 to 2.0 s.
- Only the companion is a native Mac process with a place for native code
  (its N-API addon). Reading the caret and posting ⌘V need Accessibility.
  Ghostty's text area reports a selection range but no position for it, so
  its caret cannot be found that way.

## Decision

The companion owns the key, the caret and the paste, and the daemon owns
the recording, the hearing and the polish: a clean tap of the right ⌥
starts a session, the next one finishes it and Esc cancels; the daemon
records a capture lane on its own ingress, holds the wake word off meanwhile,
hears with the voice path's recognizer and corrections, and polishes with
Typlus's prompt on its own side-job preset; the companion pastes where the
caret is, or copies the words when there is no text box to paste into.
Nothing of a dictation is written to the event log or memory.db.

## Alternatives rejected

- **The companion records with `getUserMedia`** — a second process on the
  mic outside the ingress: no echo cancellation against Jarvis's own voice,
  its own device choice beside ADR 0054's, and the wake word would still
  hear the dictation because only the daemon can hold it off.
- **Typlus's local Whisper (large-v3-turbo) in the daemon** — `mlx-whisper`
  is deliberately undeclared in `pyproject.toml`, and the model keeps about
  1.6 GB resident beside SenseVoice, which is already warm and decodes a
  10 s clip in 0.15 s.
- **The same keys as Typlus (F5, right ⌘)** — both tools would start on one
  tap and paste twice while Typlus stays installed.
- **Starting on right-⌥ down** — right ⌥ + a letter types a symbol; only a
  release with no key typed in between (read without any input permission
  from the HID system state) is a tap.
- **The conversation model (`luna`) for the polish** — it answered 4 of 5
  dictations instead of writing them down (above).

## Consequences

- The Jarvis tool hears only while the daemon's voice stack runs; with the
  voice off the route is absent and she says so at the caret.
- A long dictation decode holds SenseVoice's lock, so a wake turn right
  after it waits for it.
- Where Accessibility shows no caret (Ghostty, most Electron apps) she comes
  up at the text box's bottom-right corner, not beside the words; with no
  focused text box, beside the mouse, and the words are copied.
- The companion process needs Accessibility; until macOS grants it, every
  dictation ends as copied text.
- Names SenseVoice does not know ("Typlus", "星核") come back misheard,
  and only the polish can mend them, with the word list read from Typlus's
  own folder; if Typlus moves that file, the tool loses the list.
