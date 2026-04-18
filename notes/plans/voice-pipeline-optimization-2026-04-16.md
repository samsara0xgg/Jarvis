# Voice Pipeline Optimization — 执行计划书

**日期**：2026-04-16
**状态**：已定案，待执行
**目标读者**：执行 agent（推荐 Sonnet，WP1 可 Haiku，WP6/WP7 建议 Opus）
**决策来源**：Allen 与主 agent 基于 `notes/olv-deep-dive-2026-04-16.md` 的三轮讨论（决策 A/B/C）

---

## 0. 背景与关联文档

本计划是 Allen 与主 agent 对齐 OLV（Open-LLM-VTuber）语音管线后的执行分解。

**前置阅读**（按优先级）：
1. `notes/olv-deep-dive-2026-04-16.md` — OLV 源码级验证报告，尤其 §2 Top 10 和 §7 Verification Addendum
2. `CLAUDE.md` — Jarvis 编码规则（本计划对部分规则有 override，见 §1.4）
3. OLV 本地仓库：`~/Projects/external/Open-LLM-VTuber/`（只读参考）

**关键决策定案**：
- 决策 A（打断 memory）：抄 OLV 机制，用**方案 b**（未播句子 = 已播完完整句子，不含正在播的那句）
- 决策 B（ASR hotwords）：不换模型，做三层文本 normalizer
- 决策 C（TTS 并发合成 sequence_counter）：不做，Jarvis 当前流水线够用
- VAD 架构升级：切到 silero-vad（onnxruntime 直调），纳入执行范围
- 打断架构：VAD-first 软停 + 关键词硬停 混合模式
- OLV Top 10 的 #7（MiniMax stream=True）**不做**——源码验证 OLV 并非真流式，收益是 0

---

## 1. 执行前提

### 1.1 工作区状态
- 工作区 dirty 的清理已由 Allen 本人完成（或接受在 dirty 状态下继续）
- **全部改动直接在 `main` branch 做，不开 feature branch**
- Commit OK，**never push**
- Commit message 不得包含 `Co-Authored-By`

### 1.2 WP 间协调
- 按 §2 的顺序**连续执行完全部 7 个 WP**，不中途停顿等 Allen 审查
- 每个 WP 完成后 commit 一次（1 个 WP = 1 个 commit，除非 WP 内明确列了多个 commit 点）
- WP 内部的小步也可以多个 commit，保持 atomic

### 1.3 测试策略（override CLAUDE.md）
- **不跑 system_test**（Allen 最后会自己手测）
- **pytest 不需要每步跑**，只在：
  - 某个 WP 明确写了"跑 pytest"的地方跑
  - 所有 WP 完成后跑一次完整 `python -m pytest tests/ -q` 汇总
- 如果某个 WP 改动了被大量测试覆盖的核心文件（jarvis.py / core/llm.py / core/tts.py），WP 结束前跑一次 pytest 专门覆盖相关测试模块

### 1.4 硬性禁止
- 不得动 `data/speechbrain_model/`、`data/sensevoice-small-int8/`
- 不得 hardcode IP / API key / 路径（全部读 `config.yaml`）
- 不得用 `print`，用 `logging`
- 不得绕过 `permission_manager` 做设备操作
- 不得 push
- 不得在 commit message 加 `Co-Authored-By`

### 1.5 交付物
所有 WP 完成后，在本目录生成交付报告：`notes/plans/voice-pipeline-optimization-2026-04-16-report.md`，包含：
- 每个 WP 的 commit hash
- 改动文件清单（总览）
- pytest 汇总结果
- 已知遗留问题 / 未完成项
- 给 Allen 的手测 checklist（每个 WP 该怎么验证）

---

## 2. 工作包总览

### 2.1 速查表

| WP | 名称 | 风险 | 依赖 | 推荐模型 |
|----|------|------|------|----------|
| WP1 | 打断 chunk 调优 | 低 | 无 | Haiku |
| WP3 | TTS 预处理 + vol 修正 | 低 | 无 | Sonnet |
| WP4 | LLM 分句优化（pysbd + faster_first_response） | 中 | 无 | Sonnet |
| WP2 | ASR normalizer 三层 | 中 | 无 | Sonnet |
| WP6 | silero-vad 引擎替换（onnxruntime 直调） | 高 | 无 | Opus |
| WP7 | 打断架构升级（VAD-first 软停 + 关键词硬停） | 高 | WP6 | Opus |
| WP5 | 打断 memory 注入（方案 b） | 中 | WP7 | Sonnet |

### 2.2 执行顺序

```
WP1 → WP3 → WP4 → WP2 → WP6 → WP7 → WP5
```

**顺序逻辑**：
1. WP1 最轻，先热身 + 拿第一波打断延迟改善数据
2. WP3/WP4/WP2 三个用户感知向、互相独立、不碰打断路径
3. WP6 基础设施级重构，独立大改动，专注完成 VAD 引擎替换
4. WP7 在新 VAD 基础上改造打断路径（帧级 prob + 软停 ducking）
5. WP5 最后做，避免 WP7 把打断路径又改一遍导致 WP5 返工

### 2.3 Benchmark Protocol

**目的**：WP1 / WP6 / WP7 都影响打断延迟，需要累计对比。

**方法**：
- 在 `scripts/` 下新建 `bench_interrupt_latency.py`（由 WP1 负责创建基础框架）
- 测试流程：让 TTS 播放一段固定长度的音频（如 10 秒），在第 3 秒从 mic 说"停"，记录从说"停"到 TTS 实际停止的时间戳差
- 每 WP 完成后跑 10 次取中位数，记录到交付报告里

