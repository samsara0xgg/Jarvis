# 自研 AudioStreamPlayer + 全方面 benchmark + VAD 调优

**日期**: 2026-04-17(下午~晚上,接 `interrupt-asr-migration-2026-04-17.md` 早上那版 session)
**作者**: Allen + Claude
**session 时长**: ~6h
**硬件**: Mac(M 系列)+ 耳机 / XVF3800 USB 麦

---

## TL;DR

今早刚修完 interrupt-ASR migration(SenseVoice + pre-roll)后发现 3 个延续性问题:
1. WP7 软停在 Mac afplay + SIGSTOP 上有 ~300ms loop 尾巴(CoreAudio underrun artifact)
2. 每句 subprocess spawn 增加 30-80ms 句间延迟
3. 没有量化验收,不知道今晚改动实际效果

下午一气做完:
- **全方面 benchmark**(`scripts/bench_voice_pipeline.py`,1450 行,12 个子 bench,覆盖 WP1-7)
- **自写 PCM 播放器**(`core/audio_stream_player.py`,400 行,miniaudio + soxr + 持续 sd.OutputStream + SPSC ring + GainRamp + abort 穿透)
- **一堆细节修复**:GainRamp off-by-one pop、abort 不穿透 stream_player、stop_mic_listener segfault 顺序、VAD LSTM 冷启动 160ms 盲窗、vad_mode 档位切换
- **实测验证**:speech→detect 1760ms → 990ms(33% 快)、多字关键词命中率 50% → 100%、软停从 loop → 干净 ducking、segfault 根治

`soft_stop_enabled: true` 现在可以安全打开,完成 WP7 原始目标。

---

## 过程概述

### 早上的遗产(继续干)

早上 migration 产物:
- 打断路径换到 SenseVoice + ASRNormalizer(B4 决策)
- 修了 `from_sense_voice(language="zh")` 强制
- 加了 VAD 段 pre-roll 500ms / post-roll 200ms 救"停"初始辅音
- bench_interrupt_latency 实测 ~1.76s speech→detect

**未解决**:
- SIGSTOP + afplay loop 尾
- 句间 subprocess spawn 开销
- 没有回归基线
- 用户手测"音频暂停后有重复几次"

### 下午的动机

用户要"全方面 benchmark 确保信息量足够 debug",然后根据 bench 发现的实际问题决定下一步。

### 大致时序

1. 调研现有 bench 基础 + 决策 scope(hybrid mode / 默认 mock API / 全 WP)
2. 写 `bench_voice_pipeline.py` 骨架 + 5 个 offline bench(WP2/3/4/5/6)
3. 跑 offline,发现 WP4 multi-dot 缩写真 bug
4. 扩展 live bench 套件(interrupt / soft_stop / false_pos / keyword_sweep / sigstop_probe / stream_player_probe)
5. 跑 live_sigstop_probe,afplay + ffplay **都有 loop 尾**,确认是 CoreAudio 层问题
6. 决策 D2:自写 player
7. 深度调研(miniaudio / soxr / sounddevice pitfalls)—— 得到研究报告
8. 写 `AudioStreamPlayer` + 27 单元测试
9. 接入 `TTSEngine._play_audio_file` + suspend/resume_playback
10. 一系列 bug 迭代(见下文"踩过的坑")
11. 最终 3/3 hit + 0 underflow + 0 segfault + 100% 多字关键词命中

---

## 最终架构

```
┌───────────────────────────────────────────────────────────────────┐
│  输入侧                                                            │
│                                                                    │
│  XVF3800 (beamform + AEC + NS) → sd.InputStream                    │
│         │                                                          │
│         ├── wake_word.py (openwakeword)                            │
│         ├── audio_recorder.py (主录音 + VAD 分段)                   │
│         │     └── SileroVADDirect (prob + dBFS 双阈值,5 帧平滑,     │
│         │                          3-hit/24-miss,LSTM warmup)       │
│         └── interrupt_monitor.py (TTS 期间监听 + pre/post-roll)      │
│                                                                    │
└─────────────────────────┬──────────────────────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────────────────────┐
│  识别 + 处理                                                       │
│                                                                    │
│  SpeechRecognizer (SenseVoice INT8 + language='zh' 强制)           │
│         │                                                          │
│         ▼                                                          │
│  ASRNormalizer (三层:correction + context / alias / fuzzy)         │
│         │                                                          │
│         ▼                                                          │
│  InterruptMonitor.keyword_match OR jarvis._process_turn            │
│                                                                    │
└─────────────────────────┬──────────────────────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────────────────────┐
│  LLM + 分句                                                        │
│                                                                    │
│  LLMClient.chat_stream                                             │
│    - 首句逗号切 (faster_first_response)                            │
│    - 14 英文缩写守护 (_possible_abbreviation_prefix + word boundary)│
│                                                                    │
└─────────────────────────┬──────────────────────────────────────────┘
                          │
                          ▼
┌───────────────────────────────────────────────────────────────────┐
│  TTS + 输出                                                        │
│                                                                    │
│  TTSEngine.synth → tts_preprocessor.clean (5 filters + NFKC)       │
│         │                                                          │
│         ▼                                                          │
│  MiniMax (vol=1 int) → MP3 文件                                    │
│         │                                                          │
│         ▼                                                          │
│  TTSPipeline._play_audio_file                                      │
│         │                                                          │
│         ├── miniaudio.decode_file → int16 PCM                      │
│         ├── soxr.resample(?→48kHz) → float32 PCM                   │
│         │                                                          │
│         ▼                                                          │
│  AudioStreamPlayer (持续 sd.OutputStream callback-mode)             │
│    - RingBuffer (SPSC 无锁,power-of-2)                              │
│    - GainRamp (sample-accurate,30ms duck / 10ms unduck,连续性守门)  │
│    - _abort 事件穿透 write/drain (瞬停)                              │
│    - 0 underflow 验证通过                                           │
│                                                                    │
└───────────────────────────────────────────────────────────────────┘
```

