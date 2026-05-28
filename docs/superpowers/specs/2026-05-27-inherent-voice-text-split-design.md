# Inherent Voice / Text Channel Split — Design

**Date:** 2026-05-27
**Branch:** `worktree-claude-adr0001`
**Spec basis (primary):** `docs/spec.html` §3.6.6 (Streaming TTS and ResponsePlan), §3.6.7 (Inherent boundaries), §5.4 (Event Type Registry)
**Spec basis (corroborative):** §3.4.13 (ResponsePlan), §3.6.10 (Presentation action vs world action), §3.6.11 (Surface fallback chain)
**Companion ADRs:** ADR-0003 (Inherent text surface), ADR-0005 (Inherent voice surface)
**Status:** Draft — awaiting Allen review before handoff to writing-plans

---

## 1. Problem

Inherent mode emits a single chunk stream that carries both the spoken slice (`<voice>...</voice>`) and the displayed slice (`<document>...</document>`) interleaved with literal XML-like tag bytes. Downstream surfaces are forced to pick the bytes apart after the fact:

- `surface.voice_tts.TTSPipeline.handle_chunk` accumulates the chunk text, then runs `_extract_voice_content` (regex over the join buffer) to recover the speakable subset.
- `surface.inherent_output.InherentBroadcaster.broadcast_chunk` forwards `event.payload["text"]` byte-for-byte to the WebSocket. The InherentCard's `BridgeBackend.swift` `siriAppend` appends each token verbatim, so the user sees the literal tag characters (`<voice>`, `</document>`) on screen.

Live event log evidence (turn `T158154fb`, captured 2026-05-27 ~4:50 PM):

```
surface.response_chunk  {"text":"</document>",                                "turn_id":"T158154fb"}
surface.response_chunk  {"text":"- 重要细节放在 document，结论放在 voice。",   "turn_id":"T158154fb"}
surface.response_chunk  {"text":"- 代理自称完成不等于完成；需要证据验证。",     "turn_id":"T158154fb"}
...
surface.response_emitted {... "document_text":"当前定位：Jarvis…",
                              "voice_text":"我是 Jarvis…",
                              "text":"<voice>\n我是 Jarvis…</voice><document>当前定位：Jarvis…</document>"}
```

Notice that the final `surface.response_emitted` event already carries clean `voice_text` and `document_text` fields — the channel split is *known* at emission time. The chunk stream nevertheless ships the raw blob.

### Why it happens

`jarvis/surface/cli_render.py:170 _emit_response_chunks` slices `response_plan.text` *after* `parse_response_channels` has already produced `voice_text` / `document_text` (line 306). The slicer is fed the un-split blob and runs `split_into_sentences` — which is tag-unaware — straight across `<voice>` and `<document>` tag boundaries. Two consequences:

1. **TTS** can only recover the voice slice by re-parsing the joined chunk buffer (`voice_tts.py:980-990` `_extract_voice_content` + the unclosed-region fallback). The parser exists solely to undo the splitter's tag-blindness.
2. **InherentCard** has no client-side parser at all; it just types whatever lands in the `append` envelope. Tag bytes show up on screen.

### Why it is a spec-fit issue

`docs/spec.html` §3.6.6 says "Surface 只执行 ResponsePlan，不自己判断 claim 风险." The current chunk pipeline forces both downstream surfaces to re-derive *which channel each chunk belongs to*, which is a presentation judgment the surface should not have to make at consumption time — the channel boundary is known when the chunk is emitted.

§3.6.11 (Surface fallback chain) further assumes each surface declares its own availability / failure semantics. Today the voice-TTS and document-display surfaces share a single event-type lane, so a malformed chunk affects both at once.

---

## 2. Root cause in three lines

1. `cli_render._emit_response_chunks` slices `response_plan.text` (the tag-laden blob) instead of slicing the already-extracted `voice_text` / `document_text`.
2. The single `surface.response_chunk` event type is the only chunk lane, so both consumers re-implement channel disambiguation downstream.
3. The byproducts — TTS regex on assembled text, tag bytes on the InherentCard screen — are symptoms of (1) and (2), not independent bugs.

---

## 3. Proposed change

### 3.1 Behavior

Replace the single `surface.response_chunk` lane with two channel-typed lanes emitted by `cli_render`. Each lane carries text that is already on its channel; the consumer side has no parsing to do.