**输出格式**：
```
baseline:     <N> ms
after WP1:    <N> ms  (Δ: -<M> ms)
after WP6:    <N> ms  (Δ: -<M> ms)
after WP7:    <N> ms  (Δ: -<M> ms)
```

### 2.4 关键路径信息（提前定位好）

- **Behavior log**：`memory/behavior_log.py`（WP2 冷启动数据源）
- **Config 所有 section**：`audio / asr / speaker / verification / enrollment / auth / devices / hue / models / llm / tts / wake_word / session / memory / skills / oled / mqtt / remote / scheduler / automations / health / logging / interrupt`（config.yaml 全部顶层 key 已枚举）
- **Interrupt 配置段**：`config.yaml:571-584`
- **Hue 设备配置段**：`config.yaml:190`（WP2 扩展 aliases 要改这里）

---

## 3. WP1 — 打断 chunk 调优

### 3.1 目标
打断延迟从 ~700ms 降到 ~400ms，零架构改动。

### 3.2 改动范围
- `core/interrupt_monitor.py:88` — `_min_chunk_samples` 从 8000 改为 3200
- `config.yaml:571-584` `interrupt` 段 — 把 chunk size 提到 config，不要 hardcode

### 3.3 实施步骤

1. 在 `config.yaml` `interrupt:` 段加：
   ```yaml
   streaming_asr_chunk_samples: 3200  # 200ms @ 16kHz (was 8000/500ms)
   ```
2. 修改 `core/interrupt_monitor.py:88`：
   ```python
   # 旧：self._min_chunk_samples = 8000  # 0.5s @ 16kHz
   # 新：
   self._min_chunk_samples = int(icfg.get("streaming_asr_chunk_samples", 3200))
   ```
3. 创建 `scripts/bench_interrupt_latency.py` 基础框架（目录、CLI、记录格式）

### 3.4 验收标准
- config 驱动 chunk size
- pytest 跑 `tests/` 里打断相关的测试（`grep -l interrupt tests/`）应全绿
- Benchmark 脚本能跑，记录一次 baseline 延迟（在此 WP 里 chunk 已改，所以这次记录的就是 WP1 后的值，baseline 值 Allen 会补）

### 3.5 回滚
把 `streaming_asr_chunk_samples` 改回 8000。

### 3.6 风险
- 200ms chunk 对 sherpa-onnx streaming zipformer 偏短，可能 decode 抖动
- 如果抖动严重（实测中 ASR 结果频繁变化），提升到 4800（300ms）再试

---

## 4. WP3 — TTS 预处理 + vol 修正

### 4.1 目标
- 不再朗读 emoji / 括号 / 星号 / 尖括号 / 特殊字符
- MiniMax 音量从 5.0 改回 1.0，消除爆音

### 4.2 改动范围
- 新建 `core/tts_preprocessor.py`
- 修改 `core/tts.py`（在 `speak()` 和 `synth_to_file()` 入口加 preprocessor 调用）
- 修改 `config.yaml` `tts:` 段，加 `tts_preprocessor:` 子段 + 改 `minimax.volume`

### 4.3 实施步骤

1. 抄 OLV 的 `src/open_llm_vtuber/utils/tts_preprocessor.py`（~80 行），换成 Jarvis 风格：
   - 纯函数，输入 text 和 dict 配置，输出 cleaned text
   - 5 个独立开关：`remove_special_char`、`ignore_brackets`、`ignore_parentheses`、`ignore_asterisks`、`ignore_angle_brackets`
   - 至少 6 个单元测试（每个开关一个 + 综合一个）放 `tests/test_tts_preprocessor.py`
2. `core/tts.py`：
   - 在 `__init__` 里读 `config["tts"]["tts_preprocessor"]` 存为 `self._preprocessor_config`
   - 在 `synth_to_file` 和 `speak` 入口第一行调用 `text = tts_preprocessor.clean(text, self._preprocessor_config)`
3. `config.yaml`：
   ```yaml
   tts:
     # ...已有内容...
     tts_preprocessor:
       remove_special_char: true
       ignore_brackets: true        # [xxx]
       ignore_parentheses: true     # (xxx) （xxx）
       ignore_asterisks: true       # *xxx*
       ignore_angle_brackets: true  # <xxx>
     minimax:
       # 找到现有 volume 字段改成 1.0
       volume: 1.0
   ```

### 4.4 验收标准
- `tests/test_tts_preprocessor.py` 全绿
- 手测输入 `"好的 😊 [开心] *强调* <标签> (旁白) 这是正文"` → TTS 只念"这是正文"
- MiniMax 播放无爆音（Allen 手测）

### 4.5 风险
- 误过滤用户真想念的内容 → 5 个开关独立可关
- `ignore_parentheses` 要处理中英文括号 `()（）`，别漏
- 空白折叠：过滤后产生的多余空格要合并

### 4.6 OLV 源码参考
- `src/open_llm_vtuber/utils/tts_preprocessor.py:7-80`
- `config_templates/conf.default.yaml:469-476`
- `config_templates/conf.default.yaml` MiniMax 段的 `vol: 1.0`

---

## 5. WP4 — LLM 分句优化

### 5.1 目标
- 混中英场景不在 "Dr." "e.g." 等英文缩写处误切
- 首句遇逗号立即切（faster_first_response），首字延迟预期 -50~70%

### 5.2 改动范围
- 依赖：`uv pip install pysbd`（不用 pip，用 uv）
- `core/llm.py:591` `_SENTENCE_DELIMITERS` 相关逻辑
- `config.yaml` `llm:` 段加 `sentence_divider:` 子段

### 5.3 实施步骤

