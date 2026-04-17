# Voice Pipeline Optimization — 交付报告

**执行日期**：2026-04-16
**执行 agent**：Claude Opus 4.7
**基于计划**：`notes/plans/voice-pipeline-optimization-2026-04-16.md`
**Branch**：`main`（直改，未 push）
**起点 commit**：`341a746` (feat(desktop): Electron shell + Pet Mode wrapping ui/web/)
**终点 commit**：`e9d1eaf` (WP6 followup: test_audio_recorder_vad pin sherpa_onnx provider)

---

## 1. WP 完成状态

| WP | 状态 | Commit | 主要改动文件 | 备注 |
|----|------|--------|------------|------|
| WP1 | ✅ | `72cd699` | `core/interrupt_monitor.py` `config.yaml` `scripts/bench_interrupt_latency.py` `tests/test_interrupt_monitor.py` | chunk 8000→3200，config 驱动；bench 脚本基础框架 |
| WP3 | ✅ | `03a3340` | `core/tts.py` `core/tts_preprocessor.py` `config.yaml` `tests/test_tts_preprocessor.py` `tests/test_tts_cache.py` | 5 toggle 预处理，全角中文括号；MiniMax volume 5→1.0 |
| WP4 | ✅ | `da43c8b` | `core/llm.py` `config.yaml` `tests/test_llm_sentence_divider.py` | 缩写白名单 + faster_first_response；**pysbd 未引入**（abbreviation 白名单足够） |
| WP2 | ✅ | `b1606cd` | `core/asr_normalizer.py` `jarvis.py` `config.yaml` `tests/test_asr_normalizer.py` | 三层级联，Layer 3 默认关闭；冷启动 2 条占位示例 |
| WP6 | ✅ | `0718c21` + `e9d1eaf` | `core/vad_silero.py` `core/audio_recorder.py` `core/interrupt_monitor.py` `config.yaml` `tests/test_vad_silero.py` `tests/test_audio_recorder_vad.py` | onnxruntime 直调（旧版 ONNX 签名 h/c 分开 hidden=64），保留 sherpa_onnx fallback |
| WP7 | ✅ | `1b79655` | `core/tts.py` `core/interrupt_monitor.py` `jarvis.py` `config.yaml` `tests/test_interrupt_soft_stop.py` `tests/test_tts_suspend.py` | 选项 C SIGSTOP/SIGCONT；**`soft_stop_enabled: false` 默认**（Allen 验证后开启） |
| WP5 | ✅ | `ce29b88` | `core/tts.py` `jarvis.py` `tests/test_interrupt_memory_injection.py` | 方案 b：方法 b 已播完句拼接；`abort()` 签名保留，`played_texts` 属性新增 |

---

## 2. Pytest 汇总

**全量** `python -m pytest tests/ -q`：
- **963 passed**
- **10 failed**（均为预先存在，与本计划改动无关）

### 失败明细（pre-existing）

| Test | 原因 |
|------|------|
| `test_hue_integration.py::test_hue_bridge_connects_and_reads_resources` | 需要真实 Hue Bridge 网络 |
| `test_memory_edge_cases.py::TestProfileRebuild::test_task_with_expires_goes_to_pending` | 历史失败 |
| `test_memory_manager.py::TestExtractFallback::test_fc_success_does_not_fallback` | mock URL 解析失败（DNS） |
| `test_memory_store.py::TestEpisodes::test_episodes_user_isolated` | 模型/数据文件依赖 |
| `test_memory_store.py::TestEpisodes::test_episode_dedup_skips_similar` | 同上 |
| `test_memory_store.py::TestEpisodes::test_episode_dedup_allows_different` | 同上 |
| `test_speaker_encoder.py::test_encode_returns_embedding_and_loads_model_lazily` | 模型文件依赖 |
| `test_speaker_encoder.py::test_encode_file_reads_wav_and_resamples` | 同上 |
| `test_wake_word.py::test_start_and_stop` | `ModuleNotFoundError`（pyaudio/openwakeword） |
| `test_wake_word.py::test_process_frame_no_detection` | 同上 |

