# ADR 0340 — A terminal whose microphone did not open opens it again, only after a confirmed close

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** none

## Context

- A voice terminal (ADR 0172) built its capture session once. When the microphone did not open
  it logged one error and spoke without listening until someone restarted it: a microphone
  another process holds stays held, and trying again in a loop would only fight it.
- On 2026-10-21 at 04:01 a restart came up deaf for three hours. The first open failed, its close
  was confirmed (`state=stopped reason=shutdown_complete`), nothing held the microphone, and no
  one tried again. The daemon had the same gap on its first open, but its device watch re-reads
  a microphone that goes down later after 2, 5 and 15 s, then every 30 s (ADR 0054).
- The failing open left no cause in the log: the failure's capability snapshot was never
  published, and `import sounddevice` points fd 2 at `/dev/null` while PortAudio initialises, so
  every line logged meanwhile was lost.
- ADR 0006 allows one input owner. A foreign open that timed out may still own the device, and a
  second owner beside it fights it; the session's close says whether it is known to be free.

## Decision

A terminal whose capture session did not start opens it again when the failed attempt's close
confirmed the microphone free, and never otherwise.

- After a confirmed close: again after 2, 5, 15 s and then every 30 s (ADR 0054's schedule),
  at once when the Mac wakes or the devices to be on change; the first success ends the retry.
- After a close that was not confirmed: no second open, whatever wakes the terminal, and an
  error that says why and that a restart is needed.
- A failure before any device was reached (a model that will not load, a configuration that is
  refused) is not retried: it does not change by waiting.
- Whatever leaves the terminal unable to listen is logged by the terminal itself, at warning or
  above, with the capability state and reason, or the failure that stopped it.

## Alternatives rejected

- **Retry whatever the close said** — on 2026-10-21's class of failure a timed-out foreign open
  can still own the device; `SoundDeviceDuplexBackend` answers a second backend with
  `OWNER_BUSY` while that debt stands, so a blind retry would only be refused until it clears,
  and would open a second owner if the close were wrong.
- **Retry only on wake and on device change** — the 04:03 failure had neither; a Mac that
  stays awake with its devices unchanged would stay deaf.
- **Retry a failure before the device too** — a missing model fails the same way every 30 s
  and writes a traceback each time; nothing in the first run suggests a later one differs.
- **Leave the retry to a restart supervisor** — KeepAlive restarts only an exited process, and
  this one is up, speaking, and serving the UI.

## Consequences

- A microphone another process holds is tried every 30 s for as long as the terminal runs, and
  each failed try writes its warning.
- The recognizer's models load once; a try builds the wake engine and the session again.
- A deaf terminal runs the power observer and a device poll it did not run before; a listening
  one hands them to its coordinator instead.
- A device picked on the Settings page while the terminal is deaf is kept for its next start;
  the tries use the devices it started with.
- The daemon's first open is still not retried.