1. 确认 pysbd 安装：
   ```bash
   cd ~/Projects/jarvis && uv pip install pysbd
   ```
2. 在 `core/llm.py` 顶部加：
   ```python
   import pysbd
   _SEGMENTER_ZH = pysbd.Segmenter(language="zh", clean=False)
   _SEGMENTER_EN = pysbd.Segmenter(language="en", clean=False)
   _ABBREVIATIONS = ("Mr.", "Mrs.", "Dr.", "Prof.", "Inc.", "Ltd.",
                     "Jr.", "Sr.", "e.g.", "i.e.", "vs.", "St.", "Rd.")
   ```
3. 替换 `_flush_sentences` 的 delimiter 判断：
   - 原逻辑：扫 `_SENTENCE_DELIMITERS` 任一字符就切
   - 新逻辑：
     - 若 buffer 尾部匹配 `_ABBREVIATIONS` 任一 → 不切
     - 若 `.` 前后都是数字 → 不切（已有逻辑保留）
     - 首句（`_is_first_sentence = True`）额外把 `，,` 加入 delimiter 集合
     - 切完第一句后 `_is_first_sentence = False`
4. `config.yaml`：
   ```yaml
   llm:
     # ...已有内容...
     sentence_divider:
       faster_first_response: true     # 首句遇逗号即切
       abbreviation_protect: true      # 保护英文缩写
   ```
5. 把 `_is_first_sentence` 状态重置点放在 `_process_turn` 开始（每轮对话重置）

### 5.4 验收标准
- 单元测试 `tests/test_llm_sentence_divider.py`（新建）：
  - "Dr. Smith said hello." 不在 Dr. 处误切
  - "你好，世界。" 首句模式下第一个逗号触发切分
  - "你好。世界。" 非首句正常按句号切
  - "3.14 is pi." 数字小数点不误切
- 跑 `python -m pytest tests/test_llm*` 应全绿
- 手测：触发一轮长回复，观察第一句 TTS 触发速度

### 5.5 风险
- pysbd 在流式场景下调用成本：每来一个 delta 就调 segment 可能慢 → **不要用 pysbd 做流式分句**，用 pysbd 只在"已 buffer 的文本判断是否含完整句"时参考；主流程仍是快速 delimiter 扫描 + abbreviation guard。即 pysbd 做**校验**不做**决策**，或干脆只用 abbreviation 白名单方案，pysbd 暂不引入
- 决策后备：如果 pysbd 集成复杂，退化为"只加 abbreviation 白名单"，不引入 pysbd 依赖。此时 §5.3 步骤 1 和步骤 2 里的 pysbd import 都跳过，只保留 `_ABBREVIATIONS` 元组
- 流式 buffer 末尾恰好是 "Dr" 还未等到 "." 时，要等下一个 delta 进来再判断

### 5.6 OLV 源码参考
- `src/open_llm_vtuber/utils/sentence_divider.py:31-46`（abbreviation 列表）
- `src/open_llm_vtuber/utils/sentence_divider.py:213-266`（pysbd 封装）
- `src/open_llm_vtuber/utils/sentence_divider.py:492-507`（faster_first_response 实现）

---

## 6. WP2 — ASR normalizer 三层

### 6.1 目标
解决 Jarvis 中场景名 / Hue 设备名的 ASR 识别错误，不换 ASR 模型。

### 6.2 架构

**三层级联**，第一个命中即返回：

```
Layer 1: 手动 override 字典（硬规则 + context guard）
Layer 2: 结构化别名（Hue / scene aliases）
Layer 3: Levenshtein 兜底（代码框架写完但默认关闭）
```

### 6.3 改动范围
- 新建 `core/asr_normalizer.py`
- 新建 `tests/test_asr_normalizer.py`
- 修改 `config.yaml`：
  - 新增顶层 `asr_corrections:` 段
  - `hue:` 和 `scenes:`（或等价位置）给每个 canonical 名字加 `aliases:`
- 修改 `jarvis.py` — `_process_turn` 内 ASR 调用后插入 normalizer
- 修改 `memory/behavior_log.py` 或查询工具（冷启动数据提取，可选）

### 6.4 Layer 1 — 手动 override 字典

**config.yaml 新增**：
```yaml
asr_corrections:
  - pattern: 客厅大蛋
    replace: 客厅大灯
    require_context: [开, 关, 调, 亮, 暗, 灯, 模式]
  - pattern: 放送模式
    replace: 放松模式
    require_context: [模式, 开, 启动, 切换]
```

**规则**：
- 每条必须带 `require_context`（不能为空）
- 匹配 pattern 且文本含至少一个 context 词 → 替换
- 否则不触发（避免"大蛋糕"被改"大灯糕"）

### 6.5 Layer 2 — 结构化别名

**扩展 config.yaml**（在现有 `hue:` 段内找到设备列表位置，给每个设备加 `aliases`；scenes 同理）：
```yaml
hue:
  devices:
    - name: 客厅大灯
      id: 1
      aliases: [客厅主灯, 客厅顶灯, 大灯, 主灯]
    - name: 卧室壁灯
      id: 3
      aliases: [床头灯, 卧室小灯, 壁灯]

scenes:
  - name: 放松模式
    aliases: [休闲模式, 轻松模式]
  - name: 影院模式
    aliases: [看电影模式, 电影模式]
```

**规则**：
- 扫描 ASR 文本，含 alias → 替换为 canonical
- 全匹配，不需 context guard（alias 本身已是明确语义）
- **冷启动数据**：Allen 自己填，执行 agent 先写空列表占位

### 6.6 Layer 3 — Levenshtein 兜底

