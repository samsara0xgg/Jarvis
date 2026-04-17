# Voice Pipeline Debug Fix — 设计文档

**日期**：2026-04-16
**Owner**：Opus 4.7（本轮修复 agent）
**基于**：`notes/plans/voice-pipeline-optimization-2026-04-16.md`（原执行计划）+ `voice-pipeline-optimization-2026-04-16-report.md`（交付报告）
**范围选项**：C — 全 17 项遗留项（**16 项需代码改动** + 1 项 T2.5 决定"不改代码只加 docstring 说明"）
**结构选项**：A — 按 WP 分组，TDD 风格 commit

---

## 0. 背景

原计划由上一轮 brainstorming 跳过代码级设计直接执行，产出 11 个文件、7 个 WP、963 passed tests。已有 4 个 follow-up 修复 commits（`367ffac` 单位纠正、`ce89ef0` bench 时间戳、`e9d1eaf` 测试 pin provider、`72b0466` 跨轮状态污染）。

Allen + 我二轮审计确定 **17 项遗留项**（Allen 9 + 我 8），其中 **16 项需代码/测试改动**，1 项（T2.5 换行吞噬）经权衡决定保持现状仅更新 docstring。本设计文档给出每项的根因 + 修复方案 + 测试策略 + commit 边界，供下一步 `writing-plans` 产出实施计划。

---

## 1. Bug Inventory

| ID | WP | 严重度 | 简述 |
|----|----|------|------|
| **T1.1** | WP3 | Tier1 | MiniMax `vol` 传 float，官方 API 是 int 0-10 |
| **T1.2** | WP3 | Tier1 | `remove_special_characters` 吞 Sc 类（`¥$€`） |
| **T1.3** | WP4 | Tier1 | `_possible_abbreviation_prefix` 无 word-boundary，`Welcome.` 被误判为 `e.g.` 前缀，延迟 1 delta |
| **T1.4** | WP6 | Tier1 | `vad_silero.py` 代码默认 `db_threshold=60.0/72.0/62.0` 是 SPL 正值，与 dBFS 负值 config 不匹配 → config key 缺失时 VAD 永不触发 |
| **T1.5** | WP2 | Tier1 | `handle_text`（web/text 前端）完全绕过 asr_normalizer |
| **T1.6** | WP3 | Tier1 | preprocessor 过滤顺序：asterisks/brackets/parens/angle 在 NFKC 之前 → 全角变体字符漏过 |
| **T2.1** | WP2 | Tier2 | `_ACTION_WORDS` 含单字 `"灯"` → L3 fuzzy 启用时任何含"灯"的话都可能误匹配 |
| **T2.2** | WP2 | Tier2 | L3 `_apply_fuzzy` 允许 2 字窗口替 4 字 canonical → 文本膨胀（L3 默认关闭，但是定时炸弹） |
| **T2.3** | WP7 | Tier2 | `InterruptMonitor._recording`/`_fired` 无锁写 + `start()` 状态重置无锁 |
| **T2.4** | WP7 | Tier2 | `_on_soft_timeout` 与 `stop()` 争锁 → 冗余 callback（`resume_playback` 幂等所以无 functional bug，但有 log 噪声） |
| **T2.5** | WP3 | Tier2 | `_collapse_whitespace` 吞换行 → 多段文本合并成单行朗读（minor，大多数场景无感） |
| **T3.1** | WP3 | 测试 | 无 currency/digit/number 保留测试 |
| **T3.2** | WP4 | 测试 | 只测 2 个缩写（Dr./e.g.），plan 列了 13 个；`_possible_abbreviation_prefix` 无直接测试；`generate_response_stream` 入口 `_is_first_sentence` 重置无断言 |
| **T3.3** | WP6 | 测试 | 无 "config.yaml 生产默认值 + 真模型" 的 end-to-end sanity 测试（静音不触发 / 说话触发） |
| **T3.4** | WP1 | 测试 | `streaming_asr_chunk_samples` buffer 积攒逻辑无专门断言（小 chunk 累积 → 到阈值才 decode） |
| **T3.5** | WP2 | 测试 | `normalize()` 性能 <10ms 无断言 |
| **T3.6** | report | 文档 | 手测 checklist 未更新包含本轮修复项，也未确认 `soft_stop_enabled=true` 默认切换时机 |

