# Outcome Detector — NLI + Web Thumbs 接入 · 实施计划

## 目标

把现有 `memory/cold/outcome_detector.py`（纯 regex 59 行）替换为 **NLI + web thumbs** 双信号源，与 trace v3 的 `outcome_signal` 写入链路对接。

- **NLI**：Erlangshen-Roberta-110M-NLI，语义判断用户反馈
- **Web thumbs**：浏览器 UI 显式按钮，作最高优先级 ground truth
- **Regex**：DEPRECATED（源码保留作注释，不再调用）

另需将主路径从 sync 阻塞模式改成 async submit 模式（原 regex 0ms 同步没事，加 NLI 后必须 async 防阻塞）。

这是 v2 migration 漏做的 outcome layer 2/3 补齐，不是新方案。

---

## 背景（自包含）

### 当前代码状态（2026-04-21 grep 确认）

`memory/cold/outcome_detector.py`（**注意 cold/ 子目录**）：纯 regex 59 行，只匹配 ≤30 字锚定句（`^好的$` / `^不对$` 等）。

`memory/trace.py`：`TraceLog.update_outcome(trace_id, signal, at_turn_id)` 已存在，写入 `trace.outcome_signal` 列（INTEGER，CHECK -1/0/1/NULL）。

`jarvis.py:1458-1465` 当前对接方式（**同步**，不是 async）：
```python
# 在 _capture_turn_input (turn 入口) 里调用
from memory.cold.outcome_detector import detect_outcome
signal = detect_outcome(text)  # ← 同步调用，regex 0ms 无感
if signal is not None and self._last_trace_id is not None:
    self._pending_outcome_update = (self._last_trace_id, signal)
else:
    self._pending_outcome_update = None
```

`jarvis.py:1810-1813` `_flush_trace` 里 apply pending：
```python
if self._pending_outcome_update is not None:
    prev_id, signal = self._pending_outcome_update
    self.trace_log.update_outcome(prev_id, signal=signal, at_turn_id=trace_id)
    self._pending_outcome_update = None
```

**关键问题**：当前 `detect_outcome` 是同步阻塞主路径。regex 0ms 没事，**NLI 100-300ms 会阻塞 → 用户感知延迟增加**。因此本 plan **必须改对接架构**（见阶段 3.2）。

### 规划设计（这次要落地的）

两层信号源 + 一个异步改造：

```
outcome 信号来源（优先级从高到低）：

1. web thumbs（显式 ground truth，最高优先级）
   └─ 前端按钮 → POST /api/outcome → trace_log.update_outcome
   └─ 完全 bypass detect_outcome 函数（直接写 DB）

2. NLI 编码器（语义判断）
   └─ Erlangshen-Roberta-110M-NLI（中文 native NLI corpus 训练）
   └─ ONNX INT8 ~110MB
   └─ 异步调用（self._executor.submit）
   └─ contradiction > 0.7 → -1
   └─ entailment > 0.7  → +1
   └─ 其他            → NULL

3. NULL（None）
   └─ Phase 3 SQL 过滤器把 NULL 当"未知不过滤"

（regex 层 DEPRECATED：代码保留不调用，供测试/rollback 参考）
```

### 对接架构变更

当前 sync pattern：`_capture_turn_input` → `detect_outcome(text)` sync → 存 `_pending_outcome_update` → `_flush_trace` 里 apply

改造后 async pattern：`_capture_turn_input` 不调用 detect_outcome → `_flush_trace` 里 `self._executor.submit(_resolve_outcome)` → executor thread 跑 NLI 推理 → 完成后写 `update_outcome`

新增 state `_last_user_text`，删除 state `_pending_outcome_update`。

### 模型决策（已锁定，不要再问用户）

- **主模型**：`IDEA-CCNL/Erlangshen-Roberta-110M-NLI`（Apache 2.0, 110M params, ~110MB INT8 ONNX, 中文 CMNLI 80.8% / OCNLI 78.6%）
- **备选（暂不用，只记入 notes）**：`MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7`（317MB INT8, 80.3% zh XNLI + 85.7% en MNLI）
- **ONNX 导出**：用 `optimum` 一次性脚本，INT8 量化
- **模型位置**：`data/nli-erlangshen/` （跟 `data/sensevoice-small-int8/` 一致风格）
- **部署目标**：M2 Max 生产（~40-80ms/次），RPi5 async 后台跑（1-3s 可接受）
- **语言**：中文 primary，英文 fallback 到 regex（"yes"/"no"/"wrong" regex 覆盖够）