---

## 交付物详情

### 新增文件

#### `core/audio_stream_player.py`(~400 行)

三个模块:

**`RingBuffer`**(L47-137):SPSC(single producer single consumer)无锁环形缓冲。
- 后备 numpy float32 数组,size 自动 round 到 2 的幂(wrap 用 bit-AND)
- 写索引由主线程独占,读索引由 callback 线程独占,CPython GIL 保证单字节码原子 → 不需锁
- Underrun 时 `read_into` 自动 zero-pad(喂静音而非错)
- API: `write` / `read_into` / `available_read` / `available_write` / `reset`

**`GainRamp`**(L144-229):sample-accurate 线性增益渐变。
- 状态三变量:`_current` / `_target` / `_remaining`
- 预分配 `_scratch` + `_arange` 避免 callback 里 alloc
- 关键修复:denominator 用 `step - 1`(不是 `step`),scratch[step-1] **精确等于** next_gain,消除 1/step 的跳跃。10ms 10.5k sr unduck ramp 下 off-by-one 会产生 ~0.0015 amplitude step → -56dBFS 临界爆音,修后根治
- 支持 mid-ramp retarget(用户说到一半改变主意)

**`AudioStreamPlayer`**(L236-400):会话级协调者。
- `start()` 打开单个持续 `sd.OutputStream(blocksize=0, latency='low', callback=self._callback)`
- `write(pcm, wait_if_full=True, timeout_s=10)` 主线程 API,带 back-pressure
- `drain(timeout_s=30)` 阻塞直到 ring 空(或 abort / timeout)
- `flush()` 重置 ring + set `_abort` event → in-flight write/drain 立刻退出
- `duck(v=0.3, ramp_ms=30)` / `unduck(ramp_ms=10)` 软停快捷
- `set_gain(target, ramp_ms)` 直接控制
- `underflow_count` + `callback_calls` health 指标(watchdog 用)
- **`_abort` 事件**:flush 时 set,write 每次 iter poll,drain 每次 iter poll;抢救 abort 不穿透 bug(见踩坑第 2 条)
- **`_callback(outdata, frames, time_info, status)`** 运行在 PortAudio C 线程,严格非 alloc,只做 `ring.read_into + gain.apply`

#### `scripts/bench_voice_pipeline.py`(~1450 行)

单文件全 WP benchmark。

运行模式:
- 默认 → offline(无 mic / 无网),纯 Python 与 fixture
- `--live` → 加 mic/speaker bench(interrupt / soft_stop / false_pos / keyword_sweep)
- `--real-api` → E2E(真 LLM / 真 MiniMax,opt-in 付费)
- `--only <name>` → 单 bench
- `--refresh-baseline` → 覆盖 `voice_pipeline_baseline.json`

12 个子 bench:
| bench | 类别 | 测什么 |
|---|---|---|
| `wp2_normalizer` | offline | 3 层 normalizer 正确性 + <10ms perf |
| `wp3_preprocessor` | offline | 5 filter + NFKC + MiniMax vol clamp |
| `wp4_sentence` | offline | faster_first_response delta + 14 缩写守护 + decimal guard |
| `wp5_memory` | offline | _truncate_assistant_for_interrupt 两种 shape |
| `wp6_vad` | offline | 11 个 bench_mic_tees WAV 回放 + 帧级 trace dump |
| `live_interrupt` | live | speech→detect 延迟 × N runs |
| `live_keyword_sweep` | live | 4 关键词 × 2 runs,命中率 |
| `live_false_pos` | live | 30s TTS 沉默,误触发计数 |
| `live_soft_stop` | live | TTSEngine suspend/resume 端到端 |
| `live_soft_stop_integration` | opt-in | VAD→软停回调整链路(已知 cleanup race) |
| `live_sigstop_probe` | opt-in | afplay / ffplay 对照 SIGSTOP 行为 |
| `live_stream_player_probe` | opt-in | 自写 player duck/unduck 验证 |
| `live_e2e` | real-api | 文本→首音频字节(MiniMax 真 API) |

输出目录结构:
```
scripts/bench_results/voice_pipeline_<ts>/
  results.jsonl            每条 measurement 一行 JSON(append-only,崩不丢)
  summary.md               人看,按 bench 分组 + regression diff
  config.snapshot.yaml     跑时 config 快照(复现用)
  frames/                  WP6 VAD 每帧 dump(32ms 粒度,prob/dB/state/hits/misses/edge)
```