- 写代码，默认 `enabled: false`
- 位置：`core/asr_normalizer.py` 内 `_layer3_fuzzy_match` 方法
- 依赖：手写 Levenshtein（不新增 python-Levenshtein 依赖，手写 20 行够用）
- 规则：
  - 从 config 读所有 canonical + alias 列表
  - 对 ASR 文本做 2-5 字滑窗
  - 计算 Levenshtein 距离
  - 距离 ≤ 2 且文本含设备动作词（"开/关/调/亮/暗/模式"）→ 替换
  - 距离 > 2 或无动作词 → 不触发

### 6.7 模块接口

```python
# core/asr_normalizer.py
class ASRNormalizer:
    def __init__(self, config: dict) -> None: ...

    def normalize(self, text: str, intent_hint: str | None = None) -> str:
        """三层级联：Layer 1 → Layer 2 → Layer 3，第一个命中的返回."""
```

### 6.8 调用链插入点

`jarvis.py` `_process_turn` 方法内，ASR 返回后、进入 intent_router 之前：

```python
# 现状
text, emotion = speech_recognizer.transcribe(audio)

# 改后
text, emotion = speech_recognizer.transcribe(audio)
text = self.asr_normalizer.normalize(text)  # 新增
```

`self.asr_normalizer` 在 `Jarvis.__init__` 创建。

### 6.9 验收标准
- 单元测试 `tests/test_asr_normalizer.py`：
  - Layer 1 命中 + context guard 生效（"开客厅大蛋" → "开客厅大灯"）
  - Layer 1 pattern 匹配但无 context → 不触发（"我想吃大蛋糕" → 不变）
  - Layer 2 alias 替换（"开床头灯" → "开卧室壁灯"）
  - Layer 3 fuzzy 默认关闭不生效
  - Layer 3 手动启用后距离 ≤ 2 命中替换
  - 级联优先级（L1 命中后不走 L2/L3）
- `pytest tests/test_asr_normalizer.py` 全绿

### 6.10 性能要求
- 整个 normalize 应 < 10ms
- Layer 1 是 hashmap 查找：O(1) × len(corrections)
- Layer 2 是字典扫描：O(N × M) 其中 N=text 长度、M=aliases 总数
- Layer 3 是滑窗 + Levenshtein：O(N × M × W²) 其中 W=最长 alias 长度，所以**默认关闭合理**

### 6.11 冷启动数据策略
- **Layer 1 corrections**：Allen 翻近两周 behavior log（`memory/behavior_log.py`），找 `intent=device/scene` 执行失败的记录，人肉对比 ASR 原文 vs 意图。**这步由 Allen 本人完成**，执行 agent 在 config 只留 2-3 条占位示例（如上面的"客厅大蛋/放送模式"）。
- **Layer 2 aliases**：Allen 自己填（他清楚自己怎么叫每个设备/场景）。执行 agent 在 config 留 2-3 个占位示例。

---

## 7. WP6 — silero-vad 引擎替换

### 7.1 目标
从 sherpa-onnx 封装的 VoiceActivityDetector 切换到 **onnxruntime 直调 `silero_vad.onnx`**，拿到帧级 prob / db 原始数据，为 WP7 打断架构升级打基础。

### 7.2 核心原则
- **不新增依赖**：复用已有的 `onnxruntime`（sherpa-onnx 的传递依赖）
- **不换模型文件**：复用已有的 `data/silero_vad.onnx`
- **保留 fallback**：config 可切换回 sherpa-onnx 封装版本
- **不改麦克风接入模式**：InputStream 仍按需开，不做常驻 stream（那是独立议题）

### 7.3 改动范围
- 新建 `core/vad_silero.py`（~150-200 行，核心 LSTM state 管理 + 帧级推理）
- 新建 `tests/test_vad_silero.py`
- 修改 `core/audio_recorder.py` — `_build_vad` 支持 provider 开关
- 修改 `core/interrupt_monitor.py` — `_load_vad` 支持 provider 开关
- 修改 `config.yaml`：
  - `audio:` 段加 `vad_provider: "silero_direct"`（新）或 `"sherpa_onnx"`（旧 fallback）
  - `audio:` 段加 silero 专用参数：`vad_db_threshold`、`vad_smoothing_window`、`vad_required_hits`、`vad_required_misses`、`vad_pre_buffer_frames`
  - `interrupt:` 段同上

### 7.4 模块设计

**`core/vad_silero.py` 接口**：
```python
class SileroVADDirect:
    """onnxruntime 直调 silero_vad.onnx，提供帧级 prob + db."""

    def __init__(
        self,
        model_path: str,
        sample_rate: int = 16000,
        prob_threshold: float = 0.4,
        db_threshold: float = 60.0,
        smoothing_window: int = 5,
        required_hits: int = 3,        # 连续 N 帧判 START
        required_misses: int = 24,     # 连续 N 帧判 END
    ) -> None: ...

    def accept_waveform(self, audio: np.ndarray) -> list[VADEvent]:
        """喂一块音频（任意长度），返回帧级事件列表.

        VADEvent 包括：
          - FrameEvent(prob, db, timestamp)     # 每 32ms 一次
          - StartEvent(timestamp)                # hits 达标
          - EndEvent(timestamp)                  # misses 达标
        """

    def is_speech_detected(self) -> bool:
        """兼容旧接口，等价于 '当前是否在 ACTIVE 状态'."""

    def reset(self) -> None:
        """重置 LSTM state + 状态机，每次录音会话开始调用."""

    def empty(self) -> bool:
        """兼容旧接口，等价于 '自上次 reset 后是否检测到过完整语音段'."""
```