### 阈值决策（已锁定）

- NLI contradiction > 0.7 → -1
- NLI entailment > 0.7 → +1
- 其他 → NULL
- 用户 text 长度 > 500 字 → truncate 前 500 再跑 NLI
- 用户 text 为空 → return NULL（不跑任何 layer）

### Web thumbs UI 决策（已锁定）

- 位置：`ui/web/` 现有 Live2D 前端界面，**每条 assistant 响应下**浮出 3 按钮
- 形态：3 按钮 `👍 / 👎 / ⏭`（跳过）
- 行为：点击 → `POST /api/outcome {session_id, turn_id, signal}` → 后端 `trace_log.update_outcome(trace_id, signal)`
- `trace_id` 由前端从 WS 消息或 response metadata 拿到
- voice-only 场景（没 web UI 打开）：thumbs 不启用，靠 regex + NLI 自然兜底
- 重复点击：idempotent（后覆盖前，`update_outcome` 本身已支持）

---

## 实施顺序（5 阶段）

每阶段结束 **必须跑对应验证**，不通过不进下一阶段。

### 阶段 1 · ONNX 导出脚本

**产物**：`scripts/export_nli_onnx.py`（一次性脚本，运行一次产模型文件）

**步骤**：
1. 用 `optimum-cli export onnx` 或等价 Python API 导出 `IDEA-CCNL/Erlangshen-Roberta-110M-NLI`
2. 量化为 INT8（`optimum-cli onnxruntime quantize --avx2`）
3. 输出到 `data/nli-erlangshen/model.onnx` + tokenizer 配置文件
4. 脚本开头检查：如果 `data/nli-erlangshen/model.onnx` 已存在，跳过（幂等）

**验证**：
```bash
python scripts/export_nli_onnx.py
ls -la data/nli-erlangshen/
# 应该看到 model.onnx (~110MB) + tokenizer.json + config.json
```

**依赖**：临时装 `optimum[onnxruntime]` 和 `transformers`（仅导出用），`onnxruntime` 已在 requirements。

### 阶段 2 · NLI 分类器封装

**产物**：`memory/cold/nli_classifier.py`（新文件，~100 行）

**接口**：
```python
class NLIClassifier:
    """Erlangshen-Roberta-110M-NLI wrapper, lazy-loaded."""
    
    def __init__(self, model_dir: str | Path = "data/nli-erlangshen") -> None:
        """Store path, defer model load until first classify()."""
    
    def classify(self, premise: str, hypothesis: str) -> dict[str, float]:
        """Return {entailment: float, neutral: float, contradiction: float}, sum=1.0.
        
        Lazy-loads model on first call. Thread-safe via threading.Lock around init.
        """
    
    def detect_outcome(self, user_text: str) -> int | None:
        """Convenience: check user_text against fixed hypothesis pair.
        
        Runs classify() twice:
          - hypothesis_neg = "用户在纠正或表达不满"
          - hypothesis_pos = "用户表达认可或满意"
        
        Returns -1 if contradiction(neg hypothesis) > 0.7,
                +1 if entailment(pos hypothesis) > 0.7,
                None otherwise.
        """
```

**实现细节**：
- 用 `onnxruntime.InferenceSession` 加载 model.onnx
- 用 `transformers.AutoTokenizer` 加载 tokenizer（或手写 tokenizer 规则，但 `transformers` 已在 stack）
- Lazy init：`_session` 和 `_tokenizer` 默认 None，第一次 `classify()` 调用时建
- **不要** app init 时加载（避免 boot 慢）
- Lock 保证多线程 lazy init 安全

**验证**：
创建 `tests/test_nli_classifier.py`，手写 10+ 标注样本：

```python
TEST_CASES = [
    # (user_text, expected_signal, reason)
    ("好的", +1, "short positive"),
    ("不对", -1, "short negative"),
    ("嗯不太对吧", -1, "medium negative regex misses"),
    ("你说得很对", +1, "medium positive"),
    ("其实我想问的是别的", -1, "implicit correction"),
    ("嗯", None, "ambiguous filler"),
    ("", None, "empty"),
    ("对了我还想问", None, "topic shift, not feedback"),
    ("我觉得这个回答很有道理", +1, "positive long"),
    ("不是这个意思我是说...", -1, "correction with followup"),
]
```

