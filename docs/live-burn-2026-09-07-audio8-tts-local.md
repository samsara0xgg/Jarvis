# Live burn — 2026-09-07 (Audio8_TTS local replacement for MiniMax)

Question: can https://github.com/Edge0-AI/Audio8_TTS (0.6B DualAR TTS,
Apache-2.0) run on this M2 Max fast enough to replace the MiniMax WS
streaming TTS in `jarvis/surface/voice_tts.py`? Measurement only — nothing
under `jarvis/` changed.

Answer: no. Best local path is first audio in 1.2 s and RTF 3–7 (1.8 with a
hand-rolled pipeline); MiniMax is 0.4 s and RTF 0.06.

## Setup

- Machine: Apple M2 Max, 12 cores, 32 GiB, macOS 25.6, Python 3.12.
- Reference voice: repo demo `docs/0.1B/audio/reference/zh_01.mp3` with its
  transcript, registered through `arktts_runtime.registration`.
- Same three Chinese sentences (11 / 29 / 80 chars) for every path; MiniMax
  measured with `MiniMaxWSClient.synthesize_stream` from `~/.jarvis/env`.
- Paths tried:
  1. `onnx_runtime/` — `Audio8/Audio8-TTS-Preview-0.6B-ONNX-INT4`, CPU,
     `ARKTTS_THREADS=5` (and 10).
  2. `onnx_runtime_0_1b_int8/` — `Audio8/audio8-TTS-0.1B-ONNX-INT8`, CPU.
  3. `audio8_tts_infer.py` torch path, `--device mps`, fp16.
  4. Own script: ONNX 0.6B `iter_codes` in a producer thread, decoder in a
     second session with a 12–24 frame window instead of the stock 128.

## Results (29-char sentence, 7.7 s of audio unless noted)

| path | TTFA | wall | RTF |
|---|---|---|---|
| MiniMax WS (current) | 0.37 s (cold 0.93 s) | 0.9 s | 0.06 |
| 0.6B ONNX INT4, 5 threads, stock stream | 1.2 s | 39 s | 5.1 |
| 0.6B ONNX INT4, pipelined, ctx 12 | 1.4 s | 14 s | 1.8 |
| 0.1B ONNX INT8, 5 threads | 4.6 s | 41 s | 5.6 |
| 0.6B torch fp16 on MPS (no streaming) | n/a | 33 s | 1.4 |

Long sentence (18 s audio): 0.6B stock 116 s (RTF 6.4), 0.1B 118 s.

## Where the time goes

- 0.6B AR alone: 36 ms/frame at 5 threads = RTF 0.77. Slow step 20 ms, ten
  fast steps 23 ms, prefill of a 399-token prompt 0.7 s (this is the TTFA).
- Stock `stream()` re-decodes a 128+12 frame window every 12 frames; decoder
  costs 30 ms/frame, so decoding dominates (>80% of wall).
- Pipelining AR and decoder contends for cores: AR slows from 5.9 s to 13 s.
  10 threads is slower than 5 for every step.
- 0.1B has Mamba layers with no optimized ONNX CPU kernels; slower than 0.6B.
- MPS: 62–66 ms/frame for the AR, worse than CPU INT4, and fp16 sampling never
  emits EOS (always 512 frames; 11 chars became 24 s of audio). Only the codec
  decoder is fast on MPS (RTF 0.08).
- README latency (0.69 s p50, RTF 0.116) is an NVIDIA H20 number; the repo has
  no Apple Silicon figures and no MLX/CoreML path.

## Decision

Keep MiniMax. A hybrid (CPU INT4 AR + MPS decoder, pipelined) might reach
RTF ~0.85 but is a custom runtime for a still slower first byte and ~6 GB
resident; revisit only if an MLX build appears.

Samples and scratch scripts lived in the job tmp dir and were not kept.