**总计 17 项登记**；其中 **16 项需代码/测试改动**（T2.5 不改代码）。按桶：Tier1: 6 · Tier2: 4 需改 + 1 不改 · 测试补齐: 5 · 文档: 1。

---

## 2. 每项修复设计

### T1.1 — MiniMax `vol` 类型

**现状**：`core/tts.py:126`
```python
self.minimax_volume = float(tts_config.get("minimax_volume", 1.0))
```
**根因**：MiniMax `/t2a_v2` API 的 `vol` 字段是 int 0-10。传 `1.0` 虽然 JSON 序列化会是 `1.0` 数字，但部分 API 网关对 schema 严格校验（OpenAPI integer type）会 422。即使不 422，官方示例全是 int，稳妥起见对齐。

**修复**：
- `minimax_volume` 改为 int，range clamp `[1, 10]`：
  ```python
  raw_vol = tts_config.get("minimax_volume", 1)
  try:
      vol = int(round(float(raw_vol)))
  except (TypeError, ValueError):
      vol = 1
  self.minimax_volume = max(1, min(10, vol))
  ```
- `_synth_minimax` payload 处 `vol` 字段无需改（int 会被 `json.dumps` 输出成整数）

**测试**（`tests/test_tts_cache.py` 或新 `TestMinimaxVolume` 类）：
- `minimax_volume=1.0` → self.minimax_volume == 1（int 类型）
- `minimax_volume=7.8` → 8
- `minimax_volume="bad"` → 1（fallback 无异常）
- `minimax_volume=20` → 10（上限 clamp）
- `minimax_volume=0` → 1（下限 clamp）

---

### T1.2 — Symbol/currency 被吞

**现状**：`core/tts_preprocessor.py:73-77`
```python
def keep(char: str) -> bool:
    cat = unicodedata.category(char)
    return cat[0] in ("L", "N", "P") or char.isspace()
```

**根因**：Unicode 类别 `Sc`（Symbol, currency，如 `¥$€£￥`）被 drop。TTS 回复含 `$100` / `¥150` 变成 `100` / `150`，数字失去单位。

**修复**：白名单扩展到 `Sc`，仍 drop `Sm`（数学符号）/`So`（其他符号，含大多数 emoji）/`Sk`（修饰符号）：
```python
def keep(char: str) -> bool:
    cat = unicodedata.category(char)
    if cat[0] in ("L", "N", "P"):
        return True
    if cat == "Sc":  # currency
        return True
    return char.isspace()
```

**测试**（`tests/test_tts_preprocessor.py::TestRemoveSpecialChar`）：
- `"¥100"` → `"¥100"`
- `"$5 + €3"` → `"$5 + €3"`
- `"😊 ¥100"` → `" ¥100"`（emoji 丢，¥ 留）
- `"1 + 2 = 3"` 含 `+` `=`（`Sm` 数学符号）→ 仍丢（保持现状）
- `"¥100 😊"` → `"¥100 "`

---

### T1.3 — `_possible_abbreviation_prefix` 无 word-boundary

**现状**：`core/llm.py:775-783`
```python
for abbr in self._ABBREVIATIONS:
    for offset in range(min(len(abbr), dot_idx + 1)):
        head = abbr[: offset + 1]
        if head and head[-1] == "." and buffer[dot_idx + 1 - len(head): dot_idx + 1] == head:
            if offset + 1 < len(abbr):
                return True
```

**根因**：对 `"Welcome."`，`head = "e."` 匹配 buffer 末尾 `"e."`，返回 `True` → streaming 模式下延迟切分一个 delta（~30-50ms 首句延迟）。没有"head 起点必须是词边界"的守卫。

**修复**：加 word-boundary 检查 —— `head` 起点（`buffer[dot_idx + 1 - len(head)]`）的前一位必须是非 ASCII 字母或 buffer 开头：
```python
start = dot_idx + 1 - len(head)
if start < 0:
    continue
if buffer[start: start + len(head)] != head:
    continue
# Word-boundary guard: prev char must be non-alpha, or head at buffer start
prev_is_word = start > 0 and buffer[start - 1].isalpha()
if prev_is_word:
    continue
if offset + 1 < len(abbr):
    return True
```