断言 ≥ 8/10 通过（允许 2 个边界 case fail，因为 NLI 不完美）。

### 阶段 3 · NLI 替换 regex + 主路径改 async

这阶段改动最大也最易出错，**严格按下面的步骤做**。

#### 3.1 `memory/cold/outcome_detector.py` — regex 注释为 deprecated

保留 `_POSITIVE_PATTERNS` / `_NEGATIVE_PATTERNS` / `_POS_RE` / `_NEG_RE` 代码不删除，但在文件头部 + 每个 pattern 列表前加注释：

```python
"""DEPRECATED regex layer — kept for historical reference only.

Superseded by NLI-based detection (see memory/cold/nli_classifier.py).
The regex patterns below are no longer called by detect_outcome() as of
2026-04-21. Kept in source for:
  1. rollback path (if NLI model unavailable, can be re-enabled)
  2. test fixture reference
  3. comparison benchmarks
Do NOT extend or modify these patterns for production use.
"""

# DEPRECATED — superseded by NLI. Kept for reference only.
_POSITIVE_PATTERNS = [ ... ]

# DEPRECATED — superseded by NLI. Kept for reference only.
_NEGATIVE_PATTERNS = [ ... ]
```

**`detect_outcome()` 函数主体替换为 NLI-only**：

```python
def detect_outcome(
    user_text: str,
    nli: NLIClassifier | None = None,
) -> int | None:
    """Detect outcome signal via NLI layer.
    
    Args:
        user_text: user utterance (will be stripped).
        nli: NLIClassifier instance. If None → return None (regex layer is
             deprecated and will not be invoked as fallback).
    
    Returns:
        +1 (entailment) / -1 (contradiction) / None (ambiguous or no NLI).
    """
    text = user_text.strip()
    if not text or len(text) < 5 or len(text) > 500:
        return None
    if nli is None:
        return None
    try:
        return nli.detect_outcome(text)
    except Exception:
        LOGGER.exception("NLI outcome detection failed, returning None")
        return None
```

**保留但标为 deprecated 的辅助函数**（供测试用）：

```python
def _detect_regex_only(user_text: str) -> int | None:
    """DEPRECATED: regex-layer detection kept for test fixtures only.
    
    Production code must use detect_outcome() which goes through NLI.
    """
    text = user_text.strip()
    if not text or len(text) > 30:
        return None
    for r in _POS_RE:
        if r.match(text):
            return 1
    for r in _NEG_RE:
        if r.match(text):
            return -1
    return None
```

#### 3.2 `jarvis.py` — 4 处改动（关键对接）

**3.2.a**：`__init__` 加 `nli_classifier` init（第 ~140 行附近，靠近 `self.trace_log = TraceLog(...)`）：

```python
from memory.cold.nli_classifier import NLIClassifier
self.nli_classifier = NLIClassifier()  # lazy-loaded internally
```

**3.2.b**：`__init__` state 变量更新（第 ~173-174 行）：

```python
# 删除：
# self._pending_outcome_update: tuple[int, int] | None = None

# 保留：
self._last_trace_id: int | None = None

# 新增（outcome 需要在 _flush_trace 里异步提交）：
self._last_user_text: str | None = None
```

**3.2.c**：删除 `_capture_turn_input` 里的同步 detect_outcome 调用（第 ~1458-1465 行）：

```python
# 原代码（全部删除）：
# from memory.cold.outcome_detector import detect_outcome
# signal = detect_outcome(text)
# if signal is not None and self._last_trace_id is not None:
#     self._pending_outcome_update = (self._last_trace_id, signal)
# else:
#     self._pending_outcome_update = None
```

（这段同步阻塞主路径的代码全部清理，换成在 `_flush_trace` 里 async。）

**3.2.d**：`_flush_trace` 里替换 pending update 逻辑为 async submit（原第 ~1810-1813 行）：