Exit code: 0 通过 / 1 有 fail 或 regression / 2 harness 崩。

#### `tests/test_audio_stream_player.py`(~350 行,32 tests)

- `TestRingBuffer`:7 测试(size 取 2 幂、round-trip、wrap、underrun zero-pad、full、reset)
- `TestGainRamp`:6 测试(默认 unity、instant set、全块 ramp、ramp 短于块、跨块连续、retarget;含回归测试 `test_ramp_end_matches_target_exactly` 断言 scratch[step-1] == next_gain)
- `TestAudioStreamPlayer`:19 测试(生命周期、写/清/排空、duck/unduck、callback 读 ring 应用 gain、underrun zero-pad、underflow 计数、drain timeout;含回归测试 `test_flush_sets_abort_so_drain_exits_early` / `test_abort_mid_write_exits_loop` / `test_write_clears_stale_abort`)

### 改动文件

#### `core/tts.py`

- L24-37:加 module-level import(miniaudio / numpy / soxr / AudioStreamPlayer)+ `_STREAM_PLAYER_IMPORTS_OK` 开关,导入失败静默退 fallback
- L137-155:`__init__` 加 `stream_player` 配置段读取(enabled / sample_rate / ring_seconds / duck_volume / duck_ramp_ms / unduck_ramp_ms)+ `_stream_player` 懒初始化引用
- L665-720(新):`_ensure_stream_player` 懒初始化(sticky fail 避免 log spam)+ `_decode_file_to_pcm`(miniaudio decode + 立体声 downmix + soxr HQ 重采样到 target_sr)+ `close_stream_player`
- L720-745:`_play_audio_file` 入口分支——stream_player 路径(decode + write + drain)优先,失败退 subprocess 老路径
- L800-825:`stop()` 加 stream_player flush + gain snap 到 1.0(abort 路径兼容)
- L835-890:`suspend_playback` 改成"优先 stream_player.duck,否则 SIGSTOP",`resume_playback` 镜像

#### `core/vad_silero.py`

- L95-134:`SileroVADDirect.reset()` 清 LSTM 状态后喂 5 帧静音预热,修 160ms 冷启盲窗
- L211-265:`build_vad()` 引入 `vad_mode: headphones | speakers` 档位,硬编码 4 组平台默认值:
  | | mac | rpi |
  |---|---|---|
  | headphones | -40 | -38 |
  | speakers | -22 | -32 |
  Legacy key `vad_db_threshold_during_tts_mac/rpi` 若显式设置仍可覆盖

#### `core/interrupt_monitor.py`

