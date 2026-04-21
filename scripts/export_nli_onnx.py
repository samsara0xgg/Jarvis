"""One-time export script: Erlangshen-Roberta-110M-NLI → ONNX INT8.

Run once on each machine before first use:
    uv pip install optimum[onnxruntime] transformers
    python scripts/export_nli_onnx.py

Idempotent: skips if data/nli-erlangshen/model.onnx already exists.
Output: data/nli-erlangshen/{model.onnx, tokenizer.json, config.json, ...}

Backup model (not loaded, noted for reference only):
  MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7 (317MB INT8)
"""
from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOGGER = logging.getLogger(__name__)

MODEL_ID = "IDEA-CCNL/Erlangshen-Roberta-110M-NLI"
OUT_DIR = Path("data/nli-erlangshen")
ONNX_MODEL = OUT_DIR / "model.onnx"

# Maximum acceptable file size for the quantized model (150 MB).
MAX_BYTES = 150 * 1024 * 1024


def main() -> int:
    if ONNX_MODEL.exists():
        size_mb = ONNX_MODEL.stat().st_size / 1024 / 1024
        LOGGER.info("model.onnx already exists (%.1f MB) — skipping export", size_mb)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- Step 1: export to ONNX FP32 via optimum ---
    LOGGER.info("Exporting %s to ONNX FP32 ...", MODEL_ID)
    try:
        from optimum.onnxruntime import ORTModelForSequenceClassification
        from transformers import AutoTokenizer
    except ImportError:
        LOGGER.error(
            "Missing dependencies. Run: uv pip install 'optimum[onnxruntime]' transformers"
        )
        return 1

    fp32_dir = OUT_DIR / "_fp32_tmp"
    try:
        model = ORTModelForSequenceClassification.from_pretrained(
            MODEL_ID, export=True
        )
        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        model.save_pretrained(fp32_dir)
        tokenizer.save_pretrained(fp32_dir)
        LOGGER.info("FP32 export saved to %s", fp32_dir)
    except Exception as exc:
        LOGGER.error("ONNX FP32 export failed: %s", exc)
        return 1

    # --- Step 2: INT8 static quantization ---
    LOGGER.info("Quantizing to INT8 ...")
    try:
        from optimum.onnxruntime import ORTQuantizer
        from optimum.onnxruntime.configuration import AutoQuantizationConfig

        quantizer = ORTQuantizer.from_pretrained(fp32_dir)
        qconfig = AutoQuantizationConfig.avx2(is_static=False, per_channel=False)
        quantizer.quantize(
            save_dir=OUT_DIR,
            quantization_config=qconfig,
        )
        LOGGER.info("INT8 quantization complete")
    except Exception as exc:
        LOGGER.error("Quantization failed: %s", exc)
        return 1
    finally:
        if fp32_dir.exists():
            shutil.rmtree(fp32_dir, ignore_errors=True)

    # Rename quantized model file to canonical name if needed.
    quantized_candidates = list(OUT_DIR.glob("model_quantized*.onnx"))
    if quantized_candidates and not ONNX_MODEL.exists():
        quantized_candidates[0].rename(ONNX_MODEL)
        LOGGER.info("Renamed %s → model.onnx", quantized_candidates[0].name)

    if not ONNX_MODEL.exists():
        LOGGER.error("model.onnx not found after quantization — check output dir")
        return 1

    size_mb = ONNX_MODEL.stat().st_size / 1024 / 1024
    LOGGER.info("model.onnx size: %.1f MB", size_mb)
    if ONNX_MODEL.stat().st_size > MAX_BYTES:
        LOGGER.warning(
            "model.onnx is %.1f MB — exceeds expected <150 MB. Check quantization.",
            size_mb,
        )

    LOGGER.info("Export complete. Files in %s:", OUT_DIR)
    for f in sorted(OUT_DIR.iterdir()):
        LOGGER.info("  %s (%.1f KB)", f.name, f.stat().st_size / 1024)

    return 0


if __name__ == "__main__":
    sys.exit(main())