**测试**（`tests/test_llm_sentence_divider.py::TestAbbreviationGuard`）：
- `_flush_sentences("Welcome.", force=False)` → `out == ["Welcome."]`（不延迟）— 当前是 `leftover == "Welcome."`
- `_flush_sentences("use e.", force=False)` → `leftover == "use e."`（延迟正确：等 `g.`）
- `_flush_sentences("e.", force=False)` → `leftover == "e."`（buffer 头也算 word 起点）

---

### T1.4 — VAD 代码默认 SPL 正值（config 是 dBFS 负值）

**现状**：
- `core/vad_silero.py:51` `db_threshold: float = 60.0`
- `core/vad_silero.py:228` `cfg.get("vad_db_threshold_during_tts_mac", 72.0)`
- `core/vad_silero.py:230` `cfg.get("vad_db_threshold_during_tts_rpi", 62.0)`
- 注：audio 段 record mode fallback 是 `60.0`（line 242）

**根因**：`367ffac` 单位纠正只改 `config.yaml`，未改代码默认。`_chunk_db` 用 `20*log10(rms+1e-10)`（RMS 无参考 = dBFS），正常语音 dBFS ≈ `-30 ~ -15`，永远过不了 `60.0` 的门槛 → 任何 config 缺 key 的部署 VAD 失灵。

**修复**：
- `SileroVADDirect.__init__` 默认 `db_threshold: float = -45.0`（对齐 audio 段）
- `build_vad` tts mode 默认 Mac `-22.0` / RPi `-32.0`（对齐 interrupt 段）
- record mode fallback `-45.0`

**测试**（`tests/test_vad_silero.py::TestProviderFactory`）：
- 新增 `test_defaults_match_dbfs_scale`：不传 dB config key，build_vad 出来的 `_db_threshold` 为负值（即 dBFS）
- 结合 T3.3 的生产 config 集成测

---

### T1.5 — text 前端绕过 normalizer

**现状**：
- `jarvis.py:599` 在 `handle_utterance`（语音路径）里调 `self.asr_normalizer.normalize(text)`
- `jarvis.py:1184 handle_text` 不调，`_process_turn`（shared）也不调

**根因**：normalizer 只挂在语音入口。MQTT/web/远程可能把经外部 ASR 转好的文本直灌 `handle_text`，绕过所有修正规则。Allen 的 corrections 都带 `require_context` 保护，所以对真实打字也安全。

**修复**：把 normalize 调用从 `handle_utterance` 移到 `_process_turn` 入口（覆盖两条路径）：
- 删 `jarvis.py:599` 的调用
- `_process_turn(text, session_id, output_fn, ...)` 入口第一行：`text = self.asr_normalizer.normalize(text)`（在 `_interrupt_played_texts` 重置之后、history 读取之前）

**副作用评估**：handle_text 有可能在 debug tool/system_tests 场景灌非常规文本；所有 corrections 都要 `require_context`（L1 结构强制）→ 只有带 action 语境的文本才会被改；安全。

**测试**（`tests/test_jarvis_text_path.py` 新文件 或 `tests/test_main.py` 扩展 —— test_main.py 已删，`_process_turn` 目前无直接单测，风险低）：
- Mock `conversation_store`, `asr_normalizer`，让 `_process_turn` 早 bail，断言 normalize 被调
- 同一断言用在 `handle_text` → `_process_turn` 链路
- 同一断言用在 `handle_utterance` → `_process_turn` 链路

---

### T1.6 — preprocessor NFKC 顺序

**现状**：`core/tts_preprocessor.py:40-49` 执行顺序：
1. asterisks
2. brackets（ASCII `[]`）
3. parentheses（ASCII `()` + 中文 `（）`）
4. angle_brackets（ASCII `<>`）
5. remove_special_characters（内含 NFKC）

**根因**：假如输入带 `⟨xxx⟩`（Mathematical LEFT ANGLE BRACKET，U+27E8），NFKC 归一化到 `〈xxx〉`，但 `filter_angle_brackets` 只匹配 `<>` → 不过滤。

**修复**：把 NFKC 提到 `clean()` 入口，5 个过滤器都在归一化后的文本上运行。`remove_special_characters` 不再做 NFKC（避免双重归一化，无害但冗余）：
```python
def clean(text, config=None):
    if not text:
        return text
    text = unicodedata.normalize("NFKC", text)  # lift to entry
    cfg = dict(config) if config else {}
    # ... 5 filters on normalized text ...
```