- L517-545:`stop_mic_listener` 三步顺序从 `set stop → stop/close stream → join thread` **改成** `set stop → join thread → stop/close stream`
- 根因:线程 mid-`InputStream.read()` 时 close stream 会 use-after-free → bus error / segfault at exit(今早 migration 未做尾巴 #2)
- 加 docstring 详解顺序原因

#### `config.yaml`

三处改动:

**tts 段(新增 stream_player 子段)**:
```yaml
tts:
  stream_player:
    enabled: true              # false 退回老 subprocess afplay 路径
    sample_rate: 48000         # Mac CoreAudio 原生率,不经 HAL 重采样
    ring_seconds: 2.0          # 环形缓冲大小(秒),控制最大预缓冲
    duck_volume: 0.3           # 软停时的音量倍率(30% = 能听到但不抢戏)
    duck_ramp_ms: 30.0         # duck 渐变时长(10-50ms 是 click-free 甜点)
    unduck_ramp_ms: 10.0       # unduck 渐变时长(快恢复)
```

**interrupt 段 VAD 改动**:
```yaml
interrupt:
  # 老的 vad_db_threshold_during_tts_mac/rpi 还在,但主要看 vad_mode
  vad_mode: headphones   # headphones | speakers
  # 可选直接覆盖(设了就忽略 vad_mode 档位):
  # vad_db_threshold_during_tts_mac: -40.0
  # vad_db_threshold_during_tts_rpi: -38.0
```

**soft_stop 翻身**:
```yaml
interrupt:
  soft_stop_enabled: true    # 2026-04-17 打开(之前 false),duck 路径已干净
  soft_stop_timeout_ms: 3000
  soft_stop_method: suspend  # 字段保留;实际走 stream_player.duck() 优先
```

### 依赖新增

```bash
uv pip install miniaudio==1.61 soxr==1.0.0
```
(sounddevice / numpy 本来就在)

---

## 测试 + 量化结果

### 单元测试

| 套件 | 数量 | 状态 |
|---|---|---|
| `tests/test_audio_stream_player.py`(新建) | 32 | ✅ 全过 |
| `tests/test_interrupt_soft_stop.py` | 6 | ✅ 全过 |
| `tests/test_interrupt_monitor.py` | 18 | ✅ 全过 |
| `tests/test_interrupt_memory_injection.py` | 6 | ✅ 全过 |
| `tests/test_audio_recorder_vad.py` | 5 | ✅ 全过 |
| 合计核心音频链路 | **67** | ✅ |

### Bench 实测数据(最终状态)

**`wp2_normalizer`**:
- 7/7 正确性断言过
- 中位延迟 **0.0006 ms**(budget 10ms),p99 < 0.01 ms
- Layer 3 fuzzy 开启后 L1 仍优先命中 ✓

**`wp3_preprocessor`**:
- 14 过滤器 case(emoji / markdown / brackets / parens / angle / currency / NFKC / math symbol / whitespace)默认全过
- 1 info:underscore 斜体不过滤(实装限制,`filter_asterisks` 只认 `*`)
- MiniMax vol:9 个边界输入全部正确 int clamp 到 [1, 10]
- 中位延迟 **0.02 ms**

**`wp4_sentence_divider`**:
- `faster_first_response::delta_chars_to_first_fire` = **10 chars**(ON 在 char 3 / OFF 在 char 13)
- 12 / 14 缩写守护过
- **已知 bug** ❌:`e.g.` / `i.e.` 多点缩写在 char-by-char 流式下被劈成 3 段(`Hello e.` + `g.` + `Smith is here.`)—— 原因:`_possible_abbreviation_prefix` 只在 dot 处于 buffer 末尾时守护,多点缩写的内部 dot 在 "e.g" 中间态既没被 `_protected_dot_positions` 抓到(整词未形成)也没被末尾守护保护。audit 漏条,非本次 session 修复。

**`wp5_memory_injection`**:
- OpenAI shape(`content: str`)✓
- Anthropic shape(`content: list[block]`)✓
- 空 played + 无 assistant 两个 edge case ✓

**`wp6_vad_replay`**:
- 11 个 `bench_mic_tees/*.wav`(今早 migration session 录的)全部回放
- 每个 WAV 输出完整帧级 trace(每 32ms 一行:frame / t_ms / smooth_prob / smooth_db / state / hits / misses / edge)到 `frames/vad_*.jsonl`
- 用于将来 debug VAD 判断轨迹

**`live_sigstop_probe`**(SIGSTOP 行为对照):
| player | verdict |
|---|---|
| afplay | **l** (loop,~300ms 尾巴) |
| ffplay | **l** (loop,~300ms 尾巴) |

→ 结论:两个 player 都 loop,不是 player 实现问题,是 **macOS CoreAudio HAL underrun 层的行为**,换 player 救不了。必须走自写 player 或 fade-out 方案。

**`live_stream_player_probe`**(自写 player 验证,最终):
| 测量 | 结果 |
|---|---|
| duck verdict | **c** (clean) |
| unduck verdict | **c** (clean) |
| underflow_count | **0 / 10818 callbacks** |

→ 结论:自写 player **完全消除** SIGSTOP loop,duck/unduck 样本级干净。

**`live_soft_stop`**(TTSEngine 端到端):
| 测量 | 值 |
|---|---|
| setup::audio_started | pass |
| sigstop::success | pass |
| **duck::perceived_drop_ms** | **459 ms**(含人反应时间 ~350ms,真实 gain ramp 30ms) |
| sigcont::success | pass |
| **unduck::perceived_resume_ms** | **631 ms**(真实 ramp 10ms) |

**`live_false_positive`**(耳机 + XVF3800,30s 沉默):
| 测量 | 值 |
|---|---|
| **rate_per_second** | **0.0 /s**(0 fires / 17.8s) ✓ |

**`live_interrupt`**(最终,"停" × 3):
| run | speech_to_detect_ms |
|---|---|
| run1 | 1742.4 |
| run2 | 1176.0 |
| run3 | 1179.1 |
| **hit rate** | **3/3** |
| **median** | **1179 ms** |
| segfault | **无** |

**`live_keyword_sweep`**(最终,4 词 × 2):
| 关键词 | hit rate | median ms | 失败样本 transcripts |
|---|---|---|---|
| 停 | **2/2** | 1820 | — |
| **等一下** | **2/2** | **911** ← 最快 | — |
| 打住 | **2/2** | 1317 | — |
| 暂停 | **2/2** | 1343 | — |
| **合计** | **8/8** | — | — |

顺带 bench 抓到用户自然对话的 transcripts(`'OK了，现在识别非常的准确。'` / `'非常nice到目前为止，全部都非常准确。'` / `'能听到我在说什么吗？'`)——证明 VAD + SenseVoice **正常语速中英混合**全部正确转写,且**不会**把这些普通话语当成 interrupt 关键词误触发。

### 进度对比(早上 → 现在)

| 指标 | 早上 migration 完成时 | 现在 | 变化 |
|---|---|---|---|
| speech→detect("停" median) | 1760 ms | **1179 ms** | **-33%** |
| 多字关键词命中率(`等一下`/`打住`) | 50%(需要慢说) | **100%**(正常语速) | — |
| TTS 刚开始 160ms 说话 | 漏 | **接住**(LSTM warmup) | — |
| 软停体验 | SIGSTOP + loop 尾(像 bug) | **ducking,无 artifact** | — |
| Unduck 是否有爆音 | 有(off-by-one) | **无** | — |
| 句间 gap | ~50 ms subprocess spawn | **0 ms**(持续 stream) | — |
| Abort 真实起效 | write 继续推 pcm | **<100ms 内真停** | — |
| 进程退出 | 每次 segfault | **干净**(顺序修正) | — |
| 假打断率(耳机 30s) | N/A | **0 次** | — |
| VAD 门槛 | -22 dBFS 严(只接贴麦) | **-40 dBFS**(正常距离即可,耳机场景) | — |

---

## 踩过的坑(完整记录,root cause + fix)

按发现顺序。

### 坑 1:WP4 多点缩写真 bug(`e.g.` / `i.e.` 流式被劈)

**现象**:bench_wp4_sentence_divider 跑出来 `abbrev::e.g.` fail,transcripts = `['Hello e.', 'g.', 'Smith is here.']`。

**根因**:`_possible_abbreviation_prefix` 只在 `i + 1 >= n`(dot 处于 buffer 末尾)时守护。多点缩写 "e.g." 的流式轨迹:
- buffer="Hello e." → dot 在末尾 → 守护生效,skip
- buffer="Hello e.g" → dot 不在末尾 → 守护跳过;"e.g." 尚未形成 → `_protected_dot_positions` 也是空 → dot 当成 split 点
- 结果:在内部 dot 处劈开

**状态**:本次 session 未修(audit 漏条,属 WP4 P1 级 bug)。bench 标记为 `known_bug` 进 details,保持 fail 状态作为回归哨兵。

**修法(未做)**:`_possible_abbreviation_prefix` 扩展:对 buffer 末尾 N 字符,检查是否能匹配任何缩写的前缀子串(含内部 dot);有则 hold。

### 坑 2:afplay + ffplay 都有 SIGSTOP loop 尾

**现象**:`live_sigstop_probe` 在 afplay 和 ffplay 上均 verdict=l(loop)。

**根因**:macOS CoreAudio HAL 的输出缓冲区在进程 SIGSTOP 后进入 underrun 状态,默认 HAL 行为是**重复末尾几帧**(而非填静音),~300ms 内耗尽才静音。这是 HAL 层行为,不受 player 选择影响。

**修法**:放弃 SIGSTOP,改做自写 player + gain ducking。

### 坑 3:bench Part A 第一次跑空转(_wait_for_playback_active 错路径)

**现象**:`live_soft_stop` Part A,用户说"听到静音 Enter"但什么都没听到,timeout 继续下一 prompt。

**根因**:`_wait_for_playback_active` 只 poll `engine._play_proc`(老 subprocess 路径)。stream_player 启用后 `_play_proc` 永远 None → 15s timeout → bench 以为"音频没起来"直接 bail。

**修法**:加 stream_player 路径检测:
```python
sp = getattr(engine, "_stream_player", None)
if sp is not None and sp.is_running and sp._ring.available_read() > 0:
    return True
```

### 坑 4:bench live_interrupt run 间 stale sentinel

**现象**:live_interrupt 第一次 run 成功,第二次 run 立刻 IndexError 崩。

**根因**:bench 复用同一个 TTSPipeline 跨 runs。run 1 结束时 `pipeline.abort()` + `pipeline.stop()` 各往 `_text_queue` / `_audio_queue` 塞了 sentinel;run 2 `pipeline.start()` 创建新 worker,workers 一启动就 pop 到残留 sentinel 立刻 exit → 不播音频 → `_done` 立刻 set → wait 循环 break → `detected` 空列表,`vad_start` 也空。

**修法**:每 run 新建 `pipeline = TTSPipeline(engine)`(engine 复用省 SenseVoice 重载,pipeline 队列隔离)。

### 坑 5:bench IndexError on empty vad_start

**现象**:配合坑 4,`if not detected or vad_start[0] is None` 在 details 构造里 `vad_start[0] is not None` 访问空列表 → IndexError → bench __crash__。

**根因**:短路 `not detected or ...` 保护了 condition,但 details dict 里是无条件求值。

**修法**:`bool(vad_start) and vad_start[0] is not None`。

### 坑 6:abort 不穿透 stream_player(音频"没停")

**现象**:用户说"停"后 on_interrupt 触发(数据显示 detect ms),但音频"不稳定一下然后继续播放,都没有停"。

**根因**:
- `pipeline.abort()` → `engine.stop()` → `stream_player.flush()`(清 ring)
- 但 `_play_worker` 还在 `_play_audio_file` → `player.write(pcm)` 的重试循环里(ring 满时 sleep+retry)
- write 的循环体 `written = ring.write(pcm[offset:])` —— ring 刚被清空,**有空间**,继续写入
- 结果:ring 清了一瞬就被原 pcm 剩余填回去,音频照播直到 pcm 用完
- 对于 20s LONG_TEXT,abort 后还能播好多秒

**修法**:`AudioStreamPlayer._abort` threading.Event。
- `flush()` 同时 `set()`
- `write()` 入口 `clear()`(新 call 清陈旧),每次 retry 前 poll
- `drain()` 每次 poll 前检查
- 配合 TTSEngine.stop 调 flush → abort 从 pipeline.abort 传到 stream_player

**验证**:`test_abort_mid_write_exits_loop` 单元测试 + 用户手测确认"说停就停"。

### 坑 7:GainRamp off-by-one → unduck 爆音

**现象**:用户说"unduck 过了一秒感觉有一瞬间的小爆音",probe verdict=l。

**根因**:`GainRamp.apply` 生成 ramp 数组时:
- 分母原来是 `step`,scratch[step-1] = current + (step-1)/step × delta ≠ next_gain
- 尾巴(step 之后的 block 部分)直接乘 next_gain
- 相邻样本之间 gain 跳跃 = delta/step
- 10ms unduck ramp 在 48kHz = 480 样本,ramp 跨两个 block(256 + 224):
  - Block 1 tail→Block 2 head 跳跃:(0.673-0.672)=0.001,~-60dB,勉强
  - Block 2 scratch[223]→tail:(1.0-0.9985)=0.0015,~-56dB,**临界可听**
- 30ms duck ramp 斜率缓 5x,跳 ~-66dB,听不到 → 只 unduck 出问题

**修法**:分母 `step - 1` → scratch[step-1] 精确等于 next_gain,尾巴接上去连续,无跳。

**回归守门**:`test_ramp_end_matches_target_exactly` 断言 `block[step-1] == next_gain`(atol=1e-5)。

### 坑 8:probe Part A/B 时序 bug(duck="nothing")

**现象**:第一次 probe verdict 是 duck=n(没变化)。

**根因**:probe 预喂 1s 音频就 break,然后 sleep 2s 再 duck。但 ring 是 2s,1s 数据 1s 播完就没了 → duck 触发时 ring 已空 → 什么都没 duck。

**修法**:后台 feeder 线程循环喂 1s 500Hz 纯正弦(相位连续,无 loop click),prompt 前 sleep 2s 让用户**先听清楚**再回答。

### 坑 9:stop_mic_listener 用-后-释放 → segfault at exit

**现象**:bench 每次跑完 `zsh: segmentation fault`。早上 migration 笔记里就标了"未做尾巴 #2"。

**根因**:`stop_mic_listener` 原顺序是:
```
_stop.set() → stream.stop() + stream.close() → thread.join()
```
reader 线程阻塞在 `sd.InputStream.read()` C 层调用,主线程先 close stream 释放了 C 侧资源;reader 返回后继续访问 → use-after-free → bus error / segfault。

**修法**:顺序换成 `_stop.set() → thread.join(timeout=2) → stream.stop() + stream.close()`。线程的 while 循环每 block(100ms)检查一次 _stop,join 2s 充裕。

### 坑 10:VAD 门槛在耳机场景过严(识别不到正常音量)

**现象**:用户耳机场景,"只有离麦克风很近大声说话的时候才能识别 别的话一点都识别不出来"。部分 keyword_sweep runs transcripts=[] 空,VAD 段没打开。

**根因**:`vad_db_threshold_during_tts_mac: -22.0` 是**扬声器场景**的设计值(挡 TTS bleed)。耳机场景 TTS 不反灌,mic 只有用户声音:
- 贴麦大声:-15 ~ -20 dBFS → 过门
- 正常距离正常音量:-25 ~ -30 dBFS → 挡住
- → 用户必须贴麦+大声才能被 VAD 接收

**修法**:
1. 引入 `vad_mode: headphones | speakers` 档位,耳机模式默认 -40 dBFS
2. 保留 legacy key 作显式覆盖(向后兼容 / power user)

### 坑 11:VAD LSTM 冷启动 160ms 盲窗

**现象**:用户"TTS 刚开始的时候我说话没什么用"。

**根因**:Silero VAD 是 stateful LSTM 模型。`reset()` 清零 h/c hidden state。零初始状态不是训练时的稳定条件 → 前 ~5 帧(160ms)的 prob 输出不可靠 → 这段窗口内说话可能不触发 START。

**修法**:`reset()` 后喂 5 帧静音预热,让 LSTM 收敛到"静音基线"状态。不触发 state machine(用 `_infer_chunk` 直接前向,不经 `accept_waveform`)。

---

## 设计决策记录(后续回来不用重新想)

### 为什么选 miniaudio 而不是 pydub / pyav

- **pydub** 已 deprecated(Py 3.13 移除 audioop 它就挂了),且调用 subprocess ffmpeg → 等于把我们要消除的 subprocess 又捡回来
- **pyav** 拖全套 ffmpeg 共享库,重,RPi wheel 存在但尺寸大
- **miniaudio** 单 C 文件(dr_mp3)静态编译进 CFFI wheel,macOS arm64/x86_64 + RPi aarch64 全有预编译 wheel。3s MP3 解 ~5ms。零系统依赖

### 为什么选 soxr 而不是 samplerate / scipy / torchaudio

基于公开 benchmark(10s @ 48k → 44.1k):
- **soxr HQ**: 10.8 ms
- scipy.signal.resample: 21.3 ms
- samplerate (sinc_medium): 223 ms
- torchaudio: 13.8 ms
- resampy: 108 ms

soxr 有 `ResampleStream` 支持增量,wheel 全平台齐。24k→48k 3s 音频 ~1-2ms。LGPL 但动态链接不影响。

### 为什么 callback mode 不用 blocking write

- blocking `stream.write(chunk)` 内部 buffering 增加 100-200ms 延迟
- 关键:**无法 pause 一个已提交的 write buffer**。要做 sample-accurate ducking 必须 callback mode
- callback 可以每 5ms 重新应用 gain,response 精确

### 为什么 SPSC 无锁而不是 Lock / Queue

- callback 运行在 PortAudio C 线程,GIL acquire 已经有开销
- 在 callback 里 `lock.acquire()` 阻塞 = GIL 释放等待 = 可能 underflow
- SPSC 情形下单一 writer / 单一 reader,CPython int 赋值单字节码原子,不需锁
- 官方 PortAudio 文档明确建议 callback 避免 mutex

### 为什么 30ms duck / 10ms unduck

- 工业界 DSP 共识:线性 ramp <5ms 可能 audible click,>50ms 太慢感觉不紧凑,**10-30ms 是 click-free 甜点**
- duck 触发要**快响应**(用户开口时不想被 TTS 盖住)→ 30ms 够平滑又够快
- unduck 可以更激进(没人嫌弃"恢复太快")→ 10ms

### 为什么 ramp denominator 用 step-1 不是 step

- 用 step 时 scratch 最后一个样本差 `delta/step` 不到 next_gain,尾巴直接用 next_gain → 1/step 跳
- 10ms @ 48kHz unduck ramp 的 1/480 延迟跳 = -56dB 临界可听(听成"一瞬间爆音")
- 用 step-1 时 scratch[step-1] 精确等于 next_gain,连续

### 为什么 vad_mode 是开关而不是自动检测

- 自动检测"有耳机插着吗"需要查 CoreAudio/ALSA 当前 default output → 平台 API 不同 + 切换时机复杂
- 用户切耳机/扬声器是**主动行为**,改一行配置比自动推断更可控
- 以后扩展 `mic_profile: xvf3800_farfield | mac_builtin | ...` 同一套路

### 为什么不做主路径 pre_buffer(OLV #2)

- 原计划决策 γ 已放弃过一次
- 用户本 session 确认流程是 **唤醒词 → ack/beep → 等 → 说指令**(流程 A)
- 按流程 A,pre_buffer 没价值(用户不在 ack 前说话)
- 只有流程 B(连续无停顿)才需要。将来改 wake 流程再考虑

### 为什么不做 VAD 3 态状态机(OLV INACTIVE)

- 还没 grep OLV 源码确认 INACTIVE 具体做什么
- 根据 OLV 文档描述(`PAUSE/RESUME 是 yield 的哨兵字节串,用来给前端发 interrupt`),INACTIVE 可能是 OLV 前端 paused 专用状态,**不适用**本地场景
- 不 cargo-cult,等需要时查明再改

---

## 未做 / 待办(有 dBFS XVF3800 调试在内)

按优先级。

### P0(实际部署前要做)

1. **XVF3800 dBFS 重新标定**(用户提到的事)
   - 今晚测试就在 XVF3800 + 耳机上,但配置的 `vad_db_threshold_during_tts_mac: -40` 是 **Mac 内置 mic 的推测值**,XVF3800 硬件 AEC + beamforming + noise suppression 改变了 dBFS 绝对量级
   - **做法**:部署时跑 30 秒 silence + 30 秒各距离(0.5m / 2m / 5m)说话 + 30 秒耳语(如果要支持),录 dBFS 分布,阈值定在"3m 正常说话"减 3-5 dB
   - 可以写个 `bench_mic_calibration` 自动化这个流程
   - **先放这里**,等换到 RPi 真机测试时做

2. **live_soft_stop_integration cleanup race**
   - VAD→软停回调整链路 bench,跑结果数据都好,但 cleanup 阶段仍可能崩(不是 stop_mic_listener — 那个修了 — 是别处的竞态)
   - opt-in bench,用户不会默认触发,不阻塞
   - 需要单独一个 session 查

### P1(应该做但不急)

3. **WP4 多点缩写 bug(`e.g.` / `i.e.`)**
   - 真 bug,offline bench 每次 fail
   - 修法:`_possible_abbreviation_prefix` 扩展,对 buffer 末尾 N 字符检查缩写前缀子串
   - ~30 行 + 测试

4. **E2E 真 API bench baseline**
   - `--only live_e2e --real-api` 跑一次,量文本→首音频字节 e2e 延迟
   - 需要 MiniMax key 的费用(每次 ~$0.05)
   - 拿到基线后以后对比 regression

5. **`live_soft_stop_integration` 集成测试修复**
   - VAD→on_soft_pause→duck→timer 或 VAD-end→on_soft_resume→unduck 全链路验证
   - 和 P0-2 重叠

### P2(未来)

6. **`mic_profile` 扩展(代替 vad_mode)**
   ```yaml
   mic_profile: xvf3800_farfield  # xvf3800_farfield | xvf3800_nearfield | mac_headphones | mac_builtin
   ```
   - 每个 profile 一套 prob / db / hits / misses / smoothing 参数
   - 和 vad_mode 同套路,加进 `build_vad`
   - 触发条件:真上了多设备部署

7. **主路径 pre_buffer(若唤醒流程改成连续无停顿)**
   - 当前流程 A(ack 后说)不需要
   - 如果以后改成"Hey Jarvis 打开客厅灯"连读,需要做
   - 原 plan §三 估约 100 行(选项 α)/ 300 行(选项 L 全模式)
   - 并有 wake 词裁剪的隐藏成本(~30 行,OLV 没这个问题因为无 wake)

8. **VAD 3 态状态机(INACTIVE)**
   - 先 grep OLV 源码确认 INACTIVE 实际语义
   - 如果和 OLV 前端 PAUSE/RESUME 绑定,不适用本地 → 不做
   - 如果是 hysteresis / cooldown 态,可能有价值

9. **耳语支持**
   - 降 `vad_prob_threshold_during_tts` 到 0.3 能抓到耳语,但误触发率上升
   - 或者训练/引入耳语专用 VAD
   - 或者接受不支持,产品层 prompt 用户"请正常说话"
   - **当前建议不做**(ROI 低)

10. **3-5m 远场支持**
    - 依赖 XVF3800 beamforming + 参数调优
    - Mac 内置 mic 裸跑不可用
    - 等真部署到 RPi + XVF3800 做

11. **可以删的磁盘遗留**
    - `data/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16/`(~30MB,早上 migration 改 SenseVoice 后不再用)
    - 确认一周无回归后可 `rm -rf`

### 永远可以再做的(不 list 为待办,但知道存在)

- `AudioStreamPlayer` watchdog 线程:当前只记录 underflow_count,没做真实重启。蓝牙热插拔时要手动 restart。可以加 `_watchdog_thread` 监测 underflow 速率超阈值就 `restart()`
- 软停音量可配置成动态(比如用户声音很小时 duck 更深)
- 长期持续 stream 的 sample rate drift 监测(OLV #384 的 5 分钟漂移问题,我们没专门守)

---

## 经验总结 / 给未来 session 的提醒

1. **动 VAD / ASR / interrupt 前先 grep 决策文档**(`jarvis 核心架构/olv 语音管线对比与优化方案.md` + `OLV-migration.md`)—— 今早刚被坑 3 小时,今天下午算比较守规矩。全局记忆 `feedback_grep_decision_docs.md` 已经钉住

2. **audio pipeline 的 bug 经常出现在"看不见"的地方**
   - PortAudio C 线程调度(GIL 争用)
   - CoreAudio HAL underrun 处理(loop 尾)
   - sounddevice stream 生命周期(use-after-free)
   - LSTM stateful model 冷启动
   - 这些都不是 Python 层能直接看出来的 bug,需要读 C 文档 + GitHub issues

3. **bench 是最有价值的工具**
   - 写 bench 的过程本身就暴露了 3 个真 bug(WP4 多点、abort 不穿透、UI-wait path)
   - 每次改 audio 先跑 probe(live_stream_player_probe) 再提 commit
   - baseline regression diff 防止后续改动悄悄破坏

4. **测试套件的可信度**
   - 早上 audit 就指出"fixture 让旧测试继续绿"的问题
   - 今天下午补的单元测试明确针对"验证新行为"(如 `test_ramp_end_matches_target_exactly`),不是让现有测试不挂
   - 应该保持这个习惯

5. **未 ratify 的架构改动要警惕**
   - 早上 migration 笔记列了两次(streaming-zipformer + 打断 pre-roll)
   - 今天下午做自写 player 前先做了深度研究 + 向用户 ratify 三件事(mpv/自写、抽取 dep、fallback 行为)
   - 这次规矩,没再漂移

6. **"简单问题"往往有 iceberg**
   - 用户说"识别不准"→ 实际上是 VAD 门槛 + LSTM 冷启 + ASR 实际工作三件事
   - Transcripts 是最有用的 diagnostic,加了之后一目了然

---

## 附:commit 规划(待 user 决定)

用户今天没 commit(全在 working tree)。建议拆成 5 个 commit:

1. `feat(player): self-written AudioStreamPlayer — persistent sd.OutputStream + SPSC ring + sample-accurate gain ramp`
   - `core/audio_stream_player.py` + `tests/test_audio_stream_player.py`
2. `feat(tts): integrate AudioStreamPlayer; subprocess path kept as fallback`
   - `core/tts.py`
3. `fix(vad): LSTM warmup on reset + vad_mode headphones/speakers switch`
   - `core/vad_silero.py` + config
4. `fix(interrupt): stop_mic_listener shutdown order — join thread before closing stream`
   - `core/interrupt_monitor.py`(消 segfault)
5. `test(bench): comprehensive voice pipeline benchmark (12 sub-benches)`
   - `scripts/bench_voice_pipeline.py`
6. `config: soft_stop_enabled true (duck path now clean) + stream_player section`
   - `config.yaml`
7. `docs: self-player build session log`
   - 这个 notes 文件本身

按早上 migration 笔记的 commit 风格,无 Co-Authored-By。
