#!/bin/sh
# Compile the macOS-safe recognizer core with the harness; check the wire against the host's own code (origin/main of ~/Projects/jarvis,
# read-only: exported with git archive, run with its venv python), then run the recognizer over the recorded clips.
set -e
here=$(cd "$(dirname "$0")" && pwd)
voice="$here/../../Jarvis/Voice"
mkdir -p "$here/.build/tmp"
export TMPDIR="$here/.build/tmp"
swiftc -D VOICE_HARNESS -module-cache-path "$here/.build/modules" \
  "$voice/VoiceCore.swift" "$voice/VoiceWire.swift" "$voice/PlayerCore.swift" "$voice/VoiceRecognizer.swift" \
  "$voice/VoiceLink.swift" "$voice/CloudHearing.swift" "$voice/VoiceStub.swift" "$voice/VoiceTrace.swift" "$here/main.swift" \
  -o "$here/.build/voice_harness"

host=/Users/alllllenshi/Projects/jarvis
work=/Users/alllllenshi/.jarvis/research/phone-2026-10-09/xlang
rm -rf "$work"; mkdir -p "$work/frames" "$work/host_src"
git -C "$host" archive origin/main jarvis | tar -x -C "$work/host_src"
py="$host/.venv/bin/python"
"$py" "$here/host_codec_check.py" export "$work/frames" "$work/host_src"
"$here/.build/voice_harness" xlang "$work/frames"
"$py" "$here/host_codec_check.py" verify "$work/frames" "$work/host_src"

exec "$here/.build/voice_harness"
