# Session Handoff — 小月下一阶段：从开发到日常使用

复制以下全部内容作为新 session 的第一条消息。

---

## 背景

小月是我的私人语音管家，Python 项目在 `/Users/alllllenshi/Projects/jarvis/`。

**已完成的工作**：
- 记忆系统 5 Phase 优化（DA=95%, MRR@5=1.00, 815 tests）
- 完整语音管线：SenseVoice INT8 ASR → 声纹验证 → 意图路由(Groq→Cerebras) → LLM(GPT-4o-mini) → 5引擎TTS
- 14个内置技能 + 学习型技能框架
- Hue/MQTT/Sim 三种设备后端已实现
- 唤醒词检测（Porcupine）已实现
- 健康监控（熔断器）、自动化引擎、事件总线、行为日志

**现状**：系统软件完成度 ~80%，但从未在真实环境日常使用过。现在要从"能跑测试"转向"能用一天"。

## 本次任务：3 步让小月上线日常使用

### 任务 1：Hue 真实设备对接

**目标**：`devices.mode: sim` → `live`，语音控制真实 Hue 灯。

**现有基础**：
- `devices/hue/` 目录已有完整实现：`hue_bridge.py`（API通信）、`hue_discovery.py`（发现）、`hue_light.py`（灯控）、`hue_group.py`（灯组）、`hue_scene.py`（场景）
- `setup_hue.py` 配对脚本已存在
- `config.yaml` 已有 Hue 配置区块（L187-235），当前 `ip: ""`, `username: ""`

**步骤**：
1. 运行 `python setup_hue.py`，按 Bridge 按钮配对，获取 IP + username
2. 写入 `config.yaml` 的 `hue.bridge.ip` 和 `hue.bridge.username`
3. `config.yaml` L113: `mode: sim` → `mode: live`
4. 更新 `hue.light_aliases`（L225-234）映射到用户实际的灯具名称
5. 冒烟测试：`python jarvis.py --no-wake`，说"开客厅灯"、"把卧室灯调暗一点"
6. 确认设备状态反馈正确（"已开启"/"已关闭"等）

**注意**：
- Hue Bridge 和 Mac 必须在同一局域网
- `verify_ssl: false`（自签证书）已配好
- 灯具别名可能需要根据实际 Hue app 里的灯名调整
- 如果 Bridge 发现失败，检查 `allow_http_fallback: true`

---

### 任务 2：端到端延迟优化（目标 <1.5s，不算录音时间）

**目标**：从唤醒词检测到第一个 TTS 音节发出 < 1.5 秒。

**当前管线**（`jarvis.py:502-812` `_handle_utterance_inner`）：
```
录音结束 → [声纹验证 + ASR] 并行 → 身份解析 → 快捷检查(记住/学习)
  → DirectAnswer 尝试 → [意图路由 + 记忆查询] 并行 → 本地执行 or 云端 LLM
  → 逐句 TTS 播放
```

**已有的优化**：
- ASR + 声纹并行（ThreadPoolExecutor 3 workers）
- 意图路由 + 记忆查询并行
- HTTP 连接预热（L221-232）
- DirectAnswer 快路径（跳过 LLM）
- 逐句流式 TTS（双线程 pipeline）
- 意图路由 LRU 256 缓存

**需要做的**：
1. **加 timing instrumentation** — 在 `_handle_utterance_inner` 的每个阶段加 `time.perf_counter()`，输出到日志：
   - `t0`: 录音结束
   - `t1`: ASR + 声纹完成
   - `t2`: DirectAnswer 或意图路由完成
   - `t3`: 本地执行或 LLM 首 token
   - `t4`: 第一句 TTS 开始播放
   - 目标：`t4 - t0 < 1500ms`
2. **Profile 真实数据** — 跑 10-20 句常见指令，收集每阶段耗时分布
3. **根据瓶颈优化** — 常见瓶颈预判：
   - 意图路由 Groq API 延迟（~200-500ms）→ 已有 LRU 缓存，确认命中率
   - TTS 首句合成延迟 → 确认 OpenAI TTS 流式是否生效
   - LLM 首 token 延迟 → GPT-4o-mini 通常 ~300-500ms
   - 声纹验证延迟 → 如果纯本地应该 <100ms
4. **可选优化方向**（根据 profile 结果决定）：
   - 简单指令（开灯/关灯）走纯本地路径，完全跳过云端
   - TTS 缓存常见短回复（"好的"、"已开启"、"已关闭"）
   - 意图路由缓存预热（启动时缓存常见指令）

**不要做**：
- 不要改架构，只加计时和针对性优化
- 不要引入新依赖
- 每次改动跑 `python -m pytest tests/ -q` 确认不破坏

---

### 任务 3：唤醒词常驻 Mac

**目标**：`python jarvis.py` 唤醒词模式在 Mac 上稳定运行。

**现有基础**：
- `core/wake_word.py` — Porcupine 检测器已实现
- `jarvis.py:401-484` — `run_always_listening()` 已实现完整流程
- `config.yaml` L329-340 — 唤醒词配置已有，`picovoice_access_key: ""` 待填

**步骤**：
1. 去 https://console.picovoice.ai/ 注册免费 access key
2. 填入 `config.yaml` 的 `wake_word.picovoice_access_key`
3. 确认 Mac 系统设置已授权终端/Python 使用麦克风
4. `pip install pvporcupine` 确认已安装
5. 运行 `python jarvis.py`（不加 --no-wake）
6. 测试：说 "Jarvis" → 应该听到"在的。" → 然后说指令
7. 调参（如需要）：
   - `sensitivity: 0.5` — 太容易误触就调低，太难触发就调高
   - `session.silence_timeout: 30` — 30秒无语音自动回到监听
   - `session.utterance_duration: 5` — 单次录音时长

**稳定性检查**：
- 挂 1 小时看有没有崩溃
- 检查 CPU 占用（Porcupine 应该很低，<5%）
- 检查内存泄漏（watch `ps aux | grep jarvis`）
- 如果有问题检查 `sounddevice` 的 InputStream 是否正常关闭

---

## 执行顺序

**任务 1 → 任务 3 → 任务 2**

理由：先接 Hue（让小月能"做"事），再配唤醒词（让交互自然），最后优化延迟（基于真实使用数据）。任务 2 的 profile 需要在唤醒词模式下收集才有意义。

## 关键文件

| 文件 | 作用 |
|---|---|
| `config.yaml` | 所有配置（设备模式、Hue、唤醒词、TTS等）|
| `jarvis.py` | 主入口，`JarvisApp` 编排整个管线 |
| `setup_hue.py` | Hue Bridge 配对脚本 |
| `devices/hue/` | Hue 后端（bridge/discovery/light/group/scene）|
| `core/wake_word.py` | Porcupine 唤醒词检测 |
| `core/intent_router.py` | 意图路由 Groq→Cerebras + LRU |
| `core/tts.py` | 5引擎TTS + 双线程 pipeline |
| `core/llm.py` | LLM 客户端（流式逐句 + tool-use）|
| `memory/direct_answer.py` | 快路径（跳过 LLM 直接回答）|

## 规则

- commit 可以做，**push 必须等用户要求**
- commit message 不要加 Co-Authored-By
- `personality.py` prompt 只有用户允许才能改
- 每次改动跑 `python -m pytest tests/ -q`
- 不要换库/换框架，除非用户同意
