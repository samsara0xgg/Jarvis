# ADR 0092 — One OpenAI Key Runs Every Default Feature

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- On 2026-09-29 Allen set the goal that each person runs Jarvis with one API
  key. Asked which provider that key is, he chose "OpenAI 先行" over "either
  OpenAI or Anthropic", knowing that someone with only an Anthropic key cannot
  chat for now.
- Every model call Jarvis makes by default goes to api.openai.com with
  `OPENAI_API_KEY`: conversation (`gpt-6-luna`, with thinking on request), the
  work state, screen reading and history summaries (`gpt-6-luna`), the daily
  report (`gpt-6-sol`), dictation polish (`gpt-5.4-mini`) and live voice
  (`gpt-live-1`) (`config/jarvis.yaml` presets). `llm.provider` accepts
  `anthropic`, but no preset uses it; moving the daily report and work state
  to Claude would mean re-tuning their prompts and evaluating them with paid
  calls.
- The spoken answer is not OpenAI: setup's `reply_voice` feature is on only
  with `MINIMAX_API_KEY` (`jarvis/runtime/setup.py:191`), and the voices first
  run offers are MiniMax voice ids. Web search is registered only when a
  Tavily or Exa key is present (`config/jarvis.yaml`, `tools.web`).
- First run asks for an OpenAI key, then offers a MiniMax key for the voice
  and a Tavily key for web search (`KEY_ENVS`,
  `jarvis/runtime/setup.py:38-42`; `desktop/resonance/src/firstRun/firstRun.ts`).
- The agent stars, the notch's approvals and the Agents window run on the
  owner's own Claude Code and Codex logins, not on a Jarvis key (ADR 0049,
  ADR 0073).

## Decision

Every feature that is on by default runs on one OpenAI API key alone. Any
other provider's key only moves a feature to that provider or turns on a
feature that is off by default, and first run asks for the OpenAI key and no
other.

An Anthropic key as the one key is a later decision; the `anthropic` code path
stays, unoffered.

## Alternatives rejected

- **Either OpenAI or Anthropic** — the daily report and the work state would
  need prompts re-tuned and re-evaluated on Claude with paid calls before
  either provider could keep the one-key promise, and the Realtime voice has
  no Anthropic counterpart in the code.
- **Keep MiniMax for the spoken answer** — every new owner would need a second
  account and key before Jarvis can answer aloud; without it `reply_voice` is
  off.

## Consequences

- The spoken answer needs an OpenAI speech path, and first run's voices need
  OpenAI voices; MiniMax becomes a choice in settings.
- Everything the default features send goes to one provider, OpenAI; the
  spec's egress chapter lists what.
- An owner who pays only for Claude, as a Claude Code subscriber may, has to
  open an OpenAI account to use anything beyond the agent features.
- One key is one bill, and Jarvis has no spend cap yet (release inventory
  改15).
