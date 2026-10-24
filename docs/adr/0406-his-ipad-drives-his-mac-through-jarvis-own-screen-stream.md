# ADR 0406 — His iPad drives his Mac through Jarvis's own screen stream

**Status:** Accepted
**Date:** 2026-10-24
**Supersedes:** none

## Context

- On 2026-10-24 Allen asked to control his always-on Mac from his iPad inside the Jarvis app:
  the same picture as the Mac's screen, the same control (clicks, drags, scrolling, keys), no
  third-party app. He wants to unlock the Mac from the iPad and to have the Mac's own displays
  stay black while he works (a curtain). He said 「动手」 to that plan the same night.
- iPadOS has no built-in way to control a Mac. Universal Control and Sidecar are driven from the
  Mac; Apple's High Performance Screen Sharing is Mac-to-Mac only. An iPad VNC client gets the
  basic encodings: soft when scrolling or playing video, no audio.
- His Mac's lock is "immediately": a sleeping display is a locked Mac. FileVault is on, so after a
  restart nothing remote reaches the pre-boot screen.
- Measured on his Mac on 2026-10-24 (a dev build run from a process that holds Screen Recording
  and Accessibility): ScreenCaptureKit captures the lock screen; events posted with CGEvent at
  the HID tap reach the lock screen's password field (three characters typed showed three dots,
  then were deleted unsubmitted). A loopback client received the main display at 2704×1756,
  H.264, about 30 frames a second while the lock screen animated, first frame 3 s after
  connecting from a sleeping display.
- The tailnet already encrypts and authenticates every device (WireGuard); the Pi reached the
  Mac's port over it and was refused on a wrong token.

## Decision

A small Mac app of Jarvis's own, Jarvis Remote, streams the Mac's main display to the iPad app
and posts the iPad's input as local events; it takes connections from the tailnet and loopback
only, behind a pairing token.

- The stream is ScreenCaptureKit into a low-latency VideoToolbox H.264 encoder, sent as
  length-prefixed frames over one TCP connection; the iPad decodes into a display layer. A link
  that falls behind drops frames until the next keyframe.
- Unlocking is the iPad typing his login password into the lock screen and pressing return. The
  password lives only in the iPad's Keychain and travels only inside the tailnet connection.
- The curtain is a black window over every display; the stream leaves out Jarvis Remote's own
  windows, so the Mac shows black and the iPad shows the desktop. While a session lives the
  display is kept awake; when it ends the display sleeps, which locks the Mac again.
- It is not part of the companion (desktop/resonance) and does not go through the brain: it is
  a separate signed app with its own Screen Recording and Accessibility grants.
- Limits: the main display only; no audio; one session at a time (a new one ends the old).

## Alternatives rejected

- **Apple's Screen Sharing with a VNC client in the app.** Its iPad-reachable encodings are the
  basic VNC ones (Screens 5 documents no audio "due to VNC protocol limitations"), and the
  maintained open-source Swift client, RoyalVNC, was archived on 2026-10-05 with its iOS view
  unfinished.
- **Borrowing Apple's Screen Sharing only to unlock.** The measurement above shows the lock
  screen takes posted keys, so a second always-open port (5900, reachable from the whole LAN)
  and a second connection at every unlock buy nothing.
- **Moonlight with Sunshine, or RustDesk.** GPL-3.0 and AGPL-3.0 respectively, and Sunshine's
  macOS host is still labelled experimental in its own docs.
- **WebRTC.** Its value is NAT traversal and congestion control across the internet; the tailnet
  already provides the path, and one TCP connection between two of his devices needs neither.
- **Living inside the companion.** The companion is Electron with a native addon and its own
  owner and release cadence; a screen-capture and input-injection process restarting with it would
  drop his session whenever the companion is updated.

## Consequences

- Jarvis Remote must be granted Screen Recording and Accessibility by Allen at the Mac once, and
  re-granted if its signing identity changes.
- Anything on the tailnet that steals the pairing token can see and drive the Mac; the token is
  the only gate past the tailnet's own.
- The bitrate is fixed for the home network; driving it from outside on a thin link will stutter
  until the encoder adapts to the link.
- Windows on a second display are invisible from the iPad until a display picker exists.
