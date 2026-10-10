#!/bin/sh
# Compile the macOS-safe recognizer core with the harness and run it over the recorded clips.
set -e
here=$(cd "$(dirname "$0")" && pwd)
voice="$here/../../Jarvis/Voice"
mkdir -p "$here/.build/tmp"
export TMPDIR="$here/.build/tmp"
swiftc -D VOICE_HARNESS -module-cache-path "$here/.build/modules" \
  "$voice/VoiceCore.swift" "$voice/VoiceWire.swift" "$voice/PlayerCore.swift" "$voice/VoiceRecognizer.swift" \
  "$voice/VoiceLink.swift" "$voice/VoiceStub.swift" "$here/main.swift" \
  -o "$here/.build/voice_harness"
exec "$here/.build/voice_harness"
