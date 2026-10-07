---
title: Talking to her
description: Say "Hey Jarvis" or tap her, talk over her whenever you like, and read long answers on screen while she speaks a short version, in English or Chinese.
---

You talk to Jarvis the way you would to someone sitting next to you: start with a phrase or a tap, keep going without repeating yourself, and cut in whenever you like.

<!-- shot: the talk area under the ball mid-conversation: a short spoken line lit word by word, a small written list beneath it -->

## Start with "Hey Jarvis" or a tap

Saying "Hey Jarvis" and tapping her are the same thing. A small wake-word model on your Mac listens for the phrase, and a hit starts a conversation exactly as a tap would. Speech recognition runs on the Mac too.

## Let the conversation end by itself

Once a conversation is open she keeps listening, so follow-ups need no wake word. It closes when you tap her, when you dismiss her ("that's all", "bye", 「退下」), or after about ten seconds of quiet following her last word or your last accepted sentence.

Ten seconds is deliberate. Phone assistants wait five to ten, and an open microphone in a real room hears plenty that isn't meant for her. Hums and lone words don't keep it open, but any real sentence in the window is taken as meant for her. If you need longer, say "hold on" or 「等我一下」 and she waits a minute.

## Interrupt her any time

Talk over her and she gives way at once: she ducks the moment you start, and after under a second of speech she pauses where she is. What you said then decides. A hum like "mm-hm" and she carries on; "stop" or 「别说了」 and she stops; anything else and she stops and answers it. Tapping her stops her too.

This only works if she doesn't hear herself. With a reSpeaker XVF3800 microphone array, which Jarvis is developed with, the board removes her voice before recognition. On any other microphone the Mac subtracts what she is playing in software.

## Read the long answer, hear the short one

Read aloud word for word, a written answer drags, so she speaks one to three sentences and the full answer goes on screen. Lists, times and links appear under her, and the Dashboard keeps everything. As she speaks, her line lights up word by word. Ask her to read something out in full and she is told to.

Captions have three levels: everything, only what is worth reading (the default), or nothing. The area shows the latest exchange, and pulling up reveals earlier ones. If an answer is slow, she says a short "one moment" after a few seconds, and the screen names what she is doing.

<!-- shot: a spoken sentence lit part-way, with a written schedule list under it, sample data -->

## See what she hears

While you speak, your words appear under her and grow with you, refreshed about four times a second from a fast on-device guess. When you finish, the recognized sentence replaces it, so a word may change. She waits until you are done before answering.

<!-- shot: short loop of live captions growing as a sentence is spoken, then settling -->

## English and Chinese

She follows whichever language you speak, turn by turn. Her answers, wait lines, goodbyes and the interface all come in both, and the stop and dismiss phrases work in both; only the wake phrase is English.

## Change her voice by asking

Say "louder", "slower", "back to normal" or "keep it like this" and her voice changes, with no settings page. The model only picks a direction and a size; fixed, bounded steps do the rest, and she asks before going past double volume. "Say that again" is not read as "louder". The change ends with the conversation unless you ask her to keep it.

Design notes: [wake word, conversation mode and quiet](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0102-the-wake-word-opens-conversation-mode-and-quiet-closes-it.md), [yielding to speech over her](https://github.com/samsara0xgg/Jarvis/blob/main/docs/adr/0100-jarvis-yields-to-speech-over-her-and-its-words-decide.md).
