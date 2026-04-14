#!/usr/bin/env bash
# Download sherpa-onnx streaming zipformer model for interrupt keyword detection.
# Small bilingual zh-en model (~30MB).
set -euo pipefail

MODEL_NAME="sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16"
MODEL_DIR="data/${MODEL_NAME}"

if [ -d "$MODEL_DIR" ]; then
    echo "Model already exists at $MODEL_DIR"
    exit 0
fi

echo "Downloading ${MODEL_NAME}..."
cd data
wget -q "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${MODEL_NAME}.tar.bz2"
tar xf "${MODEL_NAME}.tar.bz2"
rm "${MODEL_NAME}.tar.bz2"
echo "Done: ${MODEL_DIR}"
