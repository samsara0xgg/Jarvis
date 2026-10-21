# ADR 0352 — A file he names is picked by his own opening history, the screen reaches her as a picture, and a concealed clipboard is not read

**Status:** Accepted
**Date:** 2026-10-21
**Supersedes:** in ADR 0011, `read_file` reading only a file's first 8 KiB and never taking a path, `screen_look` describing the main display through the vision preset so the decision model never sees pixels, and `read_clipboard` returning whatever the clipboard holds

## Context

- **Picking a file.** The terminal resolves a spoken file name with the same resolver as a daemon,
  which ranks matches by how often he opened them, then an exact name, then the newest. On a
  terminal it reads an empty log of its own, so every open count is 0 and the newest name match
  wins. `open_path` runs without a card (L1), so a wrong pick is opened and reported done. ADR 0011
  meant an ambiguous name to return up to five candidates; the terminal returns one path or none.
- The brain's event log holds every file `open_path` and `read_file` have reached.
- **Reading a file.** `read_file` reads the first 8 KiB. On 2026-10-15 he asked her to look at a
  log's last error; a log's first 8 KiB are its oldest lines. It finds files only under his
  Documents, Desktop and Downloads (`tools.open_path.search_roots`) and never takes a path, so a
  candidate he picks cannot be named back to it. Its result carries the absolute path, which
  `docs/spec.html#tools` forbids.
- **The screen.** `screen_look` captures the main display only. A second model call, through the
  vision preset and with a fixed prompt, describes it, and the conversation model gets only that
  text. The slowest case is about 70 s. Tier 0 answers 「看一下我的屏幕」 by speaking the
  description.
  - ADR 0255 keeps a picture he sends as a picture for the conversation model, because a
    description drops what its writer did not think to mention. On Claude a picture 1568 px wide
    costs about 1,600 tokens. The vision preset and the conversation preset are both Anthropic.
- **The clipboard.** `read_clipboard` returns whatever `pbpaste` gives. Password managers mark what
  they copy as concealed (`org.nspasteboard.ConcealedType`, the nspasteboard.org convention). A
  copied password reaches the model, and Tier 0's clipboard line speaks it aloud.
- On 2026-10-21 Allen approved the plan for these tools (「就按你说的去吧」):
  - pick files by how often he opens them, and ask when names tie;
  - read a file's end;
  - give her the screenshot itself;
  - don't read clipboard content marked as a password.

## Decision

Rank his files by his own opening history, read a file from either end, show her the screen itself,
and leave concealed clipboard content unread.

- **His history.** With each name to resolve, the brain sends the terminal his open counts for the
  paths it might match, from the brain's own log. A match wins only when it leads clearly. When two
  or more tie, such as one name in two folders that he has not opened, the call opens and reads
  nothing. It returns them for him to choose (ADR 0319's `needs_choice`), each with its folder and
  when it changed.
- **Either end.** `read_file` reads up to 8 KiB from the start or from the end of a file. It also
  takes a path inside its folders, so the file he picks can be named back to it. Its result names
  the file by folder and name under his home, never by an absolute path.
- **The screen as a picture.** `screen_look` captures the display that holds the frontmost window.
  On a preset that takes images, the picture itself goes to the conversation model inside the tool's
  result, in that turn only; a later question looks again. Other presets keep the description.
  「看一下我的屏幕」 goes to the model, and Tier 0 has no screen line.
- **Concealed stays unread.** When the clipboard's item is marked concealed, `read_clipboard` says
  that the clipboard holds something marked private, and never returns its text.

## Alternatives rejected

- **Keep an opening history on the terminal.** It would be a second record of what he does, kept
  beside the brain's log, and a reinstalled or second Mac would start it from zero. The brain's log
  already has every open.
- **Read the end by default for files named `.log`.** His words say which end he wants
  (「最后报什么错」), and the name does not. A crash log read for its first error needs the start.
- **Keep the description and give it a better prompt.** The writer cannot know what he asks next.
  ADR 0255 rejected descriptions of his own pictures for that reason, and each look is a second paid
  model call.
- **Capture every display.** Two or three pictures per look multiply the tokens for a question that
  is about one window.

## Consequences

- Each look adds about 1,600 input tokens to the turn's requests, in place of a separate vision
  call. A screen can show more than he meant to share. It reaches the same provider as before, and
  the egress row changes with the code.
- 「看一下我的屏幕」 costs a model request where Tier 0 answered without one.
- A tie costs him one more answer.
- A password manager that does not mark what it copies is not caught.