`remove_special_characters` 内部删 NFKC 调用（现在在入口做过）。

**测试**：
- `clean("⟨tag⟩正文")` → `"正文"`（NFKC 后是 `〈tag〉正文`，angle filter 需识别全宽 `〈〉` —— 注意：`〈〉` 不是 ASCII `<>`）
- **这揭示了一个子问题**：NFKC 不会把 `〈〉` 变成 `<>`，因为它们是语义不同字符。真正的修复需要 angle filter 匹配 `<>` **和** `〈〉`。
- 调整：`filter_angle_brackets` 用 `[("<", ">"), ("〈", "〉")]`
- 同理 brackets: `[("[", "]"), ("【", "】")]` —— 中文书名号式方括号也过滤

**测试补充**：
- `clean("【开心】正文")` → `"正文"`
- `clean("〈tag〉正文")` → `"正文"`

---

### T2.1 — `_ACTION_WORDS` 含单字"灯"

**现状**：`core/asr_normalizer.py:27-29`
```python
_ACTION_WORDS: tuple[str, ...] = (
    "开", "关", "调", "亮", "暗", "模式", "灯", "切换", "启动",
)
```

**根因**：plan §6.6 写的是"灯"作为动作词示例之一，但 "灯" 是一个高频名词字。"路灯真好看"、"我喜欢灯笼"都满足 action_word 门槛 → L3 fuzzy 启用后误触发。

**修复**：删除 "灯"，"暗" 也偏宽（"暗恋"），但先只动"灯"。新增 "打开"、"关闭" 提高动词覆盖。最终：
```python
_ACTION_WORDS: tuple[str, ...] = (
    "开", "关", "打开", "关闭", "调", "亮", "模式",
    "切换", "启动", "场景",
)
```

**测试**（`tests/test_asr_normalizer.py::TestLayer3Fuzzy`）：
- 新增 `test_enabled_does_not_fire_on_ambient_light_talk`：L3 开启，alias `{"客厅大灯": ["大灯"]}`，text `"我喜欢小灯泡"` → 不变
- 新增 `test_enabled_requires_strict_action_word`：text `"打开大蛋"` → 触发替换

---

### T2.2 — L3 长度错配

**现状**：`core/asr_normalizer.py:163-168`
```python
for cand, canonical in self._fuzzy_targets.items():
    if abs(len(cand) - len(window)) > self._fuzzy_max_distance:
        continue
    d = _levenshtein(window, cand)
    if d <= self._fuzzy_max_distance:
        return text[:i] + canonical + text[i + window_size:]
```

**根因**：窗口大小 2-5，若 alias 列表中有 "大灯"（2 字）和 canonical "客厅大灯"（4 字）：
- 窗口 `"大蛋"`（2 字）距离 `"大灯"` = 1，替换为 canonical `"客厅大灯"`（4 字）→ 文本长度 +2
- 连续文本膨胀：`"开大蛋"` → `"开客厅大灯"`（正确）但 `"开大蛋1大蛋2"` → `"开客厅大灯1大蛋2"`（部分替换）不是一致行为

**修复**：分清**输入侧 window**和**输出侧 canonical**两个长度概念：
- **输入侧约束**：window 长度必须等于 alias 长度（`len(cand)`），避免 2 字窗口去匹配 4 字 alias 导致错位
- **输出侧放宽**：canonical 长度可以 ≥ alias 长度（这是 fuzzy 的设计目的：用户说短了，补全长的，例如 alias `"大灯"` 2 字，canonical `"客厅大灯"` 4 字，仍允许）

```python
for cand, canonical in self._fuzzy_targets.items():
    # 输入侧：只匹配与 alias 同长的 window，避免错位
    if len(cand) != window_size:
        continue
    d = _levenshtein(window, cand)
    if d <= self._fuzzy_max_distance:
        # 输出侧：canonical 可以更长（alias→canonical 本来就是"短→长"的补全）
        return text[:i] + canonical + text[i + window_size:]
```

**测试**：
- `test_fuzzy_window_must_equal_alias_length`：window 3 字，只对 3 字 alias 候选跑 Levenshtein；遇到 5 字 alias → skip
- `test_fuzzy_canonical_can_expand_text`：window 2 字 `"大蛋"`，alias `"大灯"`（2 字），canonical `"客厅大灯"`（4 字）→ 输出 text 扩到 4 字，方向正确

