# contracts/

What the brain and its two clients (the iPhone app, the Mac companion) say to each other, one
JSON file per message. The files are the contract: the brain's tests hold the brain to them, and
each client's tests read the same files, so a rename on one side is red on the other
(ADR 0325, the contract level; the wire itself is `docs/spec.html#local-endpoints`).

## Files

| Folder | Message | Direction |
| --- | --- | --- |
| `phone_events/` | `POST /inherent/device/events`: the batch (`frame`), each `phone.*` row type, the answer (`ack`) | both |
| `phone_ws/` | `/phone/ws` frames: `hello`, `say`, `ready`, `said`, `step`, `error` | both |
| `waiting/` | `GET /inherent/waiting`, the two cards, and the bodies that answer them | both |
| `day/timeline.json` | `GET /inherent/day` | brain to client |
| `settings/` | `GET` and `POST /inherent/settings` | both |
| `about.json` | the `about` a phone sends with a say or a submit | client to brain |

A file that the brain sends has `route` (or `frame` and `socket`), `direction`, `description`,
`now_ms` (the stopped clock of the world it was made in), `exact` and `example` or `examples`.
A file that a client sends has `valid` and `invalid` lists (each `invalid` case names its
`refusal`) in place of examples.

## The brain

`tests/contract/` produces every example by calling the real route, serializer or daemon over a
fixed world on a `FakeClock`; no example is typed by hand. Comparison is a subset match: a key the
example has must be in the answer with the same JSON type (a new field passes; a rename or a
removal fails and the message names the path). The keys in `exact` are vocabularies (a lane, a
state, a kind) and must also keep their value. The brain must take every `valid` input and
refuse every `invalid` one with its `refusal`.

After a deliberate change to a message:

```
.venv/bin/pytest tests/contract --update-contracts
```

rewrites the examples from the real routes and nothing else (the `valid` and `invalid` lists are
edited by hand). Read the diff: it is the change both clients will see.

## The iPhone

iOS could not be built where these files were written, so none of this has been run against the
app. How the tests should read the files:

- Add `contracts/` to the `JarvisTests` target in `ios/project.yml` as resources (a folder
  reference, copied into the test bundle) and load a file with
  `Bundle(for: Self.self).url(forResource:withExtension:subdirectory:)`.
- Decode strictly in tests: `Decodable` with `keyedBy` checks, or `JSONSerialization` plus an
  assertion that every key the app reads is there with the type it reads. The app itself keeps
  decoding leniently (optional fields, unknown kinds skipped), as the desktop does, so a new
  field from the brain never breaks an old build.
- For what the app sends, encode each `valid` value with the app's own builder and assert the
  JSON equals it; assert the builder cannot produce any `invalid` one.

The tests that hand-type these messages today, and should switch to the files:

| Files | iOS tests |
| --- | --- |
| `phone_events/*` | `ReportTests.swift` (its private `Contract` enum restates the brain's keys) |
| `phone_ws/said` | `ReactionsTests.swift` |
| `phone_ws/step`, `error`, `say` | `TurnStatusTests.swift` |
| `phone_ws/hello`, `ready` | none builds or parses them by hand; add them to `TurnStatusTests.swift` |
| `waiting/*` | `WaitingTests.swift` |
| `day/timeline.json` | `DayTimelineTests.swift`, `DayTimelineDoingTests.swift`, `DayLineTests.swift` |
| `settings/*` | none reads the page today; add one over `SettingsModel` (`Settings/SettingsModel.swift`) |
| `about.json` | `AboutTests.swift` |

## The Mac companion

`desktop/resonance/scripts/verify-contracts.mjs` (`node --test scripts/verify-contracts.mjs`,
also run by `verify-all.mjs`) feeds `day/timeline.json` to `tidyReply` in `src/dayView.ts`, the
one pure parser of that body. The other messages are read by inline casts inside components
(`runtime.ts`, `SettingsPage.tsx`, `companionSettings.ts`, `useMailDraft.ts`,
`firstRun/firstRun.ts`), so there is no pure function
to feed them to.

## Not covered yet

Frames `row`, `reaction`, `ping`, `pong`, `locate`, `located` and the binary frames; pairing,
push, upload and the lifelog routes; the `notices` and `claude_requests` of `/inherent/waiting`;
the weather and the line writer's wording in the day; `POST /inherent/day/highlight`, `answer`
and `tidy`; `/inherent/track` and `/inherent/footprint`. Their tests stay hand-typed until a
file is added here.
