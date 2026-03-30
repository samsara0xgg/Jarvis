"""测试麦克风 — 录 3 秒音频并播放"""
import sounddevice as sd
import numpy as np
from scipy.io.wavfile import write
import os

SAMPLE_RATE = 16000
DURATION = 3  # 秒

print("=" * 50)
print("  麦克风测试")
print("=" * 50)

# 列出所有音频设备
print("\n可用音频设备:")
print(sd.query_devices())
print(f"\n默认输入设备: {sd.default.device[0]}")
print(f"默认输出设备: {sd.default.device[1]}")

# 录音
print(f"\n🎙️  开始录音 {DURATION} 秒，请说话...")
audio = sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype='float32')
sd.wait()
print("✅ 录音完成")

# 检查音量
volume = np.abs(audio).mean()
peak = np.abs(audio).max()
print(f"   平均音量: {volume:.4f}")
print(f"   峰值音量: {peak:.4f}")

if volume < 0.01:
    print("⚠️  音量太低！请检查麦克风是否正常")
else:
    print("✅ 音量正常")

# 保存
os.makedirs("data", exist_ok=True)
write("data/test_recording.wav", SAMPLE_RATE, audio)
print(f"\n💾 已保存到 data/test_recording.wav")

# 播放
print("🔊 播放录音...")
sd.play(audio, SAMPLE_RATE)
sd.wait()
print("✅ 播放完成")

print("\n" + "=" * 50)
print("  测试通过！麦克风工作正常")
print("=" * 50)
