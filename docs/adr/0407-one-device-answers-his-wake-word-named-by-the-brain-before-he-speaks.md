# ADR 0407 — One device answers his wake word, named by the brain before he speaks

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** in ADR 0219, the Mac's microphone staying open to his words while another device is in a conversation with him. Everything else in ADR 0219 stands.

## Context

- Every device that listens finds the wake word on its own: the Mac with microWakeWord
  (ADR 0042), the iPhone and the iPad in their recognizer's running words, and only while the
  app is on screen. Each holds a line to the brain: `/terminal/ws` (ADR 0172) or `/phone/ws`
  (ADR 0209). Nothing told a device that another one had heard him too.
- 2026-10-23 23:39: he talked to the iPad near the Mac. The Mac was in conversation mode,
  took the iPad's playback as his words, and the ADR 0219 check missed the iPad's own: the
  iPad's words were judged at 23:39:39.44 and its row written at 40.27, the Mac's judged at
  39.87, between the two.
- The owner, 2026-10-24: his wake word wakes one device, the one he is using or the nearest;
  and "我说了我不接受延迟", no wait he can feel. Approved the same day.
- The wake scores are not comparable across devices: the Mac's is a model probability, the
  phone's is a matched phrase with no score.
- Which device he uses is already seen on it: a touch on the phone's screen, the Mac's
  keyboard and mouse idle time.

## Decision

Let the brain name, before he speaks, the one device that answers his next wake word, and let
each device act on the name it was last told.

Its limits:

- **The named device:** the one that most recently began a conversation, for as long as it
  is in it (listening, thinking, speaking, or waiting for his follow-up; a watch talk counts for
  its phone, the Mac's conversation mode for the Mac); otherwise the one he used last, of those
  that can hear the wake word now.
- **Each device reports its part as it changes** and is told where it stands, relative to
  itself, at once and at each change: `mine` (it answers), `other` (another device he used
  later answers) or `taken` (another device is in a conversation). A brain that names nobody,
  or that predates this, leaves every device `mine`.
- **Taken:** the wake hit is dropped, the device's own conversation mode goes off, and words it
  still sends are judged `elsewhere`, with no row and no turn.
- **Other:** the phone ignores the wake word. The Mac takes the utterance unseen and shows it
  only if the brain takes it as a turn, which is when the named device missed the wake word and
  wrote no row of the same words (ADR 0219).
- **Turning conversation mode on claims the floor at once**, wherever another device stood.

## Alternatives rejected

- **A comparison window: every device that heard the wake word waits, and the brain picks
  the best** — the window has to cover the Pi's round trip and the slowest device's detection,
  and it lands on every wake word; the owner refused any felt delay on 2026-10-24.
- **The loudest or nearest device by its wake score** — the phone's wake word has no score,
  and a model probability on the Mac says nothing about distance.
- **First heard only (ADR 0219 as it was)** — the 23:39 pair was judged 0.43 s apart with
  neither row written yet: both devices took the words.

## Consequences

- While one device is in a conversation, the others are deaf to him, wake word included, until
  it ends or he turns conversation mode on elsewhere. His keyboard or a touch on the other device
  does not move the floor then.
- A phone that is `other` answers nothing if the named device missed the wake word; he says it
  again. Only the Mac covers for a miss.
- The Mac's unseen utterances still stream to Qwen (ADR 0400) and are judged before they are
  dropped: one wake word heard by two devices is two recognitions.
- A device whose line is down holds the floor until its socket closes.
- A host that is its own brain does not report: its microphone is always `mine`.