**LSTM state 管理细节**（最易翻车的点，详写）：

silero_vad.onnx 输入输出签名：
```
Input:
  - input: float32[1, N]      # N 必须是 512 (16kHz @ 32ms)
  - state: float32[2, 1, 128] # LSTM h, c
  - sr: int64                 # 16000

Output:
  - output: float32[1, 1]     # prob
  - stateN: float32[2, 1, 128]  # 更新后的 state，下次喂回来
```

**关键实施步骤**：
1. `__init__` 里 `self._state = np.zeros([2, 1, 128], dtype=np.float32)`
2. `accept_waveform` 把输入音频切成 512-sample chunks（不足补零或缓存到下次）
3. 每个 chunk 调 `session.run([...], {"input": chunk, "state": self._state, "sr": np.array(16000, dtype=np.int64)})`
4. 更新 `self._state = output[1]`
5. 计算 dB：`20 * np.log10(np.sqrt(np.mean(chunk**2)) + 1e-10)`
6. smoothing：`deque(maxlen=5)` 对 prob / db 分别取 mean
7. 状态机：hits/misses 计数，转换 IDLE → ACTIVE → INACTIVE → IDLE
8. `reset()` 里重置 state、deque、计数器

### 7.5 Config schema 变更

`config.yaml` `audio:` 段：
```yaml
audio:
  # ... 已有 ...

  # VAD provider 选择
  vad_provider: silero_direct  # "silero_direct"(新) | "sherpa_onnx"(fallback)

  # silero_direct 专用参数（sherpa_onnx 忽略）
  vad_prob_threshold: 0.4        # 替代旧的 vad_threshold（后者仅 sherpa_onnx 用）
  vad_db_threshold: 60.0
  vad_smoothing_window: 5
  vad_required_hits: 3
  vad_required_misses: 24

  # 旧字段保留给 fallback 用
  vad_threshold: 0.5
  vad_silence_duration: 0.5
  vad_min_speech_duration: 0.25
  vad_max_speech_duration: 20.0
```

`config.yaml` `interrupt:` 段：
```yaml
interrupt:
  # ... 已有 ...

  vad_provider: silero_direct  # TTS 播放期间的 VAD 也升级
  vad_prob_threshold_during_tts: 0.5   # 比录音模式高，抗 AEC 残余
  vad_db_threshold_during_tts_mac: 72.0   # Mac 本地扬声器 dB 偏高
  vad_db_threshold_during_tts_rpi: 62.0   # RPi5 + ReSpeaker 偏低
  vad_smoothing_window: 5
```

**运行时选择 dB 阈值**：
```python
import platform
if platform.system() == "Darwin":
    db_threshold = cfg.get("vad_db_threshold_during_tts_mac", 72.0)
else:
    db_threshold = cfg.get("vad_db_threshold_during_tts_rpi", 62.0)
```

### 7.6 audio_recorder.py / interrupt_monitor.py 适配

**`core/audio_recorder.py`** `_build_vad` 改造：
```python
def _build_vad(self, cfg: dict) -> Any:
    provider = cfg.get("vad_provider", "sherpa_onnx")
    if provider == "silero_direct":
        from core.vad_silero import SileroVADDirect
        return SileroVADDirect(
            model_path=str(cfg["vad_model_path"]),
            prob_threshold=float(cfg.get("vad_prob_threshold", 0.4)),
            db_threshold=float(cfg.get("vad_db_threshold", 60.0)),
            smoothing_window=int(cfg.get("vad_smoothing_window", 5)),
            required_hits=int(cfg.get("vad_required_hits", 3)),
            required_misses=int(cfg.get("vad_required_misses", 24)),
        )
    # fallback 到原 sherpa_onnx 封装
    import sherpa_onnx
    # ... 保持原代码不动
```

**`AudioRecorder.record`** 的 callback 内原逻辑：
```python
if self._vad is not None and captured_frames >= min_frames:
    self._vad.accept_waveform(chunk)
    if not self._vad.empty():
        ...
```

`SileroVADDirect` 要**兼容 `empty()` 和 `accept_waveform()` 语义**，让这段代码不变：
- `accept_waveform(chunk)` 内部跑帧级推理 + 维护状态机
- `empty()` 返回 `not self._segment_completed`（是否完成了一个完整语音段）

这样 WP6 只改 VAD 内部实现，`AudioRecorder.record` 本身不动。

**`core/interrupt_monitor.py`** `_load_vad` 同理改造，支持 provider 开关。

### 7.7 单元测试
`tests/test_vad_silero.py`：
- 静音输入 → prob 始终 < threshold，不触发 START
- 合成语音（用 numpy 生成 440Hz sine + 随机噪声）→ 触发 START → END
- state 在 reset 后正确回到零
- 512-sample chunk 边界正确处理（输入 1000 samples 自动切成 1×512 + 缓存 488）
- smoothing 正确：单帧高 prob 不立即触发，连续 3 帧才触发
- 连续 24 帧低 prob 才触发 END

### 7.8 启动预热
silero_vad.onnx 首次推理有 ~200ms 冷启动。`SileroVADDirect.__init__` 最后跑一次 dummy chunk 预热：
```python
dummy = np.zeros(512, dtype=np.float32)
self.accept_waveform(dummy)
self.reset()
```

### 7.9 验收标准
- `pytest tests/test_vad_silero.py` 全绿
- `config.yaml` 切到 `silero_direct` 后，`python jarvis.py --no-wake` 能启动、录音、ASR 正常（手测由 Allen 做，执行 agent 只需启动能不报错）
- `vad_provider: sherpa_onnx` 切回时行为与替换前一致（兼容性）
- 不新增 pip 依赖（跑 `uv pip list | grep -i silero` 应无结果）