---

### T2.3 — InterruptMonitor 无锁状态写

**现状**：
- `core/interrupt_monitor.py:117` `self._fired = False` — 无锁（start）
- `core/interrupt_monitor.py:119` `self._recording = True` — 无锁（start）
- `core/interrupt_monitor.py:134` `self._recording = False` — 无锁（stop）
- `core/interrupt_monitor.py:165` `with self._lock: self._fired = False` — reset 有锁

**根因**：mic 线程在 `feed_audio` 内读 `self._recording` 和 `self._fired`；start()/stop() 在主线程修改；无 happens-before 保证，理论上 CPython GIL 让 bool 读写原子，但 (a) 其他平台不保证，(b) 与 `_lock` 使用不一致，(c) 代码阅读者会误以为无同步。

**修复**：所有 `_recording` / `_fired` 读写都在 `self._lock` 下：
- `start()`：`with self._lock: self._fired=False; self._recording=True`
- `stop()`：`with self._lock: self._recording=False; ...`
- `feed_audio()` 入口：
  ```python
  with self._lock:
      if not self.enabled or not self._recording:
          return
      fired = self._fired
  # (subsequent heavy work outside the lock)
  ```
  —— `self.enabled` 在 init 后不变，可省；但 `_recording` 变。
- `_check_partial` 已经在 `with self._lock:` 下访问 `_fired`，无需改

**测试**：
- 用 `threading.Barrier` 同步多线程 start/stop/feed_audio，断言无异常 + 最终状态一致
- 或更务实：加一个 `test_stop_prevents_further_feed_audio` —— start → feed → stop → feed（应 noop）→ 断言 callback 只触发一次

---

### T2.4 — Timer vs stop() 争锁

**现状**：`core/interrupt_monitor.py:132-161` `stop()` 先 `self._cancel_soft_timer()`（含锁），再进第二个 `with self._lock` 读状态+设 NORMAL，再锁外 call `on_soft_resume`。

**根因**：timer 已 fire 时 `cancel()` 无效；`_on_soft_timeout` 持锁检查 `_soft_state == "DUCKED"` 可能通过，然后 call `on_soft_resume`。stop() 自己也可能 call `on_soft_resume`。两次都 call → `resume_playback` 幂等所以无崩溃，但 log 里有重复 warning（或多打一行 debug）。

**修复**：合并两个 `with self._lock` 为一个，cancel 和状态迁移同一个 critical section 里：
```python
def stop(self) -> np.ndarray | None:
    with self._lock:
        self._recording = False
        self._cancel_soft_timer_locked()
        should_resume = (
            self._soft_stop_enabled
            and self._soft_state == "DUCKED"
            and self._on_soft_resume is not None
        )
        self._soft_state = "NORMAL"
    if should_resume and self._on_soft_resume is not None:
        try:
            self._on_soft_resume()
        except Exception as exc:
            LOGGER.warning("on_soft_resume on stop() failed: %s", exc)
    # ... ASR cleanup ...
```

**测试**（**首选同步化避免 flaky**）：
- **方法 A（首选）**：`patch("threading.Timer")` 让 timer 变同步（`Timer(interval, func)` 返回 MagicMock，手工 `timer_instance.function()` 触发）。场景：
  1. 构造 monitor，start()，mock VAD 抛 `_update_soft_state(is_speech=True)` → 断言 `_start_soft_timer_locked` 中 `threading.Timer(...)` 被 call 一次
  2. 调 `stop()` → 断言 mock timer 的 `cancel()` 被 call，`_soft_state == "NORMAL"`
  3. 手工触发 timer callback（`_on_soft_timeout`）→ 断言状态已是 NORMAL（race-losing path），`on_soft_resume` 最多调一次
- **方法 B（补充）**：用 `threading.Event` 作同步点，`on_soft_resume` 内 `event.set()`；主线程 `event.wait(timeout=1.0)` + 断言 call count ≤ 1
- 放弃：纯 `time.sleep + 检查 count`（CI 负载高时 flaky）

---

### T2.5 — `_collapse_whitespace` 吞换行

**现状**：`core/tts_preprocessor.py:61-62`
```python
def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
```

**根因**：输入 `"第一行\n第二行"` → `"第一行 第二行"`，TTS 朗读没有自然停顿。