**WP 引入的新测试（全部通过）**：
- `test_tts_preprocessor.py`: 16 tests
- `test_llm_sentence_divider.py`: 14 tests
- `test_asr_normalizer.py`: 17 tests
- `test_vad_silero.py`: 15 tests（含真实 ONNX 模型加载）
- `test_interrupt_soft_stop.py`: 8 tests
- `test_tts_suspend.py`: 8 tests
- `test_interrupt_memory_injection.py`: 10 tests

---

## 3. Benchmark 结果

`scripts/bench_interrupt_latency.py` 已就绪（半自动，需 Allen 在播放中说"停"）。

```
baseline:     <Allen 补>
after WP1:    <Allen 补，预期 ~400ms>
after WP6:    <Allen 补，预期 ~250-350ms>
after WP7:    <Allen 补，预期 ~150-250ms>
```

跑法：
```bash
python scripts/bench_interrupt_latency.py --runs 10 --label after-WP7
```
中位数会写到 `scripts/bench_results/interrupt_latency.jsonl`。

---

## 4. 已知遗留问题 / 决策偏离

### 4.1 计划与代码现状的差异（已修正）

1. **plan §8.5 写 `self._current_proc`，实际是 `self._play_proc`** — 实施时按真实属性名走
2. **plan §7.4 写 silero ONNX 签名是 `state[2,1,128]+sr`，实际 `h+c[2,1,64]`，无 sr** — 实施时按真实签名（旧版 silero_vad.onnx）
3. **plan §9.4 `abort()` 改返回 `(unplayed, played)` tuple** — 改用 `played_texts` property，保持 `abort()` 签名 list[str] 不变（避免破坏 8 个 test_tts_stop 用例和 jarvis 多处调用）

### 4.2 主动降级 / 谨慎默认

1. **WP4 pysbd 未引入** — plan 允许"如果集成复杂可降级"，本次直接走 abbreviation 白名单，避免给流式分句路径加 segmenter 调用成本
2. **WP7 `soft_stop_enabled: false` 默认** — plan 写 true，但 SIGSTOP+macOS afplay 的 audio buffer 行为未充分验证（CoreAudio driver 层 buffer 残余 100-300ms 风险），保守默认 off，Allen 手测无副作用后改 true
3. **WP6 vad_provider 默认 `silero_direct`** — 按 plan，sherpa_onnx 保留为 fallback

### 4.3 冷启动数据待 Allen 补充

- `config.yaml` `asr_corrections`: 留 2 条占位（`客厅大蛋→客厅大灯`、`放送模式→放松模式`）
- `config.yaml` `asr_aliases`: 留 2 条占位（`客厅大灯` / `放松模式`）

Allen 翻 `memory/behavior_log.py` 找近两周 `intent=device/scene` 失败记录补充。

### 4.4 未做项

1. **OLV Top 10 #7（MiniMax stream=True）** — plan 已定不做（OLV 非真流式，收益 0）
2. **决策 C（TTS 并发合成 sequence_counter）** — plan 已定不做
3. **Bench 脚本完全自动化** — 当前需人工说"停"，自动化需要预录 trigger 音频或合成路径

---

## 5. Allen 手测 checklist

按 plan §11 模板，每条都给具体验证方法：

- [ ] **WP1: 打断延迟改善** — `python scripts/bench_interrupt_latency.py --runs 10 --label after-WP1`，期望中位数 ~400ms（之前 ~700ms）
- [ ] **WP2: 误识别修正** —
  - 说"开客厅大蛋" → 应转为"开客厅大灯"再执行
  - 说"我想吃大蛋糕" → 不应被改（context guard）
- [ ] **WP3: TTS 不读特殊字符** —
  - 让 LLM 回复包含 `好的 😊 [开心] *强调* <break/> (旁白) 实际内容` → TTS 只念"实际内容"（朗读时听一下）
  - MiniMax 播放 → 不应有爆音（之前 vol=5 偶发）
- [ ] **WP4: 缩写不误切 + 首句更快** —
  - 让 LLM 说一段含 "Dr. Smith said hello." 的中英混 → Dr. 处不停顿断句
  - 触发一句长回复 → 首句出 TTS 应明显比之前快（plan 期望 -50%~70%）