### 7.10 风险
- **LSTM state 污染**：多次录音会话间 reset 必须调用，否则前一轮的 state 会影响后一轮
- **chunk 边界**：512 samples 固定，任意输入长度要正确切分 + 残余缓存
- **首帧冷启动**：预热机制必须在 `__init__` 里跑
- **sherpa-onnx fallback 丢失**：保留旧路径作为 `vad_provider: sherpa_onnx`，不得删
- **AEC 场景 dB 阈值**：Mac 和 RPi 差异大，必须分平台默认值（已在 §7.5 体现）
- **onnxruntime 版本兼容**：silero_vad.onnx 要求 opset ≥ 13，现有 onnxruntime 肯定满足，无需特殊处理

### 7.11 OLV 源码参考
- `src/open_llm_vtuber/vad/silero.py:1-188` — 整个文件都是参考，尤其 `_init`、`_infer`、状态机部分
- OLV 的 silero.py 用 torch 跑推理，我们换成 onnxruntime.InferenceSession

---

## 8. WP7 — 打断架构升级

### 8.1 目标
基于 WP6 的帧级 VAD prob，实现：
- **VAD-first 软停**：VAD 检测到用户说话 → TTS 音量 ducking 到 30%，同时开启 ASR 关键词识别
- **关键词硬停**：ASR 识别到 `INTERRUPT_KEYWORDS` → TTS 完全停止 + 触发 cancel
- **非关键词恢复**：如果 3 秒内没识别到关键词 → TTS 音量恢复 100%
- 整体打断感知延迟从 400ms（WP1 后）降到 150-250ms

### 8.2 状态机设计

```
[NORMAL] TTS 100% 音量
   │
   │ VAD detect start (帧级 prob 连续 3 帧 > 0.5)
   ↓
[DUCKED] TTS 30% 音量 + ASR 激活
   │
   ├── 关键词命中 ──→ [CANCELLED] TTS stop + on_interrupt callback
   │
   ├── 3s timeout no keyword ──→ [NORMAL] 音量恢复
   │
   └── VAD detect end (连续 24 帧低 prob) + 无关键词 ──→ [NORMAL] 音量恢复
```

### 8.3 改动范围
- 修改 `core/interrupt_monitor.py`：
  - 新增软停/硬停状态机
  - 利用 WP6 的帧级 VAD 事件
  - 管理 3s timeout
  - 触发 TTS 音量 ducking
- 修改 `core/tts.py`：
  - 新增 `duck_volume(level: float)` 方法，能在播放中改音量
  - 实施方式：afplay 不支持音量热调，需要切到其他播放器 or 用 ffplay `-volume` 参数，**或更简单的方案**：在 TTS 合成阶段产出两个副本（100% / 30%），收到 duck 信号时切换播放进程
- 修改 `config.yaml` `interrupt:` 段加软停参数
- 修改 `jarvis.py`：可能需要协调 `_cancel_current` 与软停路径

### 8.4 音量 ducking 实施方案

**三个选项，选最简单的**：

**选项 A（推荐）：切换播放器到 ffplay**
- Mac/Linux 都有 ffplay（ffmpeg 自带）
- `ffplay -volume 100 -nodisp -autoexit file.mp3` 启动播放
- 发 SIGUSR1 或 stdin 命令调音量不行 → ffplay 不支持运行时调音量
- **实际做法**：kill 当前 ffplay + 以新音量重启，seek 到上次播放位置
- 复杂度：高。估计 ~150 行

**选项 B：预合成双音量版本**
- TTS 合成完一句后，用 pydub 再生成一份 30% 音量版本
- 平时播 100%，收到 duck 信号时 `kill 100% proc + start 30% proc from offset`
- 复杂度：中。估计 ~100 行
- 缺点：每句 TTS 要多存一个副本，磁盘 ×2

**选项 C（最简单）：不真正 duck，改成暂停 + 快速恢复**
- 收到 VAD start → `proc.suspend()`（SIGSTOP on Unix）+ 开 ASR
- 3s timeout no keyword → `proc.resume()`（SIGCONT）
- 关键词命中 → `proc.terminate()`
- 复杂度：低。估计 ~30 行
- 缺点：用户体验上不是"小声继续播"，而是"静音等待"，但这个差别对打断场景可接受
- **强烈推荐选项 C** —— ROI 最高

### 8.5 实施步骤（选项 C）

1. `core/tts.py` 的 `_play_audio_file` 方法里存 `self._current_proc` 引用
2. 新增方法：
   ```python
   def suspend_playback(self) -> None:
       if self._current_proc and self._current_proc.poll() is None:
           os.kill(self._current_proc.pid, signal.SIGSTOP)

   def resume_playback(self) -> None:
       if self._current_proc and self._current_proc.poll() is None:
           os.kill(self._current_proc.pid, signal.SIGCONT)
   ```
3. `core/interrupt_monitor.py` 状态机：
   - 从 `SileroVADDirect.accept_waveform` 拿帧事件
   - 收到 `StartEvent` → 切到 DUCKED → 调 `tts.suspend_playback()`
   - ASR 继续跑，关键词命中 → 切到 CANCELLED → 调现有 `on_interrupt` callback
   - 3s timer 或 `EndEvent` → 切回 NORMAL → 调 `tts.resume_playback()`

4. 配置：
   ```yaml
   interrupt:
     # ...已有 + WP6 新增...
     soft_stop_enabled: true              # WP7 新增
     soft_stop_timeout_ms: 3000           # 无关键词 3s 后恢复播放
     soft_stop_method: suspend            # "suspend" (选项C) | "duck" (选项 A/B 未来)
   ```

