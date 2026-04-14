# 小月 Bug / 优化修复表

> ✅ = 已修复  ⏳ = 进行中  ⏳ (待审查) = 已写代码+测试，等 Allen 复核  ❌ = 未开始  🚫 = 等硬件/不做

## 2026-04-13 批次（7 项 ⏳ 待审查）

Claude 独立修完、全部带单元测试、跑完 `python -m pytest tests/ -q` 942 passed（12 个 failure 都是 pre-existing 环境依赖，不是本批引入）。Allen 审查完把 ⏳ (待审查) 改 ✅ 即可。涉及：

- `core/tts.py` — TTS cache key + Azure SSML escape
- `core/intent_router.py` — cache-key normalize + provider-fallback cache
- `memory/manager.py` — _RELATION_KEYWORDS 英文 + word-boundary
- `memory/direct_answer.py` — _is_question 补英文 + memory-ref
- `requirements-pi.txt` — openwakeword

---

## P0 — 实际体验问题（用户反馈）

### 1. 延迟问题（最严重）
- ✅ 统一 Groq 路由+回复单次调用 — 云端对话 2-3s → ~600-800ms
- ✅ info_query 模板化 — 天气/股票/新闻跳过 LLM 转述 省 ~1500ms
- ✅ TTS 高频句预缓存 — 启动时预合成常用短句
- ✅ 升级慢模型时播 filler — "嗯，让我想想" 立即播放

### 2. 三层路由出错率过高
- ❌ L1/L2/L3 路由复杂度不够真实应用，大部分请求实际都落到云端 LLM
- ❌ 意图路由 confidence 阈值/分类准确度需要重新评估

### 3. LLM 模型选择优化
- ✅ LLM preset 系统 — 默认 Groq fast，支持运行时切换
- ✅ ModelSwitchSkill — 语音切换模型 + 单次升级关键词

### 4. Memory 系统
- ⏸️ 暂缓，目前仍为验证阶段

### 5. 小 Bug
- ✅ 再见/告别后 ~10% 概率重新触发录音 — stream.start()移到farewell分支后 + detector.reset() + drain缓冲
- ✅ Tiffany 蓝等颜色指令失效 — COLOR_XY_MAP加常用色 + 路由传原始颜色 + 本地可解析时不绕云端

---

## P1 — 代码审查发现

## jarvis.py

- ✅ handle_text() 和 _handle_utterance_inner() 重复路由逻辑 → 抽取 _process_turn 共用方法（两个入口都已瘦身为薄 wrapper）
- ❌ _NEEDS_LLM_ACTIONS fallback 逻辑复杂，设备状态注入写死在主循环
- ✅ _cancel_current() → 全双工打断系统已接入（TTSEngine.stop + Popen kill + TTSPipeline.abort）

## core/llm.py

- ✅ _CHARS_PER_TOKEN = 3 对中文偏低 → 已改为 1.5
- ✅ 流式 tool_use fallback → 改为 streaming 内 inline tool loop（3次API→2次）
- ✅ preset 支持 provider 切换 → _apply_preset 加 provider 字段
- ❌ REQLLM rephrase 路径发完整 history+tools 只为转述本地数据，应精简 context
- ❌ Anthropic/OpenAI 两套代码合并为 adapter 模式（P2 重构，不急）

## core/tts.py

- ✅ _play_audio_file() subprocess 无法中断 → 改用 subprocess.Popen + TTSEngine.stop() 加 terminate/kill handle（全双工 feat 的一部分）
- ⏳ (待审查) TTS 缓存 key 加 engine_name 前缀，防止引擎切换读到错的音频（core/tts.py:_tts_cache_key）
- ❌ 缓存只对 MiniMax 生效，最贵的 OpenAI TTS 反而没缓存
- ⏳ (待审查) Azure SSML 属性值转义（quot/amp），提取 _build_azure_ssml 静态方法（core/tts.py:_synth_azure）