| Lane | Event type | Source slice | Consumer |
|---|---|---|---|
| Voice | `surface.response_voice_chunk` | `voice_text` sliced by `required_gate_mode` | `TTSPipeline.handle_chunk` |
| Document | `surface.response_document_chunk` | `document_text` sliced by `required_gate_mode` | `InherentBroadcaster.broadcast_chunk` |

When `voice_text` is empty, zero `voice_chunk` events emit (and vice versa for `document_text`). `surface.response_open` and `surface.response_emitted` are unchanged.

### 3.2 Behavior matrix

| ResponsePlan.text shape | Old chunk stream | New chunk stream |
|---|---|---|
| `<voice>X</voice><document>Y</document>` | `[<voice>X</voice><document>Y</document>]` (1 raw chunk) or sentence-sliced through the tags | voice: `[X]`; document: `[Y]` |
| `<voice>X1. X2.</voice><document>Y</document>` w/ `required_gate_mode=sentence` | `[<voice>X1., X2.</voice><document>Y</document>]` etc. — tag-straddling slices | voice: `[X1., X2.]`; document: `[Y]` |
| Plain text (no tags) | one or more chunks of plain text | voice: `[plain text]`; document: `[plain text]` (per `parse_response_channels` legacy fallback — both channels receive the stripped full text; see §3.4) |
| `<voice>X</voice>` only | `[<voice>X</voice>]` | voice: `[X]`; document: `[]` |
| `<document>Y</document>` only | `[<document>Y</document>]` | document: `[Y]`; voice: `[]` |

### 3.3 Emit-site change

```python
# jarvis/surface/cli_render.py — _emit_response_chunks (rewritten)

def _emit_response_chunks(
    conn: sqlite3.Connection,
    *,
    turn_id: str,
    response_plan: ResponsePlanLike,
) -> None:
    channels = parse_response_channels(response_plan.text)

    def _emit(event_type: str, body: str) -> None:
        if not body.strip():
            return
        if response_plan.required_gate_mode == "sentence":
            chunks = split_into_sentences(body)
        else:
            chunks = [body]
        for chunk_text in chunks:
            emit_event(
                conn,
                type=event_type,
                payload={"turn_id": turn_id, "text": chunk_text},
                correlation={"turn_id": turn_id},
            )

    _emit("surface.response_voice_chunk", channels.voice)
    _emit("surface.response_document_chunk", channels.document)
```

The render_response body's existing `parse_response_channels` call (line 306) is reused — pass `channels` down rather than re-parsing.

### 3.4 The plain-text fallback (`parse_response_channels`)

`parse_response_channels` (jarvis/surface/cli.py:91-98) returns `voice=document=stripped(text)` when the input has no tags. Under the new design that fallback emits the same plain text on *both* the voice chunk lane and the document chunk lane — i.e., a plain-text reply gets spoken AND displayed.

This preserves legacy behavior verbatim: today's CLI surface already fires `cli_stdout` + `say` for un-channelized text via the same dual-channel default (see `render_response` §4 dispatch, cli_render.py:343-371). No change to `parse_response_channels`; no change to the dual-fire semantics. The change is localised to chunk emission.

### 3.5 Event type registration (L2)

Two new entries in `jarvis/state/event_log.py` `_REGISTRY_ENTRIES`, mirroring the existing `surface.response_chunk` shape (Day-1 5-field schema):

```python
EventTypeSchema(
    event_type="surface.response_voice_chunk",
    owner_layer="L5",
    required_payload=("turn_id", "text"),
    optional_payload=(),
    schema_version=1,
),
EventTypeSchema(
    event_type="surface.response_document_chunk",
    owner_layer="L5",
    required_payload=("turn_id", "text"),
    optional_payload=(),
    schema_version=1,
),
```

The old `surface.response_chunk` entry is **deleted**. Single-consumer / single-emitter codebase, no backward-compatibility window needed.

### 3.6 Consumer changes

**`jarvis/runtime/inherent_loop.py`** — two watcher cursor updates:

- `_response_watcher`'s SQL `WHERE type IN (...)` clause: `surface.response_chunk` → `surface.response_document_chunk`.
- `_tts_watcher`'s SQL `WHERE type IN (...)` clause: `surface.response_chunk` → `surface.response_voice_chunk`.

The dispatch `elif ev.type == "surface.response_chunk"` branches change to the new type strings. Method calls are otherwise unchanged.

