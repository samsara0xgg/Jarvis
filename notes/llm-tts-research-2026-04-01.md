# 云端 LLM + 情感 TTS 调研 — 2026-04-01

## 一、Kimi (Moonshot AI) API

### 模型

| 模型 | 上下文 | 输入/M tokens | 输出/M tokens | 说明 |
|------|:---:|:---:|:---:|------|
| **kimi-k2.5** (最新) | 256K | $0.60 | $3.00 | 多模态，支持 thinking mode |
| kimi-k2-turbo-preview | 256K | — | — | 快速版，60-100 tok/s |
| moonshot-v1-128k | 128K | $2.00 | $5.00 | 旧版 |

### 关键信息
- **OpenAI 兼容**: 完全兼容，`base_url=https://api.moonshot.ai/v1`
- **Tool calling**: 支持
- **Streaming**: 支持
- **注册**: 需中国手机号（用户已有）
- **加拿大访问**: 可用，服务器在中国，延迟 200-400ms
- **免费额度**: 新用户 ¥15 (~$2)
- **内置能力**: web_search、file_read 可通过 tool 调用

### 对比

| 服务 | 输入/M | 输出/M | 中文质量 | 延迟(加拿大) |
|------|:---:|:---:|:---:|:---:|
| DeepSeek V4 | $0.30 | $0.50 | 最优 | 200-400ms |
| **Kimi K2.5** | $0.60 | $3.00 | 优秀 | 200-400ms |
| GPT-4o | $2.50 | $10.00 | 优秀 | <100ms |
| GPT-4o-mini | $0.15 | $0.60 | 良好 | <100ms |

**结论**: Kimi K2.5 中文质量好，但性价比不如 DeepSeek V4（输出贵 6 倍）。优势在 256K 上下文和内置 web search。

---

## 二、Azure Neural TTS

### 中文情感语音

最强的几个 zh-CN 语音及支持的风格：

**XiaoxiaoNeural (女声，最全面):**
affectionate, angry, assistant, calm, chat, cheerful, customerservice,
disgruntled, excited, fearful, friendly, gentle, lyrical, newscast,
poetry-reading, sad, serious, sorry, whispering — **20 种风格**

**YunxiNeural (男声，适合助手):**
angry, assistant, chat, cheerful, depressed, disgruntled, embarrassed,
fearful, narration-relaxed, newscast, sad, serious — **12 种风格**
角色: Boy, Narrator, YoungAdultMale

**XiaomoNeural (女声，支持角色扮演):**
affectionate, angry, calm, cheerful, depressed, disgruntled, embarrassed,
envious, fearful, gentle, sad, serious — **12 种风格**
角色: Boy, Girl, OlderAdultFemale/Male, SeniorFemale/Male, YoungAdultFemale/Male

### SSML 情感控制语法

```xml
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis"
       xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">
  <voice name="zh-CN-XiaoxiaoNeural">
    <mstts:express-as style="cheerful" styledegree="1.5">
      好的，灯已经打开了！
    </mstts:express-as>
  </voice>
</speak>
```

`styledegree`: 0.01-2.0，控制情感强度。

### 定价
- **Neural TTS**: $16/M 字符 (约 $0.016/千字)
- **免费额度**: 每月 50 万字符免费 (F0 tier)
- **Canada Central 区域**: 可用，延迟 ~200ms

### 集成方式
```bash
pip install azure-cognitiveservices-speech
```
支持流式音频输出（PCM/MP3），Python SDK 成熟。

### SenseVoice 情绪映射

| SenseVoice 检测 | Azure style |
|:---:|:---:|
| HAPPY | cheerful |
| SAD | sad |
| ANGRY | angry |
| NEUTRAL | chat |
| FEARFUL | fearful |
| DISGUSTED | disgruntled |
| SURPRISED | excited |

---

## 三、MiniMax TTS

### 模型
- **speech-2.8-hd**: 最高质量
- **speech-2.8-turbo**: 低延迟版

### 情感控制
9 种预设: `happy`, `sad`, `angry`, `fearful`, `disgusted`, `surprised`, `calm`, `fluent`, `whisper`

通过 API 参数直接控制:
```json
{
  "voice_setting": {
    "voice_id": "Chinese_Female_1",
    "emotion": "happy",
    "speed": 1.0,
    "vol": 5,
    "pitch": 0
  }
}
```

### 定价
- **speech-2.8-turbo**: ¥2/千次 (~$0.28/千次)
- **speech-2.8-hd**: ¥5/千次 (~$0.70/千次)
- **免费额度**: 新用户赠送额度

### 关键信息
- **API**: REST，非 OpenAI 格式，需自定义集成
- **流式**: 支持 SSE 流式音频
- **音频格式**: mp3, pcm, wav, flac, aac, opus
- **注册**: 需中国手机号
- **加拿大访问**: 可用
- **100+ 中文预置音色**

---

## 四、对比总结

### 情感 TTS 对比

| | Azure Neural TTS | MiniMax TTS |
|---|---|---|
| **中文质量** | 优秀 | 极佳（TTS Arena 榜首） |
| **情感风格数** | 20+ (XiaoxiaoNeural) | 9 种 |
| **情感强度控制** | 有 (styledegree 0.01-2.0) | 无 |
| **延迟** | ~200ms (Canada Central) | ~300-500ms (中国服务器) |
| **价格** | $16/M chars, **50万字/月免费** | ¥2-5/千次 |
| **集成难度** | 低 (官方 Python SDK) | 中 (REST, 需自定义) |
| **流式** | 支持 | 支持 |
| **角色扮演** | 支持 (8种角色) | 100+ 预置音色 |

### 推荐

**先试 Azure Neural TTS**:
- 50 万字/月免费够日常用
- Canada Central 延迟最低
- 官方 SDK 集成最简单
- 20 种情感风格 + 强度控制
- SenseVoice 情绪可直接映射

**MiniMax 作为备选**:
- 中文语音质量可能更自然（TTS Arena 排名高）
- 但延迟稍高、集成稍复杂、需中国手机注册

### 云端 LLM 推荐

**DeepSeek V4 作为主力** ($0.30/$0.50):
- 中文最强 + 最便宜
- OpenAI 兼容，改 base_url 即可

**Kimi K2.5 作为备选** ($0.60/$3.00):
- 256K 上下文 + 内置 web search
- 但输出价格是 DeepSeek 的 6 倍
