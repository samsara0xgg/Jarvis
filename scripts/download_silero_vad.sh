#!/usr/bin/env bash
# Download sherpa-onnx's Silero VAD ONNX model (629KB) to data/.
set -euo pipefail

MODEL_PATH="data/silero_vad.onnx"

if [ -f "$MODEL_PATH" ]; then
    echo "Model already exists at $MODEL_PATH"
    exit 0
fi

mkdir -p data
echo "Downloading silero_vad.onnx..."
URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o "$MODEL_PATH" "$URL"
elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$MODEL_PATH" "$URL"
else
    echo "ERROR: neither curl nor wget is installed" >&2
    exit 1
fi

actual_size=$(stat -c%s "$MODEL_PATH" 2>/dev/null || stat -f%z "$MODEL_PATH")
if [ "$actual_size" -lt 500000 ]; then
    echo "ERROR: download truncated (${actual_size} bytes)"
    rm -f "$MODEL_PATH"
    exit 1
fi
echo "Done: $MODEL_PATH ($actual_size bytes)"