```python
# 原代码（删除）：
# if self._pending_outcome_update is not None:
#     prev_id, signal = self._pending_outcome_update
#     self.trace_log.update_outcome(prev_id, signal=signal, at_turn_id=trace_id)
#     self._pending_outcome_update = None

# 新代码：
if self._last_trace_id is not None and self._last_user_text is not None:
    prev_id = self._last_trace_id
    prev_user_text = self._last_user_text
    cur_trace_id = trace_id
    nli = self.nli_classifier
    tl = self.trace_log
    
    def _resolve_outcome() -> None:
        from memory.cold.outcome_detector import detect_outcome
        signal = detect_outcome(prev_user_text, nli=nli)
        if signal is not None:
            tl.update_outcome(prev_id, signal=signal, at_turn_id=cur_trace_id)
    
    self._executor.submit(_resolve_outcome)

# 保留（更新 last_trace_id）：
self._last_trace_id = trace_id

# 新增（记住本 turn user_text，下 turn flush 用）：
self._last_user_text = user_text  # 从 log_turn 的 kwargs 里取
```

#### 3.3 测试

**保留现有 `tests/test_outcome_detector.py`**（测 regex 层 deprecated 版本 + `_detect_regex_only` 直接调用），改成用 `_detect_regex_only` 导入：

```python
# 原：
from memory.cold.outcome_detector import detect_outcome
# 改为（保留历史 regex 测试）：
from memory.cold.outcome_detector import _detect_regex_only as detect_outcome
```

这样老 regex 测试依然能跑（验证 deprecated 代码不坏），但不再测 production `detect_outcome` 的 regex 路径（因为 NLI-only 了）。

**新建 `tests/test_outcome_detector_nli.py`** 验证 NLI-only 路径：

```python
def test_nli_none_returns_none():
    """No NLI instance = always None (regex deprecated)."""
    assert detect_outcome("好的") is None       # regex 不再兜底
    assert detect_outcome("不对") is None
    assert detect_outcome("嗯不太对吧") is None

def test_nli_positive():
    nli = MagicMock()
    nli.detect_outcome.return_value = 1
    assert detect_outcome("我觉得很好", nli=nli) == 1

def test_nli_negative():
    nli = MagicMock()
    nli.detect_outcome.return_value = -1
    assert detect_outcome("嗯不太对吧", nli=nli) == -1

def test_length_filter():
    """Too short / too long → None without NLI call."""
    nli = MagicMock()
    assert detect_outcome("嗯", nli=nli) is None  # len < 5
    assert detect_outcome("a" * 501, nli=nli) is None  # len > 500
    nli.detect_outcome.assert_not_called()

def test_nli_exception_falls_to_none():
    nli = MagicMock()
    nli.detect_outcome.side_effect = RuntimeError("boom")
    assert detect_outcome("嗯不太对吧", nli=nli) is None
```

**新建 `tests/test_jarvis_outcome_async.py`** 验证 jarvis.py 对接：

```python
def test_flush_trace_submits_outcome_to_executor(jarvis_app):
    # turn 1: log some user_text
    jarvis_app._last_user_text = "美元换人民币"
    jarvis_app._last_trace_id = 1
    
    # turn 2 flush 触发
    submitted_callables = []
    jarvis_app._executor = MagicMock()
    jarvis_app._executor.submit.side_effect = lambda fn: submitted_callables.append(fn)
    
    jarvis_app._flush_trace(..., user_text="下一 turn 话")
    
    # 应该 submit 了一个 callable，但还没真正跑 NLI
    assert len(submitted_callables) == 1
    # 主路径没被 NLI 阻塞
    # 手动跑 callable（模拟 executor thread）
    submitted_callables[0]()
    # 现在 trace.outcome_signal 应该更新了
```

#### 3.4 验证

```bash
pytest tests/test_outcome_detector.py tests/test_outcome_detector_nli.py tests/test_jarvis_outcome_async.py -q
```

全绿才能进阶段 4。

### 阶段 4 · Web Thumbs UI + Backend

**Backend 端**（修改 `ui/web/server.py`）：

