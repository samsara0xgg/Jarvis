# ADR 0118 — Test-time recordings of her voice and of the model requests are off-by-default switches

**Status:** Accepted
**Date:** 2026-10-01
**Supersedes:** none

## Context

- Live voice tests are judged from `~/.jarvis` records: `mac_events.db`, the kept
  input audio, `daemon.err.log` and the realtime trace. Two things are missing.
  On 2026-10-01 MiniMax read "It's 6:47 p.m. in Victoria." in Romanian and the
  audio could not be replayed; and what a model request sent and got back is
  nowhere, so a structured reply that ended on 「我查一下」 with no tool call
  cannot be explained.
- ADR 0067 keeps logs free of what was said and deletes kept recordings by age
  from `memory.audio_dir`. Both new records hold speech.
- The first spoken word is the latency budget: the stream's first delta and the
  provider PCM reader share a loop with playback.

## Decision

Two switches in a `diagnostics:` block of `config/jarvis.yaml`, both `false`:
`record_tts_audio` keeps each segment's provider PCM as
`tts-<response_id>-<sequence>` audio plus a one-line `.json` of the text sent,
voice and model in `memory.audio_dir`; `log_llm_io` appends one JSON line per
model request to `logs/llm-io.jsonl`. Each is written from a thread of its own
after the segment or request ends; a write failure costs one warning.

Limits: the request line carries the body as sent with tools by name only, and
is scrubbed of API keys and Bearer tokens; the legacy batch TTS path keeps its
resampled output, not the raw frames; `chat_stream` (unused) is not logged.

## Alternatives rejected

- **Extend `memory.retain_audio` to her voice** — it is a privacy default of the
  product (false, ADR 0067) and would turn her voice on for every owner who
  keeps recordings; a test needs the switch on for days, not the product.
- **Add the request body to the realtime trace JSONL** — trace rows are bounded
  scalar attributes, exported only by an environment variable at launch, and a
  full message history per request does not fit that row shape.
- **Capture the speaker output** — it is gained, resampled and mixed with
  barge-in cuts, so it cannot say what MiniMax returned for a wrong-language
  read.

## Consequences

`llm-io.jsonl` holds what Allen said and what she answered and is cut by no
rotation (`rotate_logs` takes `*.log` only); it grows until deleted by hand.
Her kept audio is deleted with the input audio at `audio_retention_days`, and
`audio_retention_days: null` keeps it forever. A request that never settles
(a stream abandoned before it is read) leaves no line.
