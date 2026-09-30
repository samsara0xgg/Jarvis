# Third-party notices

Jarvis ships or downloads the work below. The full license texts travel with
the app, in `Jarvis.app/Contents/Resources`: Electron's in `LICENSE.electron`
and Chromium's in `LICENSES.chromium.html`; CPython's in
`python/lib/python3.12/LICENSE.txt`; each Python package's in its
`*.dist-info` folder under `python/lib/python3.12/site-packages`.

- **SenseVoiceSmall** by FunASR / FunAudioLLM, the speech recognition model,
  downloaded on first launch as the int8 ONNX conversion from k2-fsa
  sherpa-onnx (csukuangfj). FunASR Model Open Source License v1.1:
  https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE. Model card:
  https://huggingface.co/FunAudioLLM/SenseVoiceSmall
- **Silero VAD**, the voice activity detector, downloaded on first launch as
  exported by sherpa-onnx. Copyright (c) 2020-present Silero Team, MIT
  License: https://github.com/snakers4/silero-vad/blob/master/LICENSE
- **hey_jarvis** microWakeWord model by Kevin Ahrendt, inside
  pymicro-wakeword. Apache License 2.0:
  https://github.com/esphome/micro-wake-word-models
- **sherpa-onnx** (Apache-2.0), **ONNX Runtime** (MIT; its
  `ThirdPartyNotices.txt` is in its package folder), **pymicro-wakeword** with
  TensorFlow Lite (Apache-2.0), **Electron** (MIT) and **CPython** (PSF).
- **highlight.js**, the code colours in Startrail's preview, bundled into the
  Agents window. Copyright (c) 2006, Ivan Sagalaev. BSD 3-Clause License:
  https://github.com/highlightjs/highlight.js/blob/main/LICENSE