- [ ] **WP6: VAD provider 切换** —
  - `vad_provider: silero_direct`（默认）能启动、录音、ASR 结束正常
  - 切回 `vad_provider: sherpa_onnx` → 行为应与替换前一致
  - 跑 `uv pip list | grep -i silero` → 应无新依赖
- [ ] **WP7: 软停（先开 `soft_stop_enabled: true`）** —
  - TTS 播放时说"嗯嗯"3 秒不说关键词 → TTS 应先暂停后自动恢复
  - TTS 播放时说"停" → TTS 应在 ~200ms 内暂停并取消
  - macOS afplay 暂停后恢复音质有无 click/pop（重点观察）
- [ ] **WP5: history 注入** —
  - 触发一次"长回复 → 打断"，等下一轮对话
  - dump sqlite：`sqlite3 data/memory/jarvis_memory.db "SELECT content FROM messages WHERE session_id='...' ORDER BY id DESC LIMIT 4"` → 看 assistant 内容是否截断到已播完句 + 后面有 `[Interrupted by user]` 标记

---

## 6. 配置变更清单

### 新增字段

**`audio:` 段**：
- `vad_provider: silero_direct`
- `vad_prob_threshold: 0.4`
- `vad_db_threshold: 60.0`
- `vad_smoothing_window: 5`
- `vad_required_hits: 3`
- `vad_required_misses: 24`

**`llm:` 段**：
- `sentence_divider:` 子段
  - `faster_first_response: true`
  - `abbreviation_protect: true`

**`tts:` 段**：
- `minimax_volume: 1.0`
- `tts_preprocessor:` 子段（5 个 toggle，默认全 true）

**`interrupt:` 段**：

> **注**：下列 `vad_db_threshold_during_tts_*` 初始为 SPL 正值，后经 `367ffac` 修正为 dBFS 负值，又在 2026-04-16 debug round (§9) 代码默认也对齐 dBFS。config.yaml 现行值是 dBFS（见 line 624-625）；本表记录 WP7 初始交付时的值。

- `streaming_asr_chunk_samples: 3200`
- `vad_provider: silero_direct`
- `vad_prob_threshold_during_tts: 0.5`
- `vad_db_threshold_during_tts_mac: 72.0`
- `vad_db_threshold_during_tts_rpi: 62.0`
- `vad_smoothing_window: 5`
- `vad_required_hits: 3`
- `vad_required_misses: 24`
- `soft_stop_enabled: false`（**手测后改 true**）
- `soft_stop_timeout_ms: 3000`
- `soft_stop_method: suspend`

**新增顶层段**：
- `asr_corrections:` (2 条占位)
- `asr_aliases:` (2 条占位)
- `asr_normalizer_fuzzy:` (`enabled: false`, `max_distance: 2`)

### 修改字段

无（旧字段全部保留为 fallback 或继续生效）

---

## 7. 未覆盖 / 未来议题

1. **WP7 选项 A/B（真 ducking 不暂停）** — plan 已记录方案 A (ffplay 切换重启) 和 B (预合成双音量副本)，本次走选项 C；用户体验差异（"静音等待" vs "小声继续"）若手测不接受，可升级到 A/B
2. **Bench 自动化** — 当前依赖人工说"停"。未来可预录 trigger 音频从虚拟麦克风注入
3. **Layer 3 fuzzy 实战调参** — Allen 在生产中按需开 `asr_normalizer_fuzzy.enabled=true`，调 `max_distance`，可能需要补充 action_words 列表
4. **MQTT/远程频道的 history 注入** — `_truncate_assistant_for_interrupt` 只挂在 cloud_path（streaming LLM）；非 streaming 路径（直接 response_text）若有打断也想注入 marker 需类似处理

---

## 8. 文件清单总览

```
新增（10 个）：
  core/asr_normalizer.py
  core/tts_preprocessor.py
  core/vad_silero.py
  scripts/bench_interrupt_latency.py
  tests/test_asr_normalizer.py
  tests/test_interrupt_memory_injection.py
  tests/test_interrupt_soft_stop.py
  tests/test_llm_sentence_divider.py
  tests/test_tts_preprocessor.py
  tests/test_tts_suspend.py
  tests/test_vad_silero.py
修改（10 个）：
  config.yaml
  core/audio_recorder.py
  core/interrupt_monitor.py
  core/llm.py
  core/tts.py
  jarvis.py
  tests/test_audio_recorder_vad.py
  tests/test_interrupt_monitor.py
  tests/test_tts_cache.py
```

