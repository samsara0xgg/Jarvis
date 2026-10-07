---
title: Try the demo
description: One command shows the Jarvis companion on built-in sample data, with no keys, no accounts and no daemon.
---

One command on a Mac shows you Jarvis's interface running on sample data. It needs no keys, no accounts and no background daemon.

## How do you run it?

Paste this into Terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/samsara0xgg/Jarvis/main/scripts/try-demo.sh | bash
```

The first run takes a few minutes, mostly the Electron download. The script is short and written to be read before you run it. Its whole body sits in functions and `main` runs on the last line, so a download that is cut off never executes half a script.

<!-- shot: Terminal showing the script's checklist and the "Continue?" prompt, no missing tools -->

## What does it check, and what does it ask first?

It checks first and asks once before changing anything. It exits on anything other than macOS (an Intel Mac only gets a warning). Then it looks for the Xcode Command Line Tools, which the companion's small native glass module needs to compile, and for Node.js 24 or newer. It prints what it would install and waits for a `y`; answering no ends with "Nothing was installed."

- **Command Line Tools:** Apple's own installer dialog opens.
- **Node.js 24:** the official installer from nodejs.org, checked against its published checksum, and macOS asks for your password.
- **Jarvis:** a shallow clone into `~/Jarvis`. An existing Jarvis clone is updated, and a folder that is not Jarvis is left alone.
- **npm dependencies:** installed inside that folder, then the demo starts.

It never asks for keys or touches paid APIs. Pass `--dir <path>` to clone elsewhere.

## What does the demo show, and what doesn't it?

It shows the companion beside the notch, its agent stars, and the Dashboard filled with sample calendar, to-dos, mail, a morning brief and agents. Poke the ball and she plays a scripted exchange. On a screen without a notch she lives in a small black pill at the top.

It does not listen or speak, and nothing in it is your data. Replies, approvals and plugin sign-ins are all simulated locally, and the full Agents window (Startrail) does not open. Those need the full setup.

<!-- shot: short loop of the ball by the notch opening the Dashboard with sample mail and to-dos -->

## How do you quit?

Press Ctrl+C in the Terminal window where it is running.

## Prefer to do it by hand?

With the Xcode Command Line Tools and Node.js 24 installed:

```bash
git clone https://github.com/samsara0xgg/Jarvis
cd Jarvis/desktop/resonance
npm ci
npm run companion -- --demo
```

## Running it for real

Today Jarvis is a developer setup, not an installer. It is built for one person and one Mac, runs from a source checkout, and is not a signed app yet. A few defaults still assume the author's machine. Running it for real adds Python 3.12 and uv, the daemon that owns the microphone and speaker, an OpenAI key, and a one-time download of speech models. A public version for other people is planned for later. The README's "Run it for real" section has the steps.