新增 endpoint：
```python
@app.post("/api/outcome")
async def post_outcome(payload: dict):
    """Record explicit user feedback (thumbs up/down/skip).
    
    Payload: {session_id: str, turn_id: int, signal: int}
    
    - signal = 1  → thumbs up (positive)
    - signal = -1 → thumbs down (negative)
    - signal = 0  → skip (neutral, explicit no-opinion)
    
    Writes to trace.outcome_signal via trace_log.update_outcome.
    Looks up trace_id by (session_id, turn_id).
    """
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    signal = payload.get("signal")
    
    if signal not in (-1, 0, 1):
        return JSONResponse({"error": "signal must be -1, 0, or 1"}, status_code=400)
    
    # lookup trace_id
    trace = trace_log.query_by_session_turn(session_id, turn_id)  # new helper
    if trace is None:
        return JSONResponse({"error": "trace not found"}, status_code=404)
    
    trace_log.update_outcome(trace["id"], signal, at_turn_id=turn_id)
    return {"ok": True, "trace_id": trace["id"]}
```

需要在 `memory/trace.py` 新增 helper（如果不存在）：
```python
def query_by_session_turn(self, session_id: str, turn_id: int) -> dict | None:
    """Lookup trace row by (session_id, turn_id). Returns dict or None."""
```

**Frontend 端**（修改 `ui/web/` 现有 Live2D 前端）：

1. 找到渲染 assistant response 的 DOM 区域
2. 在每条 response 下追加 3 按钮：`<button data-signal="1">👍</button> <button data-signal="-1">👎</button> <button data-signal="0">⏭</button>`
3. Click handler：
```js
async function sendOutcome(session_id, turn_id, signal) {
  await fetch('/api/outcome', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id, turn_id, signal})
  });
  // 点完后视觉 feedback（button 变灰或打勾）
}
```
4. `session_id` 和 `turn_id` 从现有 WS response payload 里拿（应该已经有，如果没有，需要 server.py 在发 assistant response 时带上）

**验证**：
```bash
# 1. 启动 jarvis + web UI
python -m ui.web.server &
# 2. 浏览器打开 localhost:8000
# 3. 说一句话 → 看 response 下出现 3 按钮
# 4. 点 👎 → 看 browser devtools network 里 POST /api/outcome 200
# 5. 查 DB
sqlite3 data/memory/jarvis_memory.db "SELECT id, session_id, turn_id, outcome_signal FROM trace ORDER BY id DESC LIMIT 1"
# 应该看到最新 trace 的 outcome_signal = -1
```

### 阶段 5 · 端到端集成测试

**测试脚本**：`tests/test_outcome_detector_e2e.py`（或手动验证）

**5.1 语义正确性（NLI 真跑 + 真写入 trace）**

1. 启动真 jarvis（`python jarvis.py --no-wake`）
2. 说 `"美元换人民币"` → LLM 回答
3. 下一 turn 说 `"嗯不太对吧，我是问加元"`（regex 原本漏，NLI 应命中 -1）
4. 第三 turn 任意说一句，触发 `_flush_trace` 的 async outcome submit
5. 等 ~1-2s（NLI 后台推理完成窗口）
6. 查 DB：
```bash
sqlite3 data/memory/jarvis_memory.db -cmd ".mode line" "SELECT id, user_text, assistant_text, outcome_signal FROM trace ORDER BY id DESC LIMIT 3"
```
7. 期望：**第二条 trace**（"美元换人民币"那条）的 `outcome_signal = -1`

**5.2 Latency 不回归（关键！）**

NLI 接入后主路径 latency 不能增加。手动对比：

```bash
# 基线（本 commit 前）：
python system_tests/runner.py --mode cc --suite general
# 记录 average turn latency

# 接入后（本 commit 后）：
python system_tests/runner.py --mode cc --suite general
# average turn latency 不应超过基线的 +5%
```

如果 latency 回归 >5%，说明 NLI 意外跑在主路径上 → 回去检查 `_capture_turn_input` 是否真删干净了同步 detect_outcome，或 `_flush_trace` 是否真的 async submit（没 `.result()` 等待）。

**5.3 async resolution 真的生效**

```bash
# 开 DEBUG 日志跑 1 个 turn，看 log 里有没有：
#   - "NLI classifier lazy load" 信息（first call）
#   - "trace outcome updated: id=X signal=Y" 信息
#   - 这两条 log 的时间戳应该**晚于** "trace logged: id=X" 的时间戳
# 如果 outcome update 的时间戳跟 trace log 同时出现 → 说明没 async，是 sync 调用 → bug
```

三条验证全过才算阶段 5 通过。

---

## 文件清单

### 新建文件