---

## 9. 2026-04-16 Debug Fixup 记录（Post-Delivery）

交付后 Allen + 执行 agent 二轮审计发现 17 项遗留项（6 Tier1 + 4 Tier2 改代码 + 1 只改 docstring + 5 测试缺口 + 1 文档更新 — 详见 `voice-pipeline-debug-2026-04-16-design.md` §1）。后续 8 个 fixup commits 全部落地：

| # | 主 Commit | Polish / 关联 | 范围 |
|---|----------|---------------|------|
| 0 | `e5ff894` | 计划 `8df1f44` | 设计 + 实施 plan 文档落盘 |
| 1 | `a826368` | polish `508a894` | WP3 preprocessor + MiniMax vol int (T1.1/1.2/1.6 + T2.5 docstring + T3.1) |
| 2 | `36ac4cc` | — | WP4 abbreviation word-boundary + 13 abbrev param + stream reset (T1.3/T3.2) |
| 3 | `5ade632` | polish `0edc801` | WP6 VAD dBFS 代码默认 + 生产 config 真模型 sanity (T1.4/T3.3) |
| 4 | `b70fceb` | polish `235b193` + 回归 `524c6e8` | WP2 text 路径 + _ACTION_WORDS + L3 长度守卫 + perf (T1.5/T2.1/T2.2/T3.5) |
| 5 | `48baeea` | polish `2fa5186` | WP7 thread-safety + stop/timer 合并 (T2.3/T2.4) |
| 6 | `b69b166` | polish `1e94ca2` | WP1 buffer 批量断言 + bench 脚本修复 (T3.4 + B1/B2/B3) |
| 7 | _this commit_ | — | 本文档更新 (T3.6) |

**Pytest 最终**：1021+ passed / 10 failed（10 failed 全部 pre-existing 环境依赖；跨本轮 8 个 commit 无新增 failure；新增 ~35 个 test cases 覆盖所有修复点）。

### 9.1 决策偏离本轮 design 的明细

1. **Task 4 — `_ACTION_WORDS` 额外删除 "亮"**（设计 doc §2 T2.1 保留，implementer 自行删除，Allen 最终审查时确认接受）
   - 理由：`_ACTION_WORDS` 作为 L3 fuzzy 的 action-word gate，"亮" 出现在 "漂亮/明亮/月亮" 中会让正常 talk 通过 gate 进入 fuzzy 扫窗
   - 副作用：L3 fuzzy 启用时，"亮度/亮一点" 类单字 "亮" 触发被弱化；但用户真实发指令更多用 "调亮" / "开亮一点" / "灯调亮"，都含已保留的 "调"/"开"
   - 若未来 Allen 发现"亮" 作为 action 不可或缺，直接在 `core/asr_normalizer.py:_ACTION_WORDS` 加回即可

2. **Task 4 — `_apply_fuzzy` 添加 `_action_word_positions()` 辅助**（设计 doc §2 T2.2 未要求，implementer 为了让测试 `test_window_must_equal_alias_length` 在 max_distance=2 下过而添加）
   - 理由：仅 `len(cand) != window_size` 守卫不够 —— "打开大蛋灯" 的 window "打开" 能距离 "大灯" 2 以内被 fuzzy 匹配，吞掉 action word
   - 该扩展 reviewer 验证"逻辑必需"且无新回归

3. **Task 6 scope 扩展 bench 脚本修复**（设计 doc §2 T3.4 原本仅 test-only；Allen 在 Task 6 dispatch 前明确批准 fold bench fixes 进同 commit）
   - B1 critical: `scripts/bench_interrupt_latency.py` 的 `start_mic_listener()` 从"3 秒倒计时前"改到"倒计时后 submit 前"；之前的数据 `speech_to_detect_ms` 因环境噪声虚增不可信
   - B2 minor: chunk_samples fallback default 8000 → 3200
   - B3 doc: module docstring 澄清 "VAD-confirmed-start → detect"，说明 ~96ms 偏移