## core/speech_recognizer.py

- ❌ Confidence 只有 0.1/0.9 两档，比较粗糙
- ❌ 没有语言自动检测逻辑（固定 zh）

## core/intent_router.py

- ⏳ (待审查) 缓存 key NFKC normalize（全角→半角）+ 常用繁简映射；"开灯"/"开一下灯" 语义层问题仍未处理（core/intent_router.py:_normalize_cache_key）
- ⏳ (待审查) provider 全挂时若有 last-good cache 返回 provider="cache_fallback"，否则落 cloud 并改进 log（core/intent_router.py:_all_providers_failed）

## memory/manager.py

- ✅ _call_openai_json() 和 _call_llm_extract() 复用 requests.Session
- ✅ 偏好分类 "不" in content 误判双重否定 → 改用完整否定短语匹配
- ❌ Profile 重建遍历所有 active memories，O(n) 无缓存
- ⏳ (待审查) _RELATION_KEYWORDS 补英文亲属/关系词，加 _has_relation_keyword() 用 regex + \b word-boundary 防止 "reason"→"son" 误匹配（memory/manager.py）
- ❌ Episode digest 简单拼接，无 LLM 摘要

## memory/direct_answer.py

- ✅ _is_question() "啊"/"哪"/"几" 误判 → 去掉"啊"，改完整词组匹配
- ⏳ (待审查) _is_question() 补 memory-reference 模式（"我昨天说的那件事"/"还记得"/"上次说过的"）（memory/direct_answer.py）
- ⏳ (待审查) _is_question() 补英文问句（what/why/how/tell me/do you 等 startswith + 末尾 "?"），"whatever" 不误判（memory/direct_answer.py）

## 测试 (9 failed)

- ❌ test_wake_word (×2) — openwakeword 导入失败
- ❌ test_tts TTS speed — OpenAI TTS speed 参数
- ❌ test_memory_store episode dedup — Jaccard 阈值/逻辑
- ❌ 其余失败测试

## 其他模块

- ❌ LearningRouter — config/compose 模式落 LLM
- 🚫 自定义唤醒词 "小月" — 需 openwakeword 训练

## 技能系统

- ❌ SkillFactory 生成的技能加 pending_review 状态追踪（可用但待审查）
- ❌ RPi ~/.claude/CLAUDE.md 写专用 skill 生成指令（代码风格/安全/测试/接口规范）
- ❌ 加 WebSearchSkill（DuckDuckGo，零配置）
- ❌ RemoteDevSkill — 小月通过 SSH 控制 Mac 端 CC/开发环境（跑测试/改代码/git 操作/rsync 同步）
- ❌ ClaudeSkill — 通用语音→CC 接口（"帮我问Claude..."）

---

## P1 — SkillFactory 优化（2026-04-13 发现）

### 背景
测试 general suite 时说 "学会查fifa世界杯门票"，intent 检测到了 learn_create，但后台 subprocess 没完成 skill 就没生成。同时审查了之前生成的 `exchange_rate.py`，整体质量尚可但有多处可改进点。

### 1. 生成质量问题（exchange_rate.py 审查）

#### 1.1 ❌ 返回消息语言不统一
- **问题**：所有用户可见的消息都是英文（如 `"Failed to get exchange rate for {base}: {exc}"`、`"Error: target currency is required."`、`"Unknown currency code: {target}"`、`"API error: {error_type}"`）
- **影响**：Jarvis 是中文系统，小月用中文说话突然冒出英文错误消息不一致
- **原因**：prompt 没强调返回值要中文
- **修复**：`core/skill_factory.py` 的 `_build_prompt` 里明确要求 "所有返回给用户的消息必须用中文，技术错误也要中文表达"