### 8.6 验收标准
- 单元测试（mock 音频 + mock TTS proc）：
  - VAD 触发 START → `suspend_playback` 被调用
  - 3s 内关键词命中 → `on_interrupt` 触发
  - 3s timeout 无关键词 → `resume_playback` 被调用
  - 关键词命中优先于 timeout（先命中先触发）
- Allen 手测：TTS 播放中说"停"，能在 ~200ms 内暂停并取消

### 8.7 风险
- **SIGSTOP/SIGCONT 在 macOS afplay 上行为**：macOS 不保证 afplay 被 STOP 后 audio buffer 不崩；需要实测，不行换 ffplay
- **Windows 不支持 SIGSTOP**：Jarvis 主要跑 Mac / RPi5，Windows 不在支持范围，但 `core/tts.py` 本身 cross-platform，要加 `if platform.system() in ("Darwin", "Linux")` guard，Windows 下禁用软停
- **状态机并发**：VAD 事件从 `interrupt-mic` 线程来，状态变化要加锁
- **3s 窗口里 TTS 播放进度丢失**：suspend 期间播放进度冻结，resume 后从原位置继续（SIGSTOP 是进程级冻结，不会丢状态）
- **如果 TTS 队列还有下一句未播**：当前句 suspend 时，`_play_worker` 不应继续 pop 下一句 → 要在 suspend 状态标志内 gate 住 worker

### 8.8 依赖
- 依赖 WP6：需要帧级 VAD 事件
- 如果 WP6 里 `SileroVADDirect` 没暴露 `StartEvent` / `EndEvent` 粒度，WP7 会卡住。WP6 实施时必须保证接口完整

---

## 9. WP5 — 打断 memory 注入

### 9.1 目标（方案 b）
打断发生时，把 LLM 对话历史里 assistant 的内容**改为"已播完的完整句子拼接"**（不含正在播的那句），再 append `[Interrupted by user]` 标记，让 LLM 知道"我没说完"。

**示例**：
- LLM 回复全文：句 S1、句 S2、句 S3
- TTS 状态：S1 已播完 + S2 已播完 + S3 正在播到一半
- 用户打断 → heard_response = S1 + S2（不含 S3）
- History 里 assistant.content 改成 `"<S1><S2>..."`
- Append `{role:"user", content:"[Interrupted by user]"}`

### 9.2 改动范围
- 修改 `jarvis.py` `_cancel_current` 方法：
  - 收集已播完句子 + append interrupted 标记到 conversation_history
- 修改 `core/tts.py` `abort()` 方法（如果还不够）：
  - 确保能分辨"已播完句"和"未播完当前句"
- 可能修改 `memory/store.py`：
  - 确认 conversation_history 的写入时机不会覆盖打断后的修改

### 9.3 与现有机制的交互

**现有 Jarvis 行为**（resume replay）：
- `_cancel_current` 把 `pipeline.abort()` 返回的"未播放文本列表"存到 `self._interrupted_response`
- 下一轮若用户说"继续说" → 把 `_interrupted_response` 逐句重新 output + 写 history

**两套机制如何共存**：

打断发生时：
1. `pipeline.abort()` 返回 `unplayed_sentences`（未播队列中的句子 + 正在播的当前句）
2. 计算 `played_sentences = all_submitted_sentences - unplayed_sentences`
3. **History 修改**（WP5 新增）：
   - `heard_response = "".join(played_sentences)` — 已播完的完整句子
   - `conversation_history[-1].content = heard_response + "..."` （改 assistant 那条）
   - `conversation_history.append({"role": "user", "content": "[Interrupted by user]"})`
4. **resume replay 保留**（现有）：
   - `self._interrupted_response = unplayed_sentences`
   - 下轮说"继续" → 从 `_interrupted_response` 恢复

**关键细节**：正在播的当前句进入 `unplayed_sentences`（方案 b 语义），不计入 `played_sentences`。

### 9.4 实施步骤

1. `core/tts.py` `abort()` 确认返回值包含"已完全播完的句子列表"——当前代码可能只返回"队列中剩余的"（需读源码确认，若不够则改造）
2. 如果 `abort()` 返回的信息不够，加一个计数器：
   - `self._played_sentence_count` — 在 `_play_worker` 每次播完一句后 +1
   - `abort()` 额外返回 `played_count` + `played_text_buffer`
3. `jarvis.py` `_cancel_current`：
   ```python
   def _cancel_current(self) -> None:
       pipeline = self._active_pipeline
       if pipeline is None:
           return
       unplayed, played = pipeline.abort()  # 新返回值
       self._interrupted_response = unplayed
       if played:
           heard_response = "".join(played)
           # 修改 conversation_history 最后一条 assistant
           self._truncate_assistant_history(heard_response)
           self._append_interrupted_marker()
   ```
4. 新增 helper：
   ```python
   def _truncate_assistant_history(self, heard_response: str) -> None:
       if not self.conversation_history:
           return
       last = self.conversation_history[-1]
       if last.get("role") == "assistant":
           last["content"] = heard_response + "..."

   def _append_interrupted_marker(self) -> None:
       self.conversation_history.append({
           "role": "user",
           "content": "[Interrupted by user]"
       })
   ```

### 9.5 验收标准
- 单元测试（需 mock TTS pipeline）：
  - abort 时 3 句中已播 2 句 → history assistant 内容改为前 2 句拼接 + "..."
  - abort 时 3 句都未播（打断在 LLM 还没 flush 第一句时发生）→ assistant 内容改为 "..."
  - abort 后 history 末尾有 `{role: "user", content: "[Interrupted by user]"}`
  - resume replay 功能保留：下轮"继续"后 `_interrupted_response` 被消费
