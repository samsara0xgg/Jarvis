# ADR 0091 — The Product Name Is One Value and the Assistant's Name Is the Owner's

**Status:** Accepted
**Date:** 2026-09-29
**Supersedes:** none

## Context

- On 2026-09-29 Allen wrote "jarvis也不要是一个定量 未来可能改名且允许用户给他自定义名字":
  the product may be renamed, and each owner may name the assistant.
- Three kinds of name are mixed in the code today:
  - The assistant's name is already a setting. `assistant_name`
    (`config/jarvis.yaml:6`, default "Jarvis") is offered at first run
    (`jarvis/runtime/setup.py:201-224`) and put in for every `{assistant}` in
    the prompts and the config's strings (`jarvis/runtime/__init__.py:555-600`).
  - The product's name is spelled out: 107 non-comment lines in
    `desktop/resonance` (first run, settings, dashboard and more); the bundle's
    name, executable and microphone prompt
    (`desktop/resonance/scripts/package.mjs:31-39`); the OAuth client name
    that sign-in pages show (`jarvis/execution/mcp_oauth.py:200`); lines in
    `jarvis/shared/lang.py` such as "Jarvis 正在关闭"; the assistant name's
    fallback in three places; `JARVIS_IDENTITY` in `jarvis/constitution`.
  - Identifiers that address stored data or granted permissions: the package
    `jarvis`, the runtime root `~/.jarvis`, the `JARVIS_*` environment
    variables, the LaunchAgent labels `com.allen.jarvis` and
    `com.allen.jarvis.resonance`, the Keychain service `Jarvis`
    (`jarvis/deployment/__init__.py:47`), the bundle id
    `com.alllllenshi.jarvis` (`package.mjs:10`).
- macOS ties microphone, screen-recording and accessibility grants to the
  bundle id and signature, the Keychain finds a key by service and account,
  and all memory sits under the runtime root. Change any of them and an
  existing install looks new: every permission is asked again, and the keys
  and the memory are not found.
- The wake phrase is a trained model, microWakeWord's `hey_jarvis` (ADR 0042);
  `config/jarvis.yaml:5` already says it does not follow `assistant_name`. A
  microWakeWord model detects only the phrase it was trained on.

## Decision

Keep three names apart:

- **The product name** is defined in one place and read by every place a user
  sees the software named: the bundle and its permission prompts, windows,
  installer, sign-in pages, first run and settings. A rename changes that
  value and the artwork, nothing else.
- **The assistant's name** belongs to the owner: chosen at first run,
  changeable in settings, and the product name until the owner picks another.
  Wherever the assistant speaks, is addressed or is described acting, the text
  uses it.
- **The codename `jarvis`** stays in identifiers that address stored data or
  granted permissions, is never shown as a name, and does not change when the
  product is renamed.

The wake phrase is none of these. It stays "Hey Jarvis" until a detector for
another phrase exists, and first run and settings say which phrase wakes the
assistant.

## Alternatives rejected

- **Rename the identifiers along with the product** — a new bundle id makes
  macOS ask again for the microphone, screen recording and accessibility, a
  new Keychain service leaves the stored keys unfound, and a new runtime root
  hides the owner's memory; every existing install would start over.
- **One name for product and assistant** — the bundle name and the permission
  prompts are in the signed `Info.plist`, fixed at build time, so an owner who
  calls the assistant "Nova" would still see the build's name there, and the
  two would disagree anyway.
- **Let the wake phrase follow the assistant's name now** — there is no
  microWakeWord model for an arbitrary name, and making one takes thousands of
  synthetic samples and a training run, which first run cannot do on the
  owner's Mac.

## Consequences

- The 107 desktop lines and the `lang.py` strings each need a judgement,
  product or assistant, not a search and replace.
- An owner who names the assistant "Nova" still wakes it with "Hey Jarvis".
- Renaming the product also needs a wake model for the new name, or the
  renamed product keeps waking on "Hey Jarvis".
- The codename stays where a curious owner can look: `~/.jarvis`, Keychain
  Access, `launchctl list`, Activity Monitor.
- The identifiers still carry the author's names (`com.allen.jarvis`,
  `com.alllllenshi.jarvis`). Replacing them (release inventory 改3) costs
  only the author's install before the first public build, and every owner's
  permissions and keys after it.
