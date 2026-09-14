# ADR-0018 — Usage Observer and the Resonance Quota Module

Status: accepted (2026-09-13)

## 1. Context

Allen pays for several AI plans and APIs and has no single place that says
how much of each is left. The subscription windows (Claude Max 5 h / 7 d,
Codex 7 d) run out silently mid-task; the pay-as-you-go accounts (OpenAI
API for GPT-Live and Typeless, DeepSeek, MiniMax TTS) drift without a
readout. Menubar tools (CodexBar, cc-switch) show some of this but expose
no API, so Jarvis cannot fold it into attention or a briefing.

Every source was verified live on 2026-09-13 before this ADR:

| Service | Source | Official | Credential |
|---|---|---|---|
| claude | `GET api.anthropic.com/api/oauth/usage` (`anthropic-beta: oauth-2025-04-20`) | no (undocumented, what every menubar tool uses) | Claude Code's Keychain item `Claude Code-credentials`, file fallback |
| codex | `GET chatgpt.com/backend-api/wham/usage` | no | `~/.codex/auth.json` |
| openai | `GET /v1/organization/costs`, `/v1/organization/usage/completions` | yes | `OPENAI_ADMIN_KEY` (org admin key; a project key 403s with `api.usage.read`) |
| deepseek | `GET api.deepseek.com/user/balance` | yes | `DEEPSEEK_API_KEY` |
| minimax | none exists for pay-as-you-go balance | — | estimate, see D3 |

## 2. Decisions

**D1 — An L5 observer, shaped exactly like the repo observer (ADR-0009 D5).**
`jarvis/surface/usage_observer.py` runs under the `observer` principal:
one poll produces a total snapshot per service; `usage.state_observed` is
emitted only when a service's `(status, error, data)` changed; the
baseline is recovered from the event log at startup, never kept only in
memory. `collect` (network) runs under `asyncio.to_thread`; `emit` stays
on the connection's owning thread. Neither new type joins any trigger
tuple — observations fold silently (spec §3.4.1). The existing canary
`test_canary_observer_never_triggers_decide.py` now enumerates every
observer module instead of naming one.

**D2 — Two registry entries, both `evidence_semantics=observation`.**
`usage.state_observed` — `service`, `status` (`ok | error | unconfigured`),
`error?`, `data`, `observed_at_ms`, `actor`. `data` is service-specific
bounded JSON (percentages, dollars, ISO timestamps, plan labels, ≤ 20
rows). Secrets never enter a payload. A failed or missing credential is
a snapshot with `status` set, not a silent freeze, so a stale row explains
itself on the surface.
`tts.usage_observed` — `provider`, `characters`, `response_id`, `sequence`,
`actor`, emitted by the media owner at each provider segment-final from
the `usage_characters` MiniMax already returns (`voice_media.py`). A
missing usage block or a failed log write is skipped: playback never
depends on bookkeeping.

**D3 — MiniMax balance is an estimate and is labelled as one.**
`balance = observer.usage.minimax_anchor_usd − characters_since(anchor_at) × usd_per_million_chars / 1e6`.
The anchor is the one human-supplied fact (copied from the MiniMax console
after each top-up); the fold reads `tts.usage_observed` on the loop thread,
no network. It only sees Jarvis's own TTS spend, so it is accurate exactly
when MiniMax is used from Jarvis alone. The surface shows the formula.

**D4 — The dashboard reads a fold, not a projection.**
`latest_usage(conn)` (latest row per service) is the read model behind
`GET /inherent/usage`; `POST /inherent/usage/refresh` runs one poll and
answers the same shape. No `projections.py` change: the fold is ten lines
and has one consumer. Both routes register only when the observer is on.

**D5 — Config lives under `observer.usage` in `config/jarvis.yaml`.**
`enabled`, `poll_interval_s` (default 300 — the claude.ai endpoint locks
for an hour when polled faster), the MiniMax anchor, and the unit price.
Credentials stay in the Keychain, `~/.codex/auth.json`, and env vars.

**D6 — Resonance renders it as the existing 模型额度 module.**
`desktop/resonance/src/QuotaModule.tsx` owns the fetch (on open + every
60 s + refresh), the summary tile, and the expanded page with three
segments — 订阅 (Claude / Codex windows, reset times, reset credits),
花费 (OpenAI today / month-to-date by model and by key; Anthropic API
shown as unconfigured until an admin key exists), 余额 (DeepSeek official,
MiniMax estimate with its formula). Without a `port` (design lab) it
renders demo data. The daemon owns truth; the module owns presentation.

## 3. Consequences

- Five sources, one event stream, one HTTP shape; adding Anthropic API
  spend later is one collector and no new event type.
- Codex `rate_limit_reset_credits` is in the stream: a reset landing is
  observable without watching X.
- The claude.ai and Codex endpoints are undocumented; when they change,
  the row degrades to `status: error` with the message, not to a wrong
  number.
- OpenAI "today" is local-day hourly buckets; "month" is UTC-day buckets
  from the local month start — a few hours of skew at month edges.

## 4. Non-goals

Cursor, Copilot, Gemini, Groq, OpenRouter (Allen's call); attention
routing on thresholds (a later subscriber over the same events); a
Status Board note; the thumbnail's final visual (pending design).

## 5. Definition of done

1. Tier 1 green: `lint-imports`, `ruff`, `mypy --strict`, canaries
   (`observer_never_triggers_decide` parametrized over both observers,
   `emit_event_registered`).
2. `tests/integration/test_usage_observer_read_model.py`: emit-on-change,
   restart recovery, latest-per-service, anchor fold.
3. Live: daemon started with `observer.usage.enabled: true`; `GET
   /inherent/usage` returns five services with the real Claude / Codex
   percentages, OpenAI dollars, DeepSeek balance and the MiniMax estimate;
   `POST /inherent/usage/refresh` appends rows only for changed services.
4. Resonance built and opened against that daemon: 模型额度 expands to
   the three-segment page with live numbers; 刷新 round-trips.
