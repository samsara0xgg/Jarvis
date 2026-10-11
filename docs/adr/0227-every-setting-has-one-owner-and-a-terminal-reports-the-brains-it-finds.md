# ADR 0227 — Every setting has one owner, and a terminal reports the brain's it finds

**Status:** Accepted
**Date:** 2026-10-10
**Supersedes:** the Settings page clause of ADR 0183: that the terminal keeps only the microphone and speaker choices and forwards the rest.

## Context

- Since ADR 0170 a brain (the Pi) and a terminal (the Mac) each read their own `settings.yaml`
  and settings page store (ADR 0052) over the shipped `config/jarvis.yaml`. The split was done
  by hand, and the only ownership knowledge in code was `_BRAIN_OVERRIDES`: the device switches a
  brain forces off.
- On 2026-10-10 the Pi lacked `observer.usage.enabled` and `onboarded`. `/inherent/usage` was a
  404, the Usage page was empty and `/inherent/setup` said `first_run: true`. Nothing warned.
  `observer.usage.enabled` defaults to false, so the brain had no way to know the owner meant it
  on: the shipped file cannot tell "left off" from "never set on this machine".
- The terminal's `POST /inherent/settings` kept the microphone and speaker and forwarded every
  other page key to the brain. A change to `wake_threshold`, `final_asr_language`, `timesink` or a
  reSpeaker `board_*` key landed on the Pi, where nothing reads it.
- Which side reads a key is a fact of the code, not of the user's wish. Where it is not obvious
  (below), the table follows what runs today.
- The ask card (`clarification.requested`, ADRs 0066, 0206) was the first candidate for asking.
  It is one slot shared with the model's own questions. Answering it runs a turn whose
  transcript is "label: value", and every turn is told while it waits (ADR 0206). Both put the
  settings question into the conversation and the model's context.

## Decision

Every setting has one declared owner in `jarvis/runtime/settings_owner.py`, and the owner
decides where it is saved, and what is warned about and asked about when two machines disagree.

- **Owners.** `brain` (only the brain reads it), `device` (only a terminal, or a machine running
  alone), `both` (each reads it for its own part), `machine` (`runtime.*`, each process's own
  wiring). The table maps key-path prefixes to owners; the longest prefix wins. An integration
  test fails if any leaf of `config/jarvis.yaml` or any Settings page key is not covered, and if
  `_BRAIN_OVERRIDES` forces off anything that is not `device`.
- **Reading code decided the unclear ones.**
  - `observer.claude_sessions.enabled` is `device`: a brain ignores its own (ADR 0225).
  - `observer.usage.*` is `both`: the terminal runs the Claude and Codex observers, the brain
    runs the provider ones and serves the page.
  - `realtime.streaming_output.*` is `both`: the terminal's media actor and the brain's phone
    voice both build from it.
  - `tools.workers.*` is `device`: a brain forces it off and a terminal never registers workers,
    so today no process reads it on a split deployment.
  - `memory.*` is `both`: the brain deletes by `audio_retention_days` and `retain_audio` is read
    by the brain's turns and the terminal's dictation.
- **A terminal reports; the brain asks.** A terminal's `hello` carries `settings`: the `brain` and
  `both` keys its own `settings.yaml` and page store set, with plain values. A key whose name
  matches `key|token|secret|pass|cred|auth` is reported as present, never its value; the shipped
  defaults are not reported. The brain compares them with its own effective config. For keys that
  differ or are missing it logs one warning naming the keys, and puts a notice (`kind: mail`,
  `mail_kind: settings`) on `GET /inherent/notices` for that device only (ADR 0218). The notice
  is not in the conversation or memory.
- **Only the owner's choice changes a file.** Through `POST /inherent/notices/{id}`:
  `device` writes the terminal's values into the brain's `settings.yaml` or page store (the file
  backed up first) and restarts the brain once voice is idle; `brain` changes nothing on the
  brain. Either answer then makes the terminal delete its own brain-owned keys (backed up
  first), so each setting has one owner. The card returns only when the reported differences
  change; they are kept in `terminal-settings.json` (0600) and dropped when the device is unpaired.
- **Each side warns at start** about keys in its own files that belong to the other side (a brain:
  `device` keys; a terminal: `brain` keys), names only, and ignores them.
- **The Settings page routes by owner.** The terminal saves `device` and `both` keys locally and
  sends `brain` and `both` keys to the brain; the page shows the terminal's value for `device`
  keys and the brain's for the rest.

## Alternatives rejected

- **Sync the files automatically in both directions.** On 2026-10-10 the two files disagreed
  on `observer.usage.enabled` and `onboarded`, and no rule could say which was stale: the Mac's
  `true` was intended, but a Pi-side `false` set on purpose would look identical. A sync that
  guesses wrong overwrites the owner's choice silently; the notice asks once.
- **A list of expected values the brain checks itself against.** `observer.usage.enabled` ships
  as false. The list would have to say "true on this brain", which is the same knowledge as the
  Mac's file, kept a second time, and it goes stale with every setting the owner changes. The
  Mac's file is the evidence the brain lacked; the table says only who owns a key.
- **The ask card for the question.** It is a single slot shared with the model's questions, and
  answering it starts a turn: the settings question and the answer enter the conversation and the
  model's context (ADR 0206's reminder line each turn). A notice does neither.
- **Keep `DEVICE_KEYS` as a hand list for the page.** It was the list that missed
  `wake_threshold` and `board_*`; a new page key would be wrong again without a failing test.

## Consequences

- Whoever adds a setting must decide its owner or the integration test fails.
- A `both` key that differs stays different after "use the brain's": the terminal keeps its
  own copy, because the terminal also reads it. The card is not raised again until the reported
  values change.
- Rewriting the brain's `settings.yaml` drops its comments and key order; the backup
  (`settings.yaml.bak-<time>`) keeps them.
- The brain restarts for a chosen change, where a supervisor restarts it; elsewhere it logs
  that a restart is needed. A restart waits for no conversation and no sound playing, and a turn
  in flight is not waited for.
- The card needs the companion to draw `choices` as buttons and send `feedback` with the chosen
  `id` as its reaction; until then the card shows its title and line and can only be dismissed.
- reSpeaker `board_*` keys are now saved on the terminal, which does not apply the board look.
- `terminal-settings.json` is one more file under the runtime root, kept like
  `terminal-tools.json` (ADR 0225).
