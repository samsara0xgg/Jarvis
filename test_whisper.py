"""测试 Whisper ASR — 录音并转文字"""
import sounddevice as sd
import numpy as np
import whisper
import time

SAMPLE_RATE = 16000
DURATION = 4

# 加载模型（首次运行会下载，约 150MB）
print("⏳ 加载 Whisper base 模型...")
t0 = time.time()
model = whisper.load_model("base")
print(f"✅ 模型加载完成 ({time.time()-t0:.1f}s)")

# 录音
print(f"\n🎙️  请说一句中文（录音 {DURATION} 秒）...")
audio = sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype='float32')
sd.wait()
print("✅ 录音完成")

# 转文字
print("⏳ 识别中...")
audio_flat = audio.flatten()
t0 = time.time()
result = model.transcribe(audio_flat, language="zh", fp16=False)
elapsed = time.time() - t0

print("\n" + "=" * 50)
print(f"  📝 识别结果: {result['text']}")
print(f"  ⏱️  耗时: {elapsed:.2f}s")
print(f"  🌐 语言: {result['language']}")
print("=" * 50)
