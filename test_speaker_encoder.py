"""测试 SpeechBrain 声纹编码 — 提取嵌入向量"""
import sounddevice as sd
import numpy as np
import torch
import torchaudio
from speechbrain.inference.speaker import EncoderClassifier
import time

SAMPLE_RATE = 16000
DURATION = 3

# 加载模型（首次运行会下载，约 80MB）
print("⏳ 加载 SpeechBrain ECAPA-TDNN 模型...")
t0 = time.time()
encoder = EncoderClassifier.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir="data/speechbrain_model",
    run_opts={"device": "cpu"}
)
print(f"✅ 模型加载完成 ({time.time()-t0:.1f}s)")

# 录两段音频
embeddings = []
for i in range(2):
    print(f"\n🎙️  第 {i+1}/2 段录音（{DURATION} 秒），请说话...")
    audio = sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype='float32')
    sd.wait()

    # 转为 torch tensor
    waveform = torch.tensor(audio.flatten()).unsqueeze(0)

    # 提取嵌入向量
    with torch.no_grad():
        embedding = encoder.encode_batch(waveform)
    embeddings.append(embedding.squeeze())
    print(f"   嵌入向量维度: {embedding.shape[-1]}")

# 计算相似度
from torch.nn.functional import cosine_similarity
similarity = cosine_similarity(embeddings[0].unsqueeze(0), embeddings[1].unsqueeze(0)).item()

print("\n" + "=" * 50)
print(f"  两段录音的声纹相似度: {similarity:.4f}")
if similarity > 0.70:
    print("  ✅ 同一个人说的（> 0.70 阈值）")
elif similarity > 0.50:
    print("  ⚠️  可能是同一个人（0.50-0.70 灰区）")
else:
    print("  ❌ 不像是同一个人（< 0.50）")
print("=" * 50)