| 文件 | 行数估 | 内容 |
|---|---|---|
| `scripts/export_nli_onnx.py` | ~40 | 一次性 ONNX 导出脚本 |
| `memory/cold/nli_classifier.py` | ~120 | NLI wrapper with lazy load |
| `tests/test_nli_classifier.py` | ~80 | NLI 分类器单元测试 |
| `tests/test_outcome_detector_nli.py` | ~60 | NLI-only 路径测试（替代旧 cascade 测试）|
| `tests/test_jarvis_outcome_async.py` | ~60 | jarvis.py `_flush_trace` 异步 submit 对接测试 |
| `data/nli-erlangshen/model.onnx` | n/a | 导出的 ONNX 模型（gitignored）|
| `data/nli-erlangshen/tokenizer.json` | n/a | tokenizer 配置（gitignored）|
| `data/nli-erlangshen/config.json` | n/a | model 配置（gitignored）|

### 修改文件

| 文件 | 改动 |
|---|---|
| `memory/cold/outcome_detector.py` | regex 层加 DEPRECATED 注释（保留代码），`detect_outcome()` 主体改成 NLI-only |
| `memory/trace.py` | 新增 `query_by_session_turn(session_id, turn_id)` helper（如不存在）|
| `jarvis.py` | 4 处改动：①`__init__` 加 `self.nli_classifier = NLIClassifier()`；②state：删 `_pending_outcome_update`，加 `_last_user_text`；③`_capture_turn_input` 删同步 detect_outcome 调用；④`_flush_trace` 改 async submit |
| `tests/test_outcome_detector.py` | `from ... import detect_outcome` 改成 `from ... import _detect_regex_only as detect_outcome`（保留 regex 历史测试但指向 deprecated helper）|
| `ui/web/server.py` | 新增 `POST /api/outcome` endpoint |
| `ui/web/<existing frontend>.html/js` | 加 thumbs 按钮 + click handler |
| `.gitignore` | 加 `data/nli-erlangshen/` |
| `config.yaml` | 新增 `outcome_detector:` 段（当前 config.yaml 无此段，首次创建）|

### config.yaml 新段示例

```yaml
outcome_detector:
  nli:
    enabled: true
    model_dir: data/nli-erlangshen
    contradiction_threshold: 0.7
    entailment_threshold: 0.7
    max_text_length: 500
    min_text_length: 5
```

---

## 成功判定

全部必须满足：

1. ✅ `scripts/export_nli_onnx.py` 一次性跑成功，产出 `data/nli-erlangshen/model.onnx < 150MB`
2. ✅ `pytest tests/test_nli_classifier.py -q` 全绿（10+ 样本 ≥8 通过）
3. ✅ `pytest tests/test_outcome_detector.py tests/test_outcome_detector_nli.py tests/test_jarvis_outcome_async.py -q` 全绿
4. ✅ `python -m ui.web.server` + 浏览器测试：点 👎 后 DB 里最新 trace `outcome_signal = -1`
5. ✅ 端到端语义：说"嗯不太对吧"类中长句 → 下一 turn 后 DB 里 `outcome_signal = -1`（证明 NLI 跑起来了）
6. ✅ **端到端 latency**：`system_tests/runner.py --suite general` 跑完平均 turn latency ≤ 基线 +5%（证明 NLI 真的 async，没阻塞主路径）
7. ✅ **async 时序**：DEBUG log 里 "trace outcome updated" 时间戳 **晚于** "trace logged" 时间戳 ≥50ms（证明不是 sync）
8. ✅ `python -m pytest tests/ -q` 无新 regression（整套测试 pass）

---

## Non-goals（不要做）

- 不要在 `detect_outcome()` 主函数里回退到 regex——regex 已 DEPRECATED，NLI 失败就 return None
- 不要**删除** regex `_POSITIVE_PATTERNS` / `_NEGATIVE_PATTERNS` 代码——只加 DEPRECATED 注释，保留源码
- 不要在 `_capture_turn_input` 或任何用户感知路径里做**同步** NLI 推理——必须走 `self._executor.submit()` 异步
- 不要把"通过 thumbs 收集的 ground truth 自动校准 NLI"这层做了——那是 Phase 3.5 自我改进，不在本计划
- 不要加 `mDeBERTa` 备选的实际加载代码——只记入注释说"备选，未启用"
- 不要改 `update_outcome` 的签名或行为——它已经 stable
- 不要改 trace schema——`outcome_signal` 列早已存在
- 不要动 Phase 3 SQL 过滤器（`WHERE outcome_signal IS NULL OR >= 0`）——已存在且正确
- 不要把 NLI 模型改成同步加载 / app init 时加载——必须 lazy，首次 `classify()` 触发
- 不要保留 `_pending_outcome_update` 作向后兼容——彻底删除，用新 async submit 模式