#### 1.2 ❌ 没有缓存机制
- **问题**：每次调用都网络请求，同一汇率频繁查会重复打外部 API
- **影响**：延迟 + 外部 API 可能限流
- **修复思路**：prompt 里建议加 TTL 内存缓存（如 30 分钟），或提供 `CachedSkill` 基类

#### 1.3 ❌ 数值精度未考虑面值
- **问题**：`round(amount * rate, 4)` 对 JPY/KRW 等小面值货币可能显示 `0.0001` 不直观
- **修复**：prompt 提示"根据面值动态选精度"或在模板里加 smart formatting helper

#### 1.4 ⚠️ URL 硬编码（次要）
- **问题**：`_API_URL = "https://open.er-api.com/..."` 硬编码
- **评估**：CLAUDE.md 规则是 "don't hardcode IPs/paths"，URL 算轻违规
- **修复**：prompt 建议可配置 URL 放 `skills.<name>.api_url` 在 config.yaml

### 2. ✅ 生成质量做得好的地方（记录下次别丢）

- **类型提示齐全**（`dict[str, Any]`、`-> str` 全标）
- **Google 风格 docstring**
- **继承 `Skill` ABC 并实现完整接口**（`skill_name`, `get_tool_definitions`, `execute`）
- **工具定义 input_schema 完整**，`required` 字段明确，描述里带 "ISO 4217" 这种 hint
- **分层错误处理**：网络错误 → API 错误 → 数据错误 各自返回不同消息
- **`resp.raise_for_status()`** 处理 HTTP 错误
- **超时设置**（10s）
- **用 `self.logger.warning` 不用 print**（符合 CLAUDE.md）
- **智能返回格式**：amount=1 时简化输出
- **第三方依赖（requests）使用合理**

### 3. ❌ Subprocess 生命周期问题

#### 3.1 测试/主流程退出时 subprocess 被 kill
- **现象**：system_tests 跑完 "学会查fifa世界杯门票" 后，subprocess 还在跑但 harness.shutdown() 直接返回，subprocess 被孤儿进程杀掉 → 技能文件没生成
- **根因**：`SkillFactory._process` 在 `harness.shutdown()` 或 ctrl-c 时没有优雅等待
- **修复方向**：
  - harness.shutdown() 里检查 `skill_factory._process`，等待或明确 kill
  - 或提供 `--wait-skill-gen` flag 让测试等后台技能完成
  - 或把 subprocess detach（setsid）让它独立于父进程

#### 3.2 180s 硬超时可能太短
- **位置**：`core/skill_factory.py:129` — `self._process.wait(timeout=180)`
- **问题**：Claude Code 复杂技能可能需要 3-5 分钟（尤其是带测试的）
- **修复**：改为可配置（config.yaml `skill_factory.timeout_seconds`），默认 300s

#### 3.3 没有进度回调到 harness
- **问题**：subprocess 运行期间 stdout/stderr 行只进 `LOGGER.info`，系统测试看不到实时进度
- **修复**：`SkillFactory.create()` 的 `on_status` 回调接受更细粒度事件（started/prompt_sent/file_appeared/test_running/done）

### 4. ❌ Skill 审查/准入缺失

#### 4.1 生成完直接 hot-load 没人工审查
- **位置**：`jarvis.py:_learn_create_bg` → `skill_loader.update_metadata(..., status="pending_review")` 写了 metadata 但**没强制**审查
- **问题**：LLM 生成的代码直接跑，有安全/正确性风险
- **修复**：
  - 生成后默认 `enabled=false`，用户说 "确认这个技能" 才启用
  - 或接入 `security-reviewer` agent 自动扫描

#### 4.2 没有沙箱
- **问题**：生成的 skill 能直接 `import requests`/访问文件系统
- **修复**：沙箱执行器或更严格的 ALLOWED_IMPORTS whitelist

### 5. ❌ Prompt 模板改进点

#### 5.1 没强制要求响应中文
见 1.1