**评估**：多数 TTS 引擎对换行本就无特殊处理；OLV 也是这么合并的；MiniMax/edge-tts 把换行当空格。**决定：不修**，仅在文档里注明这是预期行为。

**文档行动**：在设计 doc 和代码 docstring 里加一句说明。**不单独 commit**，归并到 WP3 commit 的 docstring 更新。

---

### T3.1 — WP3 测试补齐

**新增**（`tests/test_tts_preprocessor.py`）：
- `test_keeps_currency_symbols`：`¥$€£￥` 都保留
- `test_keeps_digits_and_latin`：`"ABC 123 abc"` 不变
- `test_keeps_quotes`：`"「引号」'x'"` 的引号保留
- `test_drops_math_symbols`：`"1+2"` 里 `+` 被删（保持现状——`Sm` 不在白名单）

---

### T3.2 — WP4 测试补齐

**新增**（`tests/test_llm_sentence_divider.py`）：
- `test_all_13_abbreviations_protected`：参数化 pytest，所有 13 个缩写都不被切（`Mrs.`, `Prof.`, `e.g.`, `i.e.`, `Mr.`, `Ms.`, `Dr.`, `Jr.`, `Sr.`, `St.`, `Rd.`, `Inc.`, `Ltd.`, `vs.`）
- `test_possible_abbreviation_prefix_word_boundary`：直接测 `_possible_abbreviation_prefix("Welcome.", 7)` 返回 False
- `test_possible_abbreviation_prefix_at_buffer_start`：`_possible_abbreviation_prefix("e.", 1)` 返回 True
- `test_possible_abbreviation_prefix_after_space`：`_possible_abbreviation_prefix("use e.", 5)` 返回 True
- `test_generate_response_stream_resets_first_sentence`：mock 底层 stream，第一次 stream + 第二次 stream，断言每次入口前 `_is_first_sentence = True`

---

### T3.3 — WP6 真 config + 真模型集成测

**新增**（`tests/test_vad_silero.py::TestProductionDefaults` 或独立文件）：
- 加载 `config.yaml`（`yaml.safe_load`），取 `audio:` 段
- 用真 ONNX 模型 `data/silero_vad.onnx` 跑（仅此类测试走真模型）
- 三个用例：
  1. `test_silence_does_not_trigger_with_real_config`：生成 1 秒静音（zeros） → `is_speech_detected()==False, empty()==True`
  2. `test_synthetic_speech_triggers_with_real_config`：生成 1 秒 300Hz 正弦 + 白噪声 amplitude=0.3 → 最终 ACTIVE
  3. `test_tts_mode_thresholds_applied`：interrupt 段 + mode="tts" → `_prob_threshold == 0.5`、`_db_threshold == -22.0(Mac)` 或 `-32.0(Linux)`
- 这些测试在 CI 没有 ONNX 时 skip（`pytest.skip("data/silero_vad.onnx not present")`）

---

### T3.4 — WP1 buffer batching 断言

**新增**（`tests/test_interrupt_monitor.py::TestBufferBatching`）：
- 配置 `streaming_asr_chunk_samples=3200` + mock recognizer
- 喂一个 1000 samples 的 chunk → 断言 `recognizer.accept_waveform` 未被 call（累积）
- 再喂 2500 samples → 累计 3500 > 3200 → 断言 accept_waveform 被 call 一次，参数长度 3500
- 喂完后 buffer 应为空

---

### T3.5 — WP2 性能 <10ms 断言

**新增**（`tests/test_asr_normalizer.py::TestPerformance`）：
- 构造真实量级的 config（20 corrections + 30 aliases + L3 关闭）
- `time.perf_counter()` 前后跑 `normalize("开客厅大灯好吗")` 1000 次
- 断言总耗时 < 5000ms（即单次 < 5ms，留 headroom，生产 10ms 上限）
- 单独再跑一次 L3 enabled 的场景，断言 < 50ms/call（plan §6.10 预期 L3 慢，这里只保证不崩溃）

---

### T3.6 — 交付报告 + 手测 checklist 更新

**改动**：`notes/plans/voice-pipeline-optimization-2026-04-16-report.md`

- §1 表格添加 "8 followup" 行，记录本轮 commits（hash 占位符）
- §2 pytest 数字更新（预期 +30 tests 左右）
- §4.1 "计划与代码现状差异"扩充：
  - 新增"dBFS 单位在代码默认值也对齐"
  - 新增"normalize 调用点从 handle_utterance 提到 _process_turn"
  - 新增"preprocessor NFKC 顺序调整"
