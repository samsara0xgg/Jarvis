# Jarvis 延迟优化设计

## 目标

将云端对话延迟从 2-3s 降到 ~600-800ms，同时支持用户语音切换模型。

## 5 项核心改动

### 1. 合并路由+回复为单次 Groq 调用

**现状**：Groq 路由 (170ms) → 等 → Grok 回复 (800-2000ms) = 串行两次网络调用
**改为**：单次 Groq 调用，prompt 同时包含设备列表 + 人格 + 记忆上下文

输出规则：
- 设备控制 → JSON（和现在格式一致），走 local_executor
- info_query/time/automation → JSON（路由分类），走本地技能
- 其他所有 → 直接自然语言回复

检测方式：输出以 `{` 开头 → 解析 JSON 走路由逻辑；否则 → 直接送 TTS

现有路由缓存不变：smart_home 命令 temperature=0 确定性输出，缓存命中 1ms。

快速路径不带 tools（Llama-70B tool calling 弱且不需要，设备控制走 JSON 路由）。

**文件改动**：
- `core/intent_router.py` — 新方法 `route_and_respond()`，统一 prompt
- `jarvis.py` — `_handle_utterance_inner()` + `handle_text()` 调用新方法
- `config.yaml` — 新增 `llm.presets` 结构

### 2. info_query 模板化

**现状**：WeatherSkill 返回 REQLLM → 再调 Grok 转述 (+1500ms)
**改为**：技能返回结构化数据 → 模板拼接 → 直接 TTS

覆盖范围：weather / stocks / news（这三个 sub_type 数据结构固定）

**文件改动**：
- `core/local_executor.py` — REQLLM 分支增加模板格式化，不再 fallback 到 LLM
- 各技能如有必要调整返回格式

### 3. ModelSwitchSkill

用户语音控制模型切换：
- 持久切换："小月，快速模式" / "小月，深度模式" / "小月，换成GPT-4o"
- 单次升级："仔细想想..." → 本轮用慢模型，下轮自动回落
- 查询："小月，现在用什么模型"

config.yaml 预定义 preset：
```yaml
llm:
  presets:
    fast:
      provider: groq
      model: llama-3.3-70b-versatile
      base_url: https://api.groq.com/openai/v1
    deep:
      provider: openai
      model: grok-4-1-fast-non-reasoning
      base_url: https://api.x.ai/v1
  default_preset: fast
```

运行时修改 LLMClient 的 model/base_url/api_key，不需要重启。

**文件改动**：
- `skills/model_switch.py` — 新技能
- `core/llm.py` — 新增 `switch_model()` 方法
- `config.yaml` — presets 配置
- `jarvis.py` — 注册技能 + 升级关键词检测

### 4. TTS 高频句预缓存

启动时预合成常用短句，写入现有 TTS 缓存目录：
```python
_PRECACHE = ["好的", "嗯，让我想想", "好的，灯开了", "好的，灯关了", "再见", "在的"]
```

命中后 ~1ms（读本地文件），不走网络。

**文件改动**：
- `core/tts.py` — 新方法 `precache(phrases)`
- `jarvis.py` — 启动时调用

### 5. 升级慢模型时播 filler

检测到升级关键词 → 立即播放预缓存 "嗯，让我想想" → 同时发起慢模型调用 → 流式 TTS

关键词从用户 query 中剥离后再发给 LLM。

**文件改动**：
- `jarvis.py` — 升级检测 + filler 播放 + 关键词剥离

## 延迟对比

| 场景 | 现在 | 优化后 |
|------|------|--------|
| 开关灯（缓存命中） | ~520ms | ~52ms |
| 一般聊天 | 2000-3000ms | 550-1050ms |
| 天气/股票 | ~2500ms | ~600ms |
| 复杂推理（手动升级） | ~2500ms | ~2000ms（感知 300ms） |
