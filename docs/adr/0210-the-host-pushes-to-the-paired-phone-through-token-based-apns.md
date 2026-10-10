# ADR 0210 — The host pushes to the paired phone through token-based APNs

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** none

## Context

- Allen, 2026-10-10 (「手机上得要把所有功能都给它接进去，一次性来吧」): the phone must get every
  feature at once. Four of them need the host to reach a phone whose app is closed:
  - a reminder that rings with the Mac's lid shut (a brain, ADR 0170);
  - a notification that opens a link when tapped, which the transit feature will use for Uber;
  - "something is waiting for you" when no phone app is open: a confirmation, an ask card or a
    Claude Code permission prompt;
  - the app's Live Activity: her state and the next reminder in the Dynamic Island.
- A phone does not listen, and the system wakes a suspended app only for a push (ADR 0197). Apple
  Push Notification service (APNs) is the only path to it. A Live Activity is updated only by an
  APNs push to that activity's own push token.
- APNs takes HTTP/2 only. It authenticates the sender either by a provider certificate, which is
  per app and expires after a year, or by an ES256 JWT signed with a `.p8` key, which serves every
  app of the team and does not expire. It refuses a JWT older than 60 minutes and one replaced
  more than once in 20.
- A debug build registers a device token with the sandbox endpoint and a TestFlight or store build
  with the production one; a token sent to the other endpoint is refused as `BadDeviceToken`.
- The repo keeps API keys in the login Keychain on macOS and in the 0600 `env` file elsewhere, both
  read by `load_env_file` (ADR 0009), and never on a command line.
- A reminder ignores the quiet level and is written as `reminder.fired` before anything is shown or
  said, so it rings exactly once (ADR 0179). The quiet level hides cards from `no-pop` up (ADR 0153).
- Confirmations and ask cards are event-log rows. A Claude Code permission prompt is held in the
  daemon's memory (ADR 0049) and is not.
- What the phone is told (a reminder's words, a card's question) leaves the host for Apple.

## Decision

The host sends Apple push notifications to each paired phone, signed with Allen's `.p8` key and
addressed by the tokens that phone registered under its own device token, and does nothing of the
kind until a key is configured.

Its limits:

- **Credentials.** Key ID, Team ID and bundle id are settings (`push:`). The `.p8` is a 0600 file
  under the runtime root named by `push.key_file`, or the environment variable `APNS_AUTH_KEY`,
  which `load_env_file` fills from the Keychain or the `env` file. A group- or world-readable file,
  one outside the root, and a key that is not P-256 switch pushes off. The JWT is signed in
  process, reused for 50 minutes, and dropped at once when APNs calls it expired.
- **Never logged.** The key, the JWT, a device or Live Activity token, and a notification's text
  appear in no log line, argument list or error. A failure is logged as the device's name, what was
  sent and APNs's own reason word.
- **Endpoint per token.** A phone registers `sandbox` or `production` with its tokens, and the
  host sends each token to that endpoint.
- **Registration.** A paired device registers its device token, and its Live Activity token while
  one runs, with a request only its own device token opens; the local key is refused, as for the
  phone's events (ADR 0197). The tokens are kept per device beside `devices.json` and removed by
  unpair.
- **Four triggers.**
  1. A fired reminder, pushed after `reminder.fired` is written, so a push that fails cannot ring
     it again. It is not pushed when it is more than 12 hours late, as it is not spoken then.
  2. A confirmation, an ask card or a Claude Code permission prompt that starts waiting, once per
     item, and not to a phone that has its `/phone/ws` conversation socket open (ADR 0209).
  3. `Push.send(title, body, url, category, thread)` for any feature that wants a tappable
     notification.
  4. The Live Activity content, sent when her state or the next reminder changed.
- **Quiet.** Reminders push at every level, as they ring at every level. A waiting item pushes with
  sound at `off`, as a silent banner at `quiet`, and not at all from `no-pop`, where cards are
  hidden. Nothing pushed during `no-pop` or `dnd` is sent later.
- **Failure.** A 410 drops the token it answered. Any other refusal or network error is logged and
  that push is over: nothing is retried, so an APNs outage cannot ring a reminder twice.

## Alternatives rejected

- **A provider certificate instead of the `.p8`.** It is valid for one year, and the day it
  lapses every push fails with no change in the code; the `.p8` has no expiry and serves every app
  of the team.
- **A relay such as ntfy or Pushover.** None can address the app's Live Activity, which only an
  APNs push to that activity's token updates, and each puts Allen's reminder text through a third
  party as well as Apple.
- **Local notifications scheduled by the app from a synced reminder list.** A reminder the brain
  cancels or moves after the sync still rings, because iOS does not wake a suspended app on request
  to withdraw it; the host can only withdraw what it can push.
- **Retrying a failed push.** A timeout does not say whether Apple took the push, so a retry may
  ring twice; ADR 0179's guarantee is one ring, written before anything is shown.
- **Pushing from the code that makes each card.** The confirmation, the ask card and the hook
  prompt come from three layers, and each would import the sender. Two of them are rows in the log,
  which one watcher reads; the third has one callback.

## Consequences

- The text of a reminder, a card and the Live Activity passes through Apple's servers. It is a new
  row in `docs/spec.html#egress`, and the Claude prompt's push names the tool and the folder, never
  its input.
- A card is pushed within about 5 seconds of being asked, and one asked while the daemon is down, or
  already waiting at boot, is never pushed.
- A permission prompt is held only while a companion reads the board (ADR 0049), so the phone alone
  does not make the host hold one, and a prompt that is not held cannot be pushed.
- "Open" is the host's own record of live `/phone/ws` connections (ADR 0209), read when the item
  is first seen. A phone that connects a moment later still gets nothing for that item, and one
  whose socket is open but whose app is in the background gets no push either.
- A pushed reminder that APNs does not take is lost to the phone, and remains a card on the Mac.
- Whoever holds a paired device's token can register an APNs token of their own and receive what is
  pushed to that device name.
- `httpx[http2]` and `cryptography` become direct dependencies, and httpx's request line and the
  HTTP/2 header codec's debug lines are silenced for pushes, since both would write a token.