- §4.3 冷启动数据：Allen 依然自己补，不在本轮范围
- §5 手测 checklist 新增条目：
  - [ ] **T1.1 MiniMax vol**：让 LLM 回复长文 → 无 422 错误，音量正常
  - [ ] **T1.2 Currency**：让 LLM 回复 `¥100` → TTS 念"一百元"或"人民币一百"（引擎决定），不漏 `¥`
  - [ ] **T1.3 Welcome**：让 LLM 回复以 `Welcome.` 开头 → 首句 TTS 不比之前慢
  - [ ] **T1.4 VAD fallback**：临时删 config 里 `vad_db_threshold` 行 → 启动不报错，VAD 仍能用
  - [ ] **T1.5 text 前端**：`python jarvis.py` 后用 web 输入"开客厅大蛋" → normalizer 修正
  - [ ] **T1.6 全角方括号**：让 LLM 回复 `【开心】正文` → TTS 只念"正文"
- §7 未覆盖 / 未来议题扩充：
  - soft_stop 默认开启时机（保持 false，Allen 手测 SIGSTOP+afplay 无 pop 后改 true）
  - L3 fuzzy 实战调参 still pending（`_ACTION_WORDS` 已收紧）

---

## 3. Commit 计划

**顺序优化：Tier1-heavy 的 WP 先做，测试随代码同 commit**

| # | Commit | 范围 | 文件 |
|---|--------|------|------|
| 0 | `docs(plan): voice-pipeline debug 设计文档` | 本设计文档落盘 | `notes/plans/voice-pipeline-debug-2026-04-16-design.md` |
| 1 | `fix(tts): WP3 preprocessor + MiniMax vol int` | T1.1 + T1.2 + T1.6 + T2.5 注释 + T3.1 | `core/tts.py` `core/tts_preprocessor.py` `tests/test_tts_preprocessor.py` `tests/test_tts_cache.py` |
| 2 | `fix(llm): WP4 abbreviation word-boundary + 测试补齐` | T1.3 + T3.2 | `core/llm.py` `tests/test_llm_sentence_divider.py` |
| 3 | `fix(vad): WP6 代码默认 dBFS + 生产 config 集成测` | T1.4 + T3.3 | `core/vad_silero.py` `tests/test_vad_silero.py` |
| 4 | `fix(asr): WP2 text 路径 + _ACTION_WORDS + L3 length + perf test` | T1.5 + T2.1 + T2.2 + T3.5 | `jarvis.py` `core/asr_normalizer.py` `tests/test_asr_normalizer.py` |
| 5 | `fix(interrupt): WP7 thread-safety + stop/timer 锁合并` | T2.3 + T2.4 | `core/interrupt_monitor.py` `tests/test_interrupt_monitor.py` `tests/test_interrupt_soft_stop.py` |
| 6 | `test(interrupt): WP1 buffer batching 断言` | T3.4 | `tests/test_interrupt_monitor.py` |
| 7 | `docs(report): 更新手测 checklist + 本轮修复清单` | T3.6 | `notes/plans/voice-pipeline-optimization-2026-04-16-report.md` |

**每 commit 后**：`python -m pytest tests/test_<relevant>.py -q` 全绿。
**全部完成后**：`python -m pytest tests/ -q` 预期 993+ passed（963 + ~30 新）。

---

## 4. 风险 / Trade-offs