**`jarvis/surface/voice_tts.py`** — simplification:

- `_extract_voice_content` regex and the `_VOICE_REGION_RE` / `_UNCLOSED_VOICE_RE` / `_DOCUMENT_REGION_RE` constants become unreachable and are **deleted** (~30 lines).
- `TTSPipeline.handle_chunk` no longer needs to re-extract — the incoming `text` is already on the voice channel. The chunk-aggregation buffer stays (still needed for end-of-region flushing), it just stops calling the extractor.

**`jarvis/surface/inherent_output.py`** — no signature change. `broadcast_chunk` still reads `event.payload["text"]`; the bytes are now clean. WS wire envelope is byte-identical to today (`{"op":"append","payload":{"token":<text>}}`), so InherentCard / `BridgeBackend.swift` need no client change.

### 3.7 Pre-emit Gate is unaffected

`pre_emit_gate` continues to gate the full `response_plan.text` (the un-split blob). The voice/document split is a *presentation* concern; risk classification is unchanged.

### 3.8 What does NOT change

- LLM contract / prompt — the model continues to emit `<voice>...</voice><document>...</document>` markup.
- `surface.response_open` / `surface.response_emitted` — unchanged. `voice_text` / `document_text` keep their roles in the emitted-event payload (now redundant with the per-channel chunk stream, but cheap to keep for audit and for non-streaming consumers like the cli_render banner / stdout paths).
- ResponsePlan schema (§3.4.13).
- Attention channel routing (§3.6.4) / surface dispatch (`ATTENTION_CHANNEL_TO_SURFACES`).
- WS wire shape — InherentCard sees the same `open` → `append`*N → `done` envelope sequence.
- `sentence_splitter` — it just runs on cleaner inputs.

---

## 4. Spec alignment

| Spec section | Requirement | Status |
|---|---|---|
| §3.4.13 / §3.6.6 | `required_gate_mode` drives chunking | Preserved — each channel slices by the same mode |
| §3.6.6 | "Surface 只执行 ResponsePlan，不自己判断 claim 风险" | Strengthened — surface no longer infers channel from tag bytes at consumption time |
| §3.6.7 Inherent boundaries | Surface owns presentation, not truth | Intact — split is at L5, text source is L3 ResponsePlan |
| §3.6.10 Presentation vs world action | TTS / display are presentation | Unchanged |
| §3.6.11 Surface fallback chain | Each surface declares its own availability/failure | Improved — voice and document channels become independently observable |
| §5.4 Event Type Registry | Every type registered with `owner_layer` / `required_payload` / ... | Two new entries added per Day-1 5-field shape; one entry deleted |

No spec deviation. Strictly more aligned than the status quo.

---

## 5. Test plan

The Day-1 test stack (unit + canary + integration) covers the touched modules. New / modified tests:

### 5.1 Unit (L5)

- `tests/unit/test_cli_render.py` — for each `required_gate_mode` value and for each ResponsePlan.text shape in the §3.2 matrix:
  - Exactly the expected count of `surface.response_voice_chunk` and `surface.response_document_chunk` events emitted.
  - Each chunk's `text` field contains no `<voice>` / `<document>` / `</voice>` / `</document>` substring.
  - Order: voice-chunks first, then document-chunks (per §6.1 recommendation).
- `tests/unit/test_inherent_output.py` — `broadcast_chunk` test fixture event type updated to `surface.response_document_chunk`; wire envelope shape regression assertion unchanged.
- `tests/unit/test_voice_tts.py` — `handle_chunk` test fixture event type updated to `surface.response_voice_chunk`; remove the now-obsolete `_extract_voice_content` test cases (mark for deletion).

### 5.2 Canary

- `tests/canary/test_event_log_registry.py` (if exists; else add) — asserts `surface.response_chunk` no longer in registry; `surface.response_voice_chunk` and `surface.response_document_chunk` both present.

### 5.3 Integration

- `tests/integration/test_inherent_turn_streaming.py` — end-to-end through `render_response` with `streaming_enabled=True`:
  - Assert event log contains the two new chunk types in the expected counts.
  - Assert `surface.response_chunk` rows are absent.
  - Assert `surface.response_emitted` still carries the same `voice_text` / `document_text` (regression).

### 5.4 Live smoke (post-implementation)

