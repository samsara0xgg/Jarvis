# ADR 0077 — Dictation Hears With Local Whisper

**Status:** Accepted
**Date:** 2026-09-27
**Supersedes:** 0058

## Context

- ADR 0058 built Jarvis's dictation beside Typlus, which stays as it is:
  the companion ball carries it, the daemon is the single owner of the mic
  (spec §3.6.5) and records a capture lane on its ingress, the wake word
  listens to the same mic, API keys live with the daemon (ADR 0009), and
  only the companion can read the caret and post ⌘V (Accessibility; Ghostty
  reports no caret position). It heard with the voice path's SenseVoice.
- On the 60-clip set (Allen's own recordings, 2 to 107 s, graded by
  gpt-5.6-sol against what he sent, two blind passes), the same polish on
  top of SenseVoice needed fixing in 46 of 60, on top of Typlus's Whisper
  large-v3-turbo in 39, and gemini-3.8-flash hearing and writing in one step
  in 26. Most of SenseVoice's extra misses are English and product names.
- On the stretches ADR 0076 cuts, Whisper's character error rate against
  what he sent was 0.116 / 0.302 / 0.390 (2-10 / 10-40 / 40+ s) against
  SenseVoice's 0.149 / 0.322 / 0.431 and whole-clip Whisper's 0.114 / 0.341
  / 0.532, and it left 0.50 / 0.47 / 0.61 s after the stop at the median
  against SenseVoice's ~0.1 s. With the word list in its prompt it looped in
  2 of the 60 and did worse on longer clips: 0.389 / 0.486.
- The model keeps ~1.6 GB resident and takes a few seconds to load.
  mlx-whisper requires torch (used only to convert checkpoints) and numba;
  the packaged app does not ship it, and Typlus's hardened bundle died
  importing it.
- On 2026-09-27 Allen was offered local Whisper or Gemini and answered
  "whisper".

## Decision

The companion owns the key, the caret and the paste, and the daemon owns
the recording, the hearing and the polish: a clean tap of the right ⌥
starts a session, the next one finishes it and Esc cancels; the daemon
records a capture lane on its own ingress, holds the wake word off
meanwhile, hears with local Whisper large-v3-turbo in Chinese with the
simplified-Chinese prompt when mlx-whisper is installed and with the voice
path's SenseVoice when it is not, applies the voice path's corrections, and
polishes with Typlus's prompt and word list on its own side-job preset; the
companion pastes where the caret is, or copies the words when there is no
text box to paste into. Nothing of a dictation is written to the event log
or memory.db.

## Alternatives rejected

- **SenseVoice for dictation (ADR 0058)** — 46 of 60 needed fixing against
  39 with Whisper under the same polish.
- **gemini-3.8-flash** — 26 of 60, but 2.67 s median for a whole clip, paid
  per dictation, the audio leaves the Mac, and 5 of 120 calls came back cut
  off by the provider's filter; Allen chose the local model.
- **The word list in Whisper's prompt** — loops in 2 of 60 and 0.389 /
  0.486 against 0.300 / 0.389 on the longer clips; the list stays with the
  polish.
- **Declaring mlx-whisper in `pyproject.toml`** — puts torch in the lock
  and the packaged app for a conversion path the daemon never runs.
- **The companion records with `getUserMedia`** — a second process on the
  mic outside the ingress: no echo cancellation against Jarvis's own voice,
  and only the daemon can hold the wake word off.
- **The same keys as Typlus (F5, right ⌘)** — both tools would start on one
  tap and paste twice while Typlus stays installed.
- **The conversation model for the polish** — gpt-5.6-luna answered 4 of 5
  dictations instead of writing them down (2026-09-12).

## Consequences

- The daemon holds ~1.6 GB more from boot; a dictation in its first seconds
  waits for the load, and Typlus, when running, holds its own copy.
- Each stop leaves ~0.5 s of hearing instead of ~0.1 s.
- A fresh venv, a worktree or the packaged app hears with SenseVoice and
  misses more names; the daemon log says which ears it has. mlx-whisper's
  version is not locked: `uv sync --inexact` keeps it, a plain `uv sync`
  removes it.
- The tool hears only while the daemon's voice stack runs, the companion
  needs Accessibility to paste, and in Ghostty she comes up at the text
  box's corner; names the recognizer misses are mended only by the polish.