| 风险 | 影响 | 缓解 |
|------|------|------|
| MiniMax API 对 `vol=1.0` 向来接受 | T1.1 修改可能无 functional 差别 | 纯对齐文档，无副作用 |
| `normalize()` 从语音路径移到 shared _process_turn | text 前端打字也 normalize，corrections 的 context guard 保证不误伤 | Test 覆盖两条路径 |
| `_possible_abbreviation_prefix` word-boundary guard | 新 guard 对 `e.g._test` 类代码变量名会提前切 | 此场景极罕见，接受 |
| VAD 默认改负值 | 旧的测试如果硬编码正值 threshold 会失败 | **已确认兼容**：`grep -n db_threshold tests/test_vad_silero.py` 查得（line 46/151/159/212 用 -200/-30/-100/-200；line 181/182 在 `TestProviderFactory` 里传 80.0/65.0 但都是**显式 cfg 覆盖 default**，assertions 测的是 cfg 值的回传 → 与 default 改动无关）。唯一建议顺手调整：把 181/182 的 80.0/65.0 改成 -22.0/-32.0 以免阅读者混淆（列入 commit 3 顺手项） |
| `_ACTION_WORDS` 删"灯" | L3 对 "灯" 相关设备名的 fuzzy 容忍度下降 | L3 默认 off，真正用 L3 时 Allen 可按需加回 |
| InterruptMonitor 锁范围扩大 | 微小性能影响（每 chunk 多一次 lock acquire） | 可忽略（< 1μs） |
| NFKC 提到入口 | 之前 `<>` filter 看到原始字符，现在看到 NFKC 后；多数情况无差 | 补 `〈〉` `【】` 到对应 filter |

---

## 5. Out of Scope（本轮不做）

- 切 `soft_stop_enabled=true` 默认（Allen 手测 SIGSTOP+afplay 无 click/pop 后再改）
- L3 fuzzy 在生产中的实战调参（等 Allen 跑一轮行为 log）
- bench 脚本完全自动化（现在依赖人工说"停"）
- MiniMax QPS 并发（WP3 决策已拒）
- 引入 pysbd（plan §5.5 降级决策已定）
- 新增任何 pip 依赖
- 动 `data/` 下模型文件
- 动 `core/permission_manager.py`
- 动 OLED / Electron / MQTT 相关（本次不 touch）

---

## 6. 验证流程（Final）

```bash
# 1. 配置自检
python -c "import yaml; yaml.safe_load(open('config.yaml'))"

# 2. 启动冒烟
python jarvis.py --no-wake  # 启动不报错即可

# 3. 单元测试全量
python -m pytest tests/ -q 2>&1 | tail -30

# 4. 新增测试定向跑
python -m pytest tests/test_tts_preprocessor.py tests/test_llm_sentence_divider.py \
    tests/test_vad_silero.py tests/test_asr_normalizer.py \
    tests/test_interrupt_monitor.py tests/test_interrupt_soft_stop.py \
    tests/test_interrupt_memory_injection.py -v

# 5. Bench（必跑一次，记录 speech_to_detect_ms 中位数，验证 VAD dB 阈值真修好）
python scripts/bench_interrupt_latency.py --runs 10 --label after-debug
# → 期望 speech_to_detect_ms 中位数 < 400ms（修前 P0-A VAD 失灵时会"说三遍才触发"）
# → 结果写入 scripts/bench_results/interrupt_latency.jsonl，commit 7 的 report 引用

# 6. Allen 手测 checklist（见 §3.6 扩充后的 report）
```

---

## 6.5 Rollback / Failure 处理

**单 commit 失败**（测试红、或 pytest 崩溃）：
1. 不继续下一个 commit
2. `git reset --hard HEAD~1` 回退到前一个已绿的状态（本轮都在 main 直改，无 feature branch）
3. 重新分析根因，修改设计再做；如果根因是设计文档错了，同步 edit 本 design 文档后重 commit

**多 commit 完成后发现回归**（某 WP 之前绿，全部改完后挂）：
1. `git bisect` 定位引入 commit
2. 针对性修复 + 新增覆盖这个回归的测试（防止再犯）
3. 本设计 §1 inventory 追加一条事后记录

**Allen 手测发现问题**（pytest 绿但实际行为错）：
1. 建新 task，不直接回退（因为 pytest 已绿意味着是测试覆盖不足而非代码错误）
2. 先补能重现 bug 的测试（红）
3. 再修代码让测试绿
4. 新增条目追加到 `voice-pipeline-optimization-2026-04-16-report.md` 的"已知遗留问题"

**不可回滚**：如果 config.yaml 不小心 commit 了 secret 或 data/ 下模型文件被误动，第一时间告知 Allen（CLAUDE.md 规则红线）。

---

## 7. 交付物

- 8 个 commit（0 设计文档 + 1-7 实施），全部 `main` 直改，不 push
- commit messages 无 `Co-Authored-By`
- 本设计文档（`notes/plans/voice-pipeline-debug-2026-04-16-design.md`）= commit 0
- 交付报告更新（commit 7）

---

**END OF DESIGN**