Drive a turn through the running daemon with an utterance that produces a non-trivial both-channel response (e.g., "介绍一下你自己"). Verify via WS client that the `append` envelopes contain no tag bytes and via `~/.jarvis/mac_events.db` that the new event types appear with the expected counts.

---

## 6. Open questions for review

### 6.1 Event interleaving order

Two reasonable orderings of the per-turn event stream:

| Order | Sequence | Pro | Con |
|---|---|---|---|
| **All-voice-then-all-document** | `open, voice*, document*, emitted` | Simple; matches `parse_response_channels` return order | TTS starts speaking before any document bytes have hit the WS; small UX latency between heard and seen content |
| **Interleaved by tag order** | `open, (voice or document)+, emitted` | Matches the LLM's tag order in the source text | Requires the splitter to track tag order through the text, modestly more complex |

Recommend **all-voice-then-all-document** — simpler, matches today's `parse_response_channels` output shape, and the TTS/display latency offset is well under perceptual threshold for short replies.

### 6.2 Should `surface.response_emitted` keep `voice_text` / `document_text`?

Yes. They are the only audit fields that capture the final post-split text. The audit value persists even when the chunk stream is replayed or absent (e.g., non-streaming surfaces).

### 6.3 Single-render-pass invariant

`render_response` currently calls `parse_response_channels(response_plan.text)` once at line 306 for the physical-surface dispatch path (voice/banner/stdout) and would call it again (or share the result) for the new chunk emit path. Plumbing the `channels` value as a parameter into `_emit_response_chunks` avoids the double-parse and ties the two surface dispatch sites to the same splitter result — small refactor, included in the change.

---

## 7. Migration / blast radius

| File | Change | Lines (approx.) |
|---|---|---|
| `jarvis/state/event_log.py` | +2 registry entries, -1 entry | +14 / -7 |
| `jarvis/surface/cli_render.py` | `_emit_response_chunks` rewrite + plumb `channels` | +25 / -15 |
| `jarvis/surface/voice_tts.py` | Drop `_extract_voice_content` + the three regex constants; `handle_chunk` no longer calls extractor | -35 / +5 |
| `jarvis/surface/inherent_output.py` | None (consumer is event-type-agnostic at body level) | 0 |
| `jarvis/runtime/inherent_loop.py` | Two `WHERE type IN (...)` lists updated; two `elif ev.type` branches renamed | +6 / -6 |
| `tests/unit/test_cli_render.py` | Matrix-driven channel-typed assertions | +60 / -30 |
| `tests/unit/test_voice_tts.py` | Drop `_extract_voice_content` cases; update chunk event type | +15 / -50 |
| `tests/unit/test_inherent_output.py` | Update chunk event type | +5 / -5 |
| `tests/integration/test_inherent_turn_streaming.py` | Update event-type expectations | +20 / -10 |

Net: < 200 lines of churn. No cross-layer signature change. ADR-0003 wire envelope unchanged.

---

## 8. Out of scope

- Changing the LLM prompt or the `<voice>/<document>` markup convention.
- Restructuring the LLM call to use OpenAI structured outputs / function calling for two named fields. (Considered as approach "B'" during brainstorming; rejected because the split already happens cleanly at finalization — moving it upstream would buy purity at a layer we already control.)
- Fixing the `delivered_via=[]` / `attention_channel=queue_review` routing observation from the same live event log (separate bug; the daemon's chunk path is observable regardless of `delivered_via`).
- Client-side InherentCard changes — none needed; wire envelope shape is preserved.

---

## 9. Acceptance criteria (for the eventual plan)

1. Live smoke turn ("介绍一下你自己" or similar) shows zero tag-byte substrings (`<voice>`, `</voice>`, `<document>`, `</document>`) in `surface.response_voice_chunk.text` or `surface.response_document_chunk.text` rows.
2. Live smoke turn produces audible TTS for the voice slice and displays the document slice on the InherentCard with no visible tag literals.
3. `~/.jarvis/mac_events.db` contains zero `surface.response_chunk` rows after a fresh turn.
4. `tests/unit/test_cli_render.py` matrix passes for all five §3.2 ResponsePlan.text shapes × both gate modes (`sentence`, `full_text`).
5. All four Tier-1 gates green: `lint-imports KEPT (1/1) · ruff clean · mypy strict clean · all unit tests pass · wall < 30s`.