- 集成手测：打断后 dump sqlite，查 assistant 最后一条内容是未说完的版本

### 9.6 风险
- **provider 差异**：OLV 按 provider 用 `user` 或 `system` role 注入 interrupted marker。Jarvis 当前用 Grok（openai-compatible），统一用 `user` role 即可
- **conversation_history 写入时机**：如果 `_process_turn` 在 LLM 流式吐字时就写 history，打断后要保证我们改的是最后一条、不被后面的写入覆盖。读源码确认写入时机，必要时加锁
- **resume replay 与 interrupted marker 冲突**：下轮用户说"继续" → Jarvis 重念未播内容 → 这次的 LLM 调用会看到 history 里有 `[Interrupted by user]` + 本轮 user 输入"继续"。LLM 可能困惑。处理方式：resume 路径里**不走** LLM（现有行为已是这样，直接从 `_interrupted_response` output），history 写入时要特殊处理（把 interrupted marker 去掉或补一条 assistant resume completion）

### 9.7 OLV 源码参考
- `src/open_llm_vtuber/agent/agents/basic_memory_agent.py:195-223`（handle_interrupt 核心逻辑）
- `src/open_llm_vtuber/conversations/conversation_handler.py:129-143`（history 写入）

---

## 10. 最终验证 checklist

全部 WP 完成后执行：

1. **pytest 汇总**：
   ```bash
   cd ~/Projects/jarvis && python -m pytest tests/ -q 2>&1 | tail -30
   ```
   记录通过数 / 失败数到交付报告。

2. **启动冒烟**：
   ```bash
   python jarvis.py --no-wake
   ```
   能启动不报错即可，不实际交互（Allen 手测）。

3. **Config 自检**：
   ```bash
   python -c "import yaml; yaml.safe_load(open('config.yaml'))"
   ```
   确保 yaml 语法无误。

4. **Benchmark 汇总**：
   跑 `scripts/bench_interrupt_latency.py` 若干次，把中位数写入交付报告。

5. **Git log 检查**：
   ```bash
   git log --oneline -20
   ```
   确认每个 WP 有对应 commit，message 无 Co-Authored-By。

---

## 11. 交付报告模板

执行 agent 完成所有 WP 后，创建 `notes/plans/voice-pipeline-optimization-2026-04-16-report.md`，按模板填：

```markdown
# Voice Pipeline Optimization — 交付报告

**执行日期**：YYYY-MM-DD
**执行 agent**：<model name>
**基于计划**：notes/plans/voice-pipeline-optimization-2026-04-16.md

## 1. WP 完成状态

| WP | 状态 | Commit hash | 主要改动文件 | 备注 |
|----|------|------------|-------------|------|
| WP1 | ✅/⚠️/❌ | abcd123 | core/interrupt_monitor.py | ... |
| ... | | | | |

## 2. Pytest 汇总

- 总用例数：N
- 通过：N
- 失败：N（失败清单）
- 跳过：N

## 3. Benchmark 结果

Interrupt latency (ms, median of 10):
- Baseline (Allen 补): ? 
- After WP1: ?
- After WP6: ?
- After WP7: ?

## 4. 已知遗留问题

- ...

## 5. Allen 手测 checklist

- [ ] WP1: 打断 TTS，观察从说话到停止的延迟（期望 ~400ms）
- [ ] WP2: 说"开客厅大蛋" → 应该正确开客厅大灯
- [ ] WP3: 让 LLM 回复含 emoji/括号 → TTS 不读
- [ ] WP4: 让 LLM 说"Dr. Smith said hello" → 不在 Dr. 处断句
- [ ] WP4: 让 LLM 回复首句有逗号 → 触发 TTS 比之前快
- [ ] WP6: 切 `vad_provider: sherpa_onnx` 能回退，切 `silero_direct` 能用
- [ ] WP7: 播放中说"嗯嗯" → 音量不降（帧级但无关键词无触发）
- [ ] WP7: 播放中说"停" → TTS 暂停 + 最终取消
- [ ] WP7: 播放中说"嗯嗯嗯"3 秒不说关键词 → TTS 自动恢复播放
- [ ] WP5: 打断后 dump sqlite 看 assistant content 是未说完版本

## 6. 配置变更清单

列出 config.yaml 新增/修改的字段及默认值。

## 7. 未覆盖 / 未来议题

- （如果某 WP 部分降级实施，在此说明）
```

---

## 12. 附录：OLV 源码地图（执行 agent 快速查询）

| 功能 | OLV 文件 | 行号 |
|------|---------|------|
| TTS preprocessor | `src/open_llm_vtuber/utils/tts_preprocessor.py` | 7-80 |
| 句子切分 pysbd | `src/open_llm_vtuber/utils/sentence_divider.py` | 213-266 |
| faster_first_response | `src/open_llm_vtuber/utils/sentence_divider.py` | 492-507 |
| 缩写白名单 | `src/open_llm_vtuber/utils/sentence_divider.py` | 31-46 |
| Silero VAD 核心 | `src/open_llm_vtuber/vad/silero.py` | 1-188 |
| 打断 handler | `src/open_llm_vtuber/conversations/conversation_handler.py` | 112-143 |
| memory 注入 | `src/open_llm_vtuber/agent/agents/basic_memory_agent.py` | 195-223 |
| MiniMax TTS | `src/open_llm_vtuber/tts/minimax_tts.py` | 48-86 |
| 默认 config | `config_templates/conf.default.yaml` | 全文参考 |

---

**END OF PLAN**