---

## 已锁定决策清单（别再问用户）

| 决策 | 锁定值 |
|---|---|
| 主 NLI 模型 | `IDEA-CCNL/Erlangshen-Roberta-110M-NLI` |
| ONNX 量化 | INT8 |
| 检测顺序 | thumbs (DB 直写) > NLI > NULL。**regex 废弃不参与** |
| Regex 代码处置 | 保留 + `DEPRECATED` 注释，production `detect_outcome()` 不调用 |
| NLI 阈值 | contradiction 0.7 / entailment 0.7 |
| 最大文本长度 | 500 字截断 |
| 最小文本长度 | 5 字（短于这个直接 NULL） |
| 模型位置 | `data/nli-erlangshen/` |
| Lazy load | 是 |
| 主路径调用方式 | **async submit 到 `self._executor`**，非同步阻塞（关键对接改动） |
| Outcome 写入时机 | `_flush_trace` 内 async submit，由 executor thread resolve |
| Thumbs 按钮数量 | 3（👍 / 👎 / ⏭） |
| Thumbs 语义 | 1 / -1 / 0 |
| Thumbs idempotent | 是（覆盖写） |
| Thumbs voice-only 回退 | 不启用，NLI 兜底 |
| Backup NLI 模型 | `mDeBERTa-v3-base-xnli-multilingual-nli-2mil7`，**只记注释，不加载** |

---

## 预估工作量

| 阶段 | 时间 |
|---|---|
| 1. ONNX 导出 | 30min |
| 2. NLI classifier | 2-3h |
| 3. Cascade 集成 | 2-3h |
| 4. Web thumbs UI + backend | 2-3h |
| 5. 端到端测试 | 30min |
| **合计** | **~1 个工作日** |

---

## 不清楚时如何处理

- **模型下载/导出失败** → 记录 warning，NLI 层降级到 no-op（`NLIClassifier.detect_outcome` 直接 return None），整个系统回退到 regex-only。不要阻塞 jarvis 启动。
- **NLI 推理报错** → `detect_outcome` 外层 try/except，log 后 return None，不要 raise 到主路径
- **Thumbs endpoint 收到异常 payload** → 400 响应，不影响后续正常请求
- **trace_id 查不到** → 404 响应（可能是老 session 已过期），不影响新 trace

---

## 不变式（实施时反复验证）

- **jarvis.py 主路径 latency 不能变慢**：`_capture_turn_input` 里原本 0ms 的 regex detect_outcome 调用被删除后，该位置不能有任何新的同步调用。NLI 必须在 `_flush_trace` 里 `self._executor.submit()` 异步跑
- **`_flush_trace` 自身不能被 NLI 阻塞**：它 submit 完 callable 就继续，不 `result()` 等 NLI 完成
- `memory/cold/outcome_detector.py` 的 `_detect_regex_only()` 必须可单独调用（deprecated 但保留，供测试）
- `trace.outcome_signal` 列的 CHECK 约束（-1/0/1/NULL）**不变**
- `update_outcome(trace_id, signal, at_turn_id)` 签名**不变**
- regex `_POSITIVE_PATTERNS` / `_NEGATIVE_PATTERNS` / `_POS_RE` / `_NEG_RE` **代码零修改**（只加 deprecated 注释）
- 导出的 model.onnx **不进 git**（体积 ~110MB，.gitignore 必加）
- `_pending_outcome_update` state 字段在改后**彻底消失**（不留死代码）

---

## 参考

- Erlangshen-Roberta-110M-NLI: https://huggingface.co/IDEA-CCNL/Erlangshen-Roberta-110M-NLI
- Optimum ONNX 导出：https://huggingface.co/docs/optimum/onnxruntime/usage_guides/export_a_model
- trace v3 schema: `memory/trace_migration.py:V3_SCHEMA_SQL`
- 现有 outcome_detector: `memory/cold/outcome_detector.py`
- 现有 trace_log.update_outcome: `memory/trace.py:291`