4. **Task 4 → Task 5 的测试回归**（spec review 在 Task 4 时未发现；Task 5 实施过程中首次报告；用 `524c6e8` 修复）
   - Task 4 在 `_process_turn` 入口加了 `self.asr_normalizer.normalize(text)` 调用
   - `tests/test_interrupt_memory_injection.py::jarvis_stub` fixture 没跟着补 asr_normalizer mock → AttributeError
   - 修复：fixture 加 `j.asr_normalizer.normalize = MagicMock(side_effect=lambda t: t)` pass-through

### 9.2 Allen 手测 checklist 扩充（接续 §5）

- [ ] **T1.1 MiniMax vol**：触发一段长 LLM 回复 → TTS 不返回 422 / type error；音量听起来正常（不爆也不过小）
- [ ] **T1.2 Currency**：LLM 说出 `¥100` / `$5` / `€3` → TTS 读出来时货币符号不被吞（每家 TTS 引擎对 ¥ 的朗读可能不同：MiniMax 多念"元"，edge-tts 多念"人民币"；关键是符号到达 TTS 之前没被 preprocessor 丢掉）
- [ ] **T1.3 Welcome. 首句延迟**：让 LLM 回复以 `Welcome.` 开头 → 首句 TTS 触发时间肉眼感知不慢（对照 plan §5.4 的 faster_first_response 期望）
- [ ] **T1.4 VAD fallback**：临时把 `config.yaml` 的 `audio.vad_db_threshold` 一行删掉并启动 jarvis.py → 无报错；说话能正常被 VAD 触发（证明代码默认值也是 dBFS 合理值）— 完事别忘了改回来
- [ ] **T1.5 text 前端修正**：`python jarvis.py` 后用 web/text 前端发 `开客厅大蛋` → 系统应识别为 `开客厅大灯` 意图并执行
- [ ] **T1.6 全角方括号/花括号**：LLM 回复 `【开心】正文` / `〈tag〉正文` → TTS 只念"正文"
- [ ] **T2.1 灯泡不误触发**：L3 fuzzy 打开（`asr_normalizer_fuzzy.enabled=true`）后说 `我喜欢小灯泡` → 不被改成其他设备名
- [ ] **T2.3 thread safety**：连续打断 3 次（TTS 说话 → 说 "停" → 再开始新对话 → 再打断...）→ 无日志里有重复的 `on_soft_resume` 警告
- [ ] **Bench 必跑**：`python scripts/bench_interrupt_latency.py --runs 10 --label after-debug` → `speech_to_detect_ms` 中位数 < 400ms，结果写进 `scripts/bench_results/interrupt_latency.jsonl`
- [ ] **soft_stop 手测后切换默认**（不在本轮 scope，但 Allen 可在这个 pass 里顺手验）：`interrupt.soft_stop_enabled=true` + 麦克风说 "嗯嗯" 不说关键词 → TTS 暂停 3s 后自动恢复，无 audio click/pop。**通过后在 config.yaml 里把默认改成 true（单独 commit）**

### 9.3 已知遗留 / 后续

- **L3 fuzzy 默认仍 `enabled=false`**；Allen 实战观察后按需开启（本轮 T2.1/T2.2 把 guard 收紧到不触发即无害）
- **`bench_interrupt_latency.py` 仍需人工说 "停"**；未来可预录 trigger 音频从虚拟 mic 注入实现全自动
- **MQTT/远程频道非 streaming 路径若有打断**，想注入 `[Interrupted by user]` marker 需扩 `_truncate_assistant_for_interrupt` 触发点（本轮未做）
- **soft_stop_enabled 默认开启时机** 仍 pending Allen 手测 SIGSTOP+afplay 无 click/pop 后打开
- **plan doc (`voice-pipeline-debug-2026-04-16-plan.md`) 两处 minor 笔误**：Task 2 的 `generate_response_stream` 应为 `chat_stream`（implementer 自适应，测试已生效）；`isalpha()` rationale 写"Chinese chars return False"实际 Python3 是 True（结果正确但推理过程错）— 如 plan 再用可更新

---

**END OF REPORT**