#### 5.2 没提供 cache/config helper 作为 reference
- **问题**：exchange_rate 从零写 fetch 逻辑，生成的每个技能都可能重造轮子
- **修复**：prompt 里 `abc_source + example_source`（目前用 `weather.py` 当例子）基础上，加 `helpers.py`（缓存、重试、config 读取通用代码）供 CC 参考调用

#### 5.3 没要求写 unit test
- **当前**：生成纯 skill 文件没测试
- **修复**：prompt 要求同时生成 `tests/test_<skill_name>.py`，至少覆盖 happy path + 一个错误 case

### 6. 🔧 系统测试侧的观察手段不足

- Phase B3 的 `skill_factory_status` 只在 run_step 完成瞬间快照一次，subprocess 后续状态看不到
- **改进**：harness 提供 `wait_for_skill(skill_id, timeout)` 方法给特定场景调用
- 或增加 `--monitor-skills` 模式，runner 在每个 step 之间 poll `skills/learned/` 变化并输出

### 7. 📋 优化顺序建议（从易到难）

1. Prompt 改进（中文回复 + test 要求 + helpers.py 参考）— 纯 prompt 改动，立刻生效
2. 生成后 pending_review 强制化 — `jarvis.py` ~20 行改动
3. 可配置 timeout + on_status 细粒度回调 — `core/skill_factory.py` 中改动
4. Subprocess detach/优雅等待 — 中等复杂度
5. 沙箱/安全扫描 — 大工程
6. 系统测试侧 skill 等待机制 — harness 改动


## 行为学习 (T2 方向)

- ❌ 消费 behavior_log — 技能成功率、常用指令、使用模式
- ❌ 用户画像定期重建 — profiles + memories + behavior_log 合成
- ❌ FTS5 会话搜索 — ConversationStore 加全文搜索

## 等硬件

- 🚫 F3 情境感知 — LD2450 + DHT22 + BH1750
- 🚫 F7 显示屏 — ST7789/GC9A01
- 🚫 **Silero VAD vs XVF3800 冲突验证** — XVF3800 内部 DSP 可能有自己的 VAD/AGC 逻辑。等硬件到位后实测：
  - 检查 USB Audio Class 是否传 VAD 元数据（大概率不传）
  - 如果不传，软件 Silero VAD 直接用 XVF3800 输出的处理后音频
  - 注意 AGC 对概率阈值的影响（音量被拉齐后 RMS 不可靠，Silero 概率也可能偏移）
  - 阈值可能需要重调（不一定是 0.5）

## Silero VAD 替换 RMS（待实现，等硬件到位后一起调参）

### 已定方案
- 用 sherpa-onnx 内置 Silero VAD，零新依赖
- 模型 629KB（`silero_vad.onnx`）
- 准确率 87% vs RMS ~50%（噪声环境）
- 每轮省 ~1 秒（silence_duration 1.5s→0.5s）

### 实现时必须处理的 8 个点
1. 下载模型（629KB k2-fsa 导出）
2. 加入启动预热队列（`jarvis.py:247-257`）
3. 短命令（"停"≈200ms）需要更敏感的 VAD 实例或共享配置
4. `min_duration` 从 1.0 降到 0.3
5. `max_speech_duration` 设成 20s（跟 utterance_duration 对齐）
6. 模型缺失时 fallback 到 RMS，不崩溃
7. 外层 `target_duration` 硬限制保留，不能完全依赖 VAD
8. config 字段语义变化（`vad_threshold` 从音量阈值变成概率阈值）

### 配置模板
```yaml
audio:
  vad_enabled: true
  vad_engine: silero          # 新增，回退 "rms"
  vad_model_path: data/silero_vad.onnx
  vad_threshold: 0.5           # 概率阈值
  vad_silence_duration: 0.5    # 从 1.5 降
  vad_min_speech_duration: 0.25
  vad_max_speech_duration: 20
  min_duration: 0.3            # 从 1.0 降
```
