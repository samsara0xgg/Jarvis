# ADR 0147 — Jarvis keeps the reSpeaker's lights and output levels over USB

**Status:** Accepted
**Date:** 2026-10-03
**Supersedes:** none

## Context

- The reSpeaker XVF3800's LED ring is bright enough to bother Allen at the desk. On firmware 2.1.1
  the ring has six effects (off, breath, rainbow, single color, direction, one color per light),
  a brightness that only breath and rainbow use, and colors per effect. The board's headphone
  and line-out jacks have their own levels (0-9).
- None of this is reachable through CoreAudio. The board takes vendor control transfers on USB
  (resource and command ids from Seeed's `xvf_host.py`), the same channel its audio tuning uses.
- Every value lives in the board's RAM. A replug, a USB re-enumeration (one on 2026-10-03 came
  with the monitor's USB-C link) or a reboot restores the factory look, and at power-up the
  board plays a rainbow and switches to direction about 2 s later on its own.
- The board can save its whole configuration to flash (`SAVE_CONFIGURATION`). That stores
  every writable parameter, including audio routing and echo-cancellation tuning, survives a
  firmware reflash, and on fw 2.0.6 left one board unable to enumerate on USB (Seeed issue #8).
- ADR 0143 rejected detecting the board by USB vendor id for echo cancellation, because the
  CoreAudio name already identifies the microphone that is open.

## Decision

The Settings page gets a 麦克风 category for the board's lights and output levels; Jarvis keeps
what Allen saved on the board for as long as it runs.

- **Settings keys.** `board_light`, `board_brightness` (0-1), `board_speed`, `board_color`,
  `board_direction_colors` (base, toward the voice), `board_ring_colors` (12),
  `board_headphone` and `board_lineout` join ADR 0052's whitelist under `respeaker:`. They
  apply at once, like the microphone and speaker picks (ADR 0054), so they raise no restart.
- **Nothing saved, nothing written.** Until a board key is saved, Jarvis never writes to the
  board; the page shows the factory values.
- **Brightness.** Breath and rainbow use the board's brightness register. Solid, direction and
  ring have none, so their colors are scaled per channel (gamma correction is on).
- **Keeping it.** A runtime watch reads the board's registers every 2 s and writes the saved
  look only when they differ. A replug, a reboot and the boot animation all show up as a
  difference. The effect register is written last, because writing a color register can
  switch the effect.
- **USB in L5.** `jarvis/surface/respeaker_board.py` talks to the board through pyusb, with
  libusb from the `libusb-package` wheel. One lock serialises the save, the watch and the
  page's status read. CoreAudio names still decide echo cancellation (ADR 0143): USB is used
  only for the board's own controls.
- **Status.** `GET /inherent/board` answers whether the board is plugged in, its firmware, and
  its direction-of-arrival reading in board degrees. The page polls it only while the
  category is open.
- **Out of scope.** No `SAVE_CONFIGURATION`, reboot or audio-processing parameter (gain, AGC,
  noise suppression, echo cancellation, output channels) is exposed or written.

## Alternatives rejected

- **`SAVE_CONFIGURATION` to flash** — it freezes every live parameter, audio included, outlives
  a reflash, and its documented failure needs Safe Mode to recover; rewriting a few registers
  from the host costs a handful of control reads every 2 s.
- **Reapply only when the device list changes** — the ADR 0054 watch fires on a change of the
  picked or default device ids, which misses a board that resets while staying the default,
  and it fires before the board's own switch from rainbow to direction 2 s after power-up.
- **Homebrew libusb** — a fresh Mac does not have it; the wheel carries its own dylib.
- **A separate file for the board** — a second settings file in the runtime root for eight
  values the page already saves through ADR 0052's routes and whitelist.

## Consequences

- Two new dependencies (`pyusb`, `libusb-package`), loaded only when the board is used.
- Once a look is saved, Jarvis rewrites the LED registers whenever something else changes them
  (Seeed's tools, a script); clearing the board keys from `settings.json` hands them back.
- A higher `board_speed` animates faster (Allen, on the board). How `board_headphone` and
  `board_lineout` map to loudness comes from the firmware and is not checked here.
