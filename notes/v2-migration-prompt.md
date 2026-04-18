# Jarvis v2 迁移 — 完整执行 Prompt

## 必读文件（按顺序）

1. **`notes/memory.md`** — 重点精读第十三章到第十九章（现状地图、v2 架构、自动学习闭环、L1 YAML 格式、工具块确认清单、整体架构图、记忆块确认清单）。前面章节快速扫一遍了解上下文即可。
2. **`CLAUDE.md`** — 项目规则和现有架构概览
3. **`jarvis.py`** — 主入口，重点看 `_register_skills()` (L1308-1386)、记忆相关 (L130-162, L830-845, L1080-1111)、行为日志 (L1096-1111)

---

## 迁移目标

把 Jarvis 从 v1（14 个 Skill class + SkillRegistry + GPT-4o-mini 记忆提取 + behavior_log）迁移到 v2（ToolRegistry + @jarvis_tool + YAML interpreter + Observer OM 记忆 + trace 表）。

完成后：
- 现有功能不 break（天气、灯控、提醒、待办、汇率都正常工作）
- 记忆系统从 v1 的"向量检索 top-k + GPT 问答"切换到"Observer 抽取 + stable prefix 全塞"
- 技能系统从 14 个 Skill class 切换到 ~10 个 @jarvis_tool 函数 + 2 个 YAML tool
- 所有旧代码清理干净，grep 不到 SkillRegistry/SkillFactory/LearningRouter/SkillLoader

---

## 编码原则（严格遵守）

1. **Think Before Coding** — 不假设，不确定就问，有多条路说出来
2. **Simplicity First** — 只写被要求的最小代码，不加未要求的功能
3. **Surgical Changes** — 只动必须动的，不顺手改进不相关代码
4. **Goal-Driven Execution** — 每步有可验证标准，做完验证再往下

---

## 全局约束

- Python 3.13，所有设置从 config.yaml 读
- Type hints everywhere，Google-style docstrings
- 用 logging 不用 print（除了现有的调试 print）
- 不动 `data/speechbrain_model/` 和 `data/sensevoice-small-int8/`
- 不硬编码 IP/API key/文件路径
- 不绕过 permission_manager
- 改完跑 `python -m pytest tests/ -q`

---

## Phase 1 · 记忆系统重建

### 1.1 trace 表（替代 behavior_log）

**文件**: `memory/trace.py`（新建）

把现有 `memory/behavior_log.py` 的 behavior_log 表升级为 trace 表。schema：

```sql
CREATE TABLE IF NOT EXISTS trace (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    turn_id         INTEGER,          -- 会话内递增
    created_at      TEXT NOT NULL,     -- ISO timestamp
    user_text       TEXT,
    assistant_text  TEXT,
    user_emotion    TEXT,              -- SenseVoice 情感
    tts_emotion     TEXT,              -- TTS 用的情感
    path_taken      TEXT,              -- farewell/direct_answer/l1_skill/local/l2_skill/cloud_llm
    tool_calls      TEXT,              -- JSON: [{name, args, result, ms}]
    llm_model       TEXT,
    llm_tokens_in   INTEGER,
    llm_tokens_out  INTEGER,
    latency_ms      INTEGER,          -- end-to-end
    outcome_signal  INTEGER,          -- null/-1/0/+1
    outcome_at_turn_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trace_session ON trace(session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_trace_path ON trace(path_taken, created_at DESC);
```

接口：
```python
class TraceLog:
    def __init__(self, db_path: str | Path = "data/memory/jarvis_memory.db"): ...
    def log_turn(self, *, session_id, turn_id, user_text, assistant_text,
                 user_emotion=None, tts_emotion=None, path_taken=None,
                 tool_calls=None, llm_model=None, llm_tokens_in=None,
                 llm_tokens_out=None, latency_ms=None) -> int: ...  # 返回 trace id
    def update_outcome(self, trace_id: int, signal: int) -> None: ...
    def query_for_observer(self, trace_id: int) -> dict: ...  # 返回 user_text + assistant_text + tool_calls
    def query_cloud_traces(self, days: int = 7) -> list[dict]: ...  # Phase 3 夜批用
```

与现有 BehaviorLog 共用同一个 SQLite 文件（`data/memory/jarvis_memory.db`），WAL 模式。BehaviorLog 的旧 behavior_log 表保留不删（历史数据），但 jarvis.py 里改为写 trace 表。

### 1.2 observations 表 + Observer

**文件**: `memory/observer.py`（新建）

observations 表 schema：

```sql
CREATE TABLE IF NOT EXISTS observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id        INTEGER,          -- Observer 每次产一块
    created_at      TEXT NOT NULL,
    content         TEXT,             -- markdown 段落，格式：
                                      -- Date: 2026-04-15
                                      -- * 🔴 (14:30) 用户偏好客厅灯暖黄色 2700K
                                      -- * 🟡 (14:30) 用户语气疲惫
                                      -- * ✅ (14:30) 客厅灯已调为暖黄
    source_turn_id  INTEGER,          -- FK → trace.id
    superseded_by   INTEGER           -- 留字段，默认 null，将来纠正用
);
CREATE INDEX IF NOT EXISTS idx_obs_created ON observations(created_at);
```

Observation emoji 优先级：
- 🔴 HIGH — 身份、偏好、目标、关键事实
- 🟡 MED — 项目细节、工具结果、情感状态
- 🟢 LOW — 不确定、次要
- ✅ DONE — 任务完成信号

Observer 类：
```python
class Observer:
    """异步抽取 observations。每轮对话结束后调用。"""

    def __init__(self, config: dict): ...
        # 主 LLM: grok-4.20-0309-non-reasoning (config 读)
        # fallback: gemini-2.5-flash (config 读)

    async def extract(self, turn_data: dict) -> list[dict]:
        """从一轮对话中抽取 0-N 条 observation。
        
        turn_data 包含: user_text, assistant_text, tool_calls, user_emotion
        通过 function call 输出: {observations: [{priority, time, text}]}
        失败 → log warn → 返回空列表
        """

    def _build_prompt(self, turn_data: dict) -> list[dict]:
        """构建 Observer prompt。
        
        英文骨架 + 中文产出。章节覆盖：
        - YOUR JOB + FORMAT RULES
        - PRIORITY EMOJI (🔴🟡🟢✅)
        - DISTINGUISH ASSERTIONS FROM QUESTIONS（用户断言 vs 提问）
        - STATE CHANGES（新状态覆盖旧）
        - PRESERVE UNUSUAL PHRASING（保留原话特殊措辞）
        - PRECISE VERBS（动词保真，不弱化不强化）
        - DETAILS IN ASSISTANT CONTENT（保留具体数值）
        - EMOTION DETECTION（SenseVoice 情感 → 🟡）
        - AUTHORITY（USER ASSERTIONS TAKE PRECEDENCE）
        
        输出 schema: record_observations tool call
        → {observations: [{priority: "🔴"|"🟡"|"🟢"|"✅", time: "HH:MM", text: "中文陈述句"}]}
        """
```

Observer prompt 参考 Mastra OM 原版 instruction + `output in Simplified Chinese`。bench 验证过的版本效果：grok-4.20 F1=0.88, recall=0.87, halluc=5%, p50=3.4s。

**关键**：Observer 是异步冷路径，对话结束后才跑，不阻塞用户。3.4s 延迟发生在用户听完回复之后。

### 1.3 stable prefix 构建

**文件**: `memory/stable_prefix.py`（新建）

```python
class StablePrefixBuilder:
    """构建注入 LLM prompt 的 stable prefix。"""

    def __init__(self, db_path: str | Path, personality_text: str): ...

    def build(self, user_id: str, recent_turns: list[dict], current_input: str) -> str:
        """拼接完整 prompt prefix。
        
        结构（按顺序拼接）：
        1. personality 系统提示（从 core/personality.py 读）
        2. core profile（姓名/身份/硬偏好）
        3. "The following observations are your memory of past conversations..."
           "Newer observations supersede older ones. Reference specific details when relevant."
        4. <observations>
             全部 observations 按 created_at 正序
             Date 分组 + emoji 行
           </observations>
        5. --- 最近 10 turn ---
           [user] ... / [assistant] ...
        6. --- 本轮 ---
           [user] current_input

        Phase 1 全塞（无筛选），前 3 个月 <25k token。
        超标后（observations > 25k token）再按 priority 筛。
        """
```

拼 prompt < 5ms（纯字符串拼接），LLM 端 cache 命中省 90%+。

### 1.4 记忆写入流程（子能力 1 · 记）

每轮对话结束：
1. TraceLog.log_turn() 写 trace 表
2. 异步提交 Observer.extract(turn_data)
3. Observer 通过 function call 抽 0-N 条 observation
4. INSERT INTO observations + 计算 embedding（DirectAnswer 快路径要用）
5. 失败 → log warn → 跳过

### 1.5 记忆读取流程（子能力 2 · 读）

每次进 Cloud LLM 路径：
1. StablePrefixBuilder.build() 拼 prompt
2. 替代原来的 memory_manager.query() 向量检索

### 1.6 DirectAnswer 保留

DirectAnswer 快路径保留（embedding 精确匹配 → 模板回复，跳过 LLM）。但数据源从旧 memories 表切到 observations 表。observations 入库时同步算 embedding。

### 1.7 v1 记忆模块处理

| 文件 | 处理 |
|---|---|
| memory/manager.py | 大幅重写：删 GPT-4o-mini 提取逻辑，改为调 Observer + StablePrefixBuilder |
| memory/store.py | 保留（observations 表加在这里或单独文件都行），旧 memories 表保留不删 |
| memory/behavior_log.py | 保留文件不删，但 jarvis.py 改为写 trace 表 |
| memory/retriever.py | 保留（DirectAnswer 还在用） |
| memory/direct_answer.py | 改数据源从 memories → observations |
| memory/conversation.py | 保留不动 |
| memory/embedder.py | 保留不动 |
| memory/user_preferences.py | 保留不动（后续看是否合并到 observations） |

### 1.8 jarvis.py 记忆相关改动

**删除/替换**：
- L138 `self.memory_manager = MemoryManager(config)` → 重构为新的 MemoryManager（内部用 Observer + StablePrefixBuilder）
- L140-143 `self.behavior_log = BehaviorLog(mem_db)` → `self.trace_log = TraceLog(mem_db)`
- L837 `memory_context = self.memory_manager.query(text, user_id)` → `memory_context = self.memory_manager.build_stable_prefix(user_id, history, text)`
- L1087 `self.memory_manager.save(...)` → 保留原有 GPT-4o-mini 提取（DirectAnswerer 仍需旧 memories 表），同时新增 `self.memory_manager.write_observation(trace_id)` 异步写 observations 表。两者并行跑。
- L1107 `self.behavior_log.log(...)` → `self.trace_log.log_turn(...)`（写更丰富的字段）
- L1499-1520 `_setup_memory_maintenance` / `_run_memory_maintenance` → 保留框架，后续改为 reflection cron

**DirectAnswer 路径 (L847-869)** 保留逻辑不变，只改底层数据源。

### 1.9 config.yaml 新增

```yaml
memory:
  db_path: "data/memory/jarvis_memory.db"
  observer:
    primary_model: "grok-4.20-0309-non-reasoning"
    fallback_model: "gemini-2.5-flash"
    enabled: true
  stable_prefix:
    max_tokens: 25000  # 超标后按 priority 筛
```

### 1.10 Phase 1 验证标准

1. `python -m pytest tests/ -q` 全过
2. 对话后 `SELECT * FROM trace ORDER BY id DESC LIMIT 1` 能看到完整 trace 记录
3. 对话后 `SELECT * FROM observations ORDER BY id DESC LIMIT 5` 能看到 Observer 抽取的 observation
4. observation 格式正确：Date 分组 + emoji + HH:MM + 中文陈述句
5. Cloud LLM 路径收到的 prompt 包含 stable prefix（personality + observations + 最近 10 turn）
6. DirectAnswer 仍然能命中（数据源切换后）
7. behavior_log 旧表数据仍在，不丢失

---

## Phase 2 · 技能系统重建

### 2.1 @jarvis_tool 装饰器

**文件**: `tools/__init__.py`（新建目录 + 文件）

```python
import inspect
from typing import get_type_hints, Any

_TOOL_REGISTRY: dict[str, dict] = {}

def jarvis_tool(func=None, *, read_only: bool = True, destructive: bool = False, 
                required_role: str = "guest"):
    """从函数签名自动生成 tool definition，注册到全局。
    
    用法：
        @jarvis_tool
        def get_time() -> str:
            '''获取当前时间日期'''
            ...

        @jarvis_tool(destructive=True, required_role="owner")
        def set_light(room: str, color: str, brightness: int = 100) -> str:
            '''控制房间灯光'''
            ...
    """
    # 支持 @jarvis_tool 和 @jarvis_tool(...) 两种写法
    # 从 type hints 反射 parameter types
    # 从 docstring 取 description
    # 默认值 → required/optional
    # annotations (read_only/destructive) 存入 registry
    # required_role 存入 registry 供权限检查
```

约 40-50 行。

### 2.2 ToolRegistry

**文件**: `core/tool_registry.py`（新建）

```python
class ToolRegistry:
    """统一注册和分发 tool calls。
    
    启动时：
    1. 扫 tools/*.py，收集所有 @jarvis_tool 注册的函数
    2. 扫 skills/*.yaml + skills/learned/*.yaml，解析为 tool definitions
    3. 合并到统一 registry
    """

    def __init__(self, config: dict): ...

    def get_tool_definitions(self, user_role: str = "guest") -> list[dict]:
        """返回按权限过滤的 tool schema 列表（OpenAI/Grok 格式）。"""

    def execute(self, name: str, args: dict, *, user_role: str = "guest") -> str:
        """分发执行：Python function 直接调，YAML 走 interpreter。"""

    def count(self) -> int:
        """当前注册的 tool 数量。超 15 log warning。"""
```

权限检查复用现有 `_ROLE_HIERARCHY`（从 skills/__init__.py 搬过来）。

### 2.3 YAMLInterpreter

**文件**: `core/yaml_interpreter.py`（新建）

```python
from jinja2.sandbox import ImmutableSandboxedEnvironment

class YAMLInterpreter:
    """读 YAML skill → HTTP 调用 → Jinja2 渲染 → 三层错误处理。"""

    env = ImmutableSandboxedEnvironment()

    def load_skill(self, yaml_path: str) -> dict:
        """加载 YAML 文件，返回 skill 定义 dict。"""

    def to_tool_definition(self, skill: dict) -> dict:
        """把 YAML skill 转为 OpenAI tool schema。"""

    def execute(self, skill: dict, params: dict) -> str:
        """执行 YAML skill。
        
        流程：
        1. Jinja2 渲染 action.url
        2. 检查 security.allowed_domains 白名单
        3. 屏蔽私网 IP (127.0.0.0/8, 10.0.0.0/8, 192.168.0.0/16)
        4. HTTP 调用 (requests)
        5. 三层错误处理：
           层 1: retry max 3, delay 1s, exponential backoff, cap 5s
           层 2: 有 error_template → 返回人话错误文本（给 LLM 看）
           层 3: 全失败 → 返回错误字符串让路由层 fallback
        6. Jinja2 extract + compute + template 渲染
        """
```

安全：allowed_domains 白名单 + 私网 IP 屏蔽。密钥从 env var 读（`os.environ[skill.security.auth_env]`）。

### 2.4 YAML skill 文件

**文件**: `skills/weather.yaml`（新建）

从现有 WeatherSkill (skills/weather.py) 迁移。wttr.in API。
按 memory.md 第十六章的 schema：name/description/version/status/parameters/annotations/action/response/security。

**文件**: `skills/learned/exchange_rate.yaml`（新建）

从现有 skills/learned/exchange_rate.py 迁移。open.er-api.com API。

### 2.5 @jarvis_tool 函数文件

**文件**: `tools/smart_home.py`

从现有 SmartHomeSkill 迁移。函数：
- `set_light(room: str, color: str, brightness: int = 100) → str` — 调 device_manager
- `set_scene(room: str, scene_name: str) → str` — 调 device_manager
- `get_device_status(device: str) → str` — 调 device_manager

需要注入 device_manager 和 permission_manager 依赖。方式：模块级变量，jarvis.py 启动时 `tools.smart_home.init(device_manager, permission_manager)` 注入。

**文件**: `tools/time_utils.py`

从现有 TimeSkill 迁移。函数：
- `get_time() → str`
- `get_date() → str`
- `set_timer(duration: str, label: str = "") → str` — 需要 TTS 回调注入

**文件**: `tools/reminders.py`

从现有 ReminderSkill 迁移。函数：
- `add_reminder(text: str, time: str) → str` — 需要 scheduler 注入
- `list_reminders() → str`
- `delete_reminder(reminder_id: str) → str`

**文件**: `tools/todos.py`

从现有 TodoSkill 迁移。函数：
- `add_todo(text: str) → str`
- `list_todos() → str`
- `check_todo(todo_id: str) → str`

### 2.6 旧 Skill 处理

| 旧 Skill | 新形态 | 处理 |
|---|---|---|
| SmartHomeSkill | @jarvis_tool (tools/smart_home.py) | 迁移逻辑 |
| WeatherSkill | skills/weather.yaml | 重写为 YAML |
| TimeSkill | @jarvis_tool (tools/time_utils.py) | 迁移逻辑 |
| ReminderSkill | @jarvis_tool (tools/reminders.py) | 迁移逻辑 |
| TodoSkill | @jarvis_tool (tools/todos.py) | 迁移逻辑 |
| ExchangeRateSkill | skills/learned/exchange_rate.yaml | 重写为 YAML |
| ModelSwitchSkill | jarvis.py 里的规则 | "仔细想想" → 临时切 deep model |
| MemorySkill | 直接砍 | OM 替代，Observer 自动记，stable prefix 自动读 |
| AutomationSkill | 直接砍 | 以后用 YAML compose 重做 |
| SystemControlSkill | 直接砍 | RPi5 上没用 |
| HealthSkill | 直接砍 | 以后按需加 |
| RemoteControlSkill | 直接砍 | 以后重做 |
| SchedulerSkill | 直接砍 | 和 Reminder 重叠 |
| RealTimeDataSkill | 直接砍 | 以后按需加 |
| SkillManagementSkill | 直接砍 | v2 管理另起 |

### 2.7 jarvis.py 技能相关改动

**删除**：
- `from skills import SkillRegistry` (L44)
- `from skills.memory_skill import MemorySkill` (L46)
- `self.skill_registry = SkillRegistry()` (L187)
- `self._register_skills(config)` (L188) 和整个 `_register_skills()` 方法 (L1308-1386)
- `from core.learning_router import LearningRouter` (L221)
- `from core.skill_factory import SkillFactory` (L222)
- `self.learning_router = ...` (L223-225)
- `self.skill_factory = ...` (L226-229)

**新增**：
```python
from core.tool_registry import ToolRegistry

# 在 __init__ 中：
self.tool_registry = ToolRegistry(config)
# ToolRegistry 启动时自动扫描 tools/ 和 skills/，不需要手动注册
# 但需要注入依赖：
import tools.smart_home
tools.smart_home.init(self.device_manager, self.permission_manager)
import tools.time_utils
tools.time_utils.init(tts_callback=self.speak)
import tools.reminders
tools.reminders.init(scheduler=self.scheduler, tts_callback=self.speak, event_bus=self.event_bus)
```

**改动**：
- `LocalExecutor(self.skill_registry, ...)` → `LocalExecutor(self.tool_registry, ...)`
- Cloud LLM tool-use 循环：`self.skill_registry.get_tool_definitions()` → `self.tool_registry.get_tool_definitions(user_role)`
- Cloud LLM tool-use 循环：`self.skill_registry.execute(tool_name, tool_input)` → `self.tool_registry.execute(tool_name, tool_input, user_role=user_role)`
- ModelSwitch：从 SkillManagement 里拆出来，在 jarvis.py 的 `_process_input()` 开头加规则判断 "仔细想想"/"认真想" → 临时切 deep model（这段逻辑已经在 L708-720 的 escalation 部分存在，确认保留即可）

### 2.8 LocalExecutor 改动

**最小改动方案**：只把 `self.skill_registry` 引用换成 `self.tool_registry`。接口 `execute()` 签名兼容（name + args → str）。不重构 LocalExecutor 本身。

### 2.9 测试迁移

现有测试文件需要适配：
- `tests/test_skills.py` → 重写为 test_tool_registry.py
- `tests/test_behavior_log.py` → 新增 test_trace.py
- `tests/test_memory_*.py` → 适配新的 MemoryManager 接口
- `tests/test_intent_router.py` → mock 从 SkillRegistry 改为 ToolRegistry

### 2.10 文件清理

Phase 2 完成后删除：
```
skills/__init__.py            # Skill ABC + SkillRegistry
skills/smart_home.py
skills/weather.py
skills/time_skill.py
skills/reminders.py
skills/todos.py
skills/memory_skill.py
skills/automation.py
skills/system_control.py
skills/model_switch.py
skills/realtime_data.py
skills/scheduler_skill.py
skills/remote_control.py
skills/health_skill.py
skills/skill_mgmt.py
skills/learned/exchange_rate.py
skills/learned/__init__.py    # 如果只剩 .yaml 文件不需要
core/skill_factory.py
core/learning_router.py
core/skill_loader.py
```

保留 `skills/` 目录（放 YAML 文件）和 `skills/learned/` 目录。

### 2.11 Phase 2 验证标准

1. `python -m pytest tests/ -q` 全过
2. system test: "今天天气怎么样" → weather.yaml 路径执行
3. system test: "把客厅灯调成暖黄" → set_light() 执行
4. system test: "提醒我下午三点开会" → add_reminder() 执行
5. system test: "500美元多少人民币" → exchange_rate.yaml 执行
6. system test: "现在几点" → get_time() 执行
7. ToolRegistry.count() ≤ 20
8. `grep -r "SkillRegistry\|SkillFactory\|LearningRouter\|SkillLoader" --include="*.py" . | grep -v tests/ | grep -v __pycache__` 零命中
9. 旧 skills/*.py class 文件全部删除（只剩 YAML）
10. Observer 能异步抽取 observation 并落库
11. stable prefix 正确注入到 Cloud LLM prompt

---

## 集成风险 + 解法（必须遵守）

### Risk 1 · Tool Name Mismatch

LocalExecutor 硬编码了旧 tool 名字（`smart_home_control`、`get_weather`、`get_stock_watchlist`、`get_news_briefing`）。如果新 @jarvis_tool 函数改名，LocalExecutor 快路径直接挂。

**解法**：新函数保持旧名字。`smart_home_control` 不拆成 set_light/set_scene/get_device_status，保持为一个函数，内部根据参数分发。`get_weather` 的 YAML name 字段就叫 `get_weather`。这是迁移，不是重设计。拆名字是以后的事。

### Risk 2 · ToolRegistry.execute 签名

LLM 调用 tool_executor 时传 `user_id` 和 `user_role`。spec 的 execute 签名不能丢 `user_id`。

**解法**：`ToolRegistry.execute(name, args, *, user_id=None, user_role="guest") → str`。和现有 SkillRegistry.execute 签名保持一致。

### Risk 3 · DirectAnswerer 数据模型不兼容

旧 memories 表是结构化记录（category/embedding/importance/key），DirectAnswerer 的多信号评分依赖这些字段。observations 是 markdown + emoji，完全不同的数据模型。直接切数据源会挂。

**解法**：Phase 1 不动 DirectAnswerer。旧 memories 表保留，DirectAnswerer 继续读旧表。Observer 写新 observations 表，stable prefix 读新表。两套并行。v1 的 memory_manager.save()（GPT-4o-mini 提取）也暂时保留，继续往旧 memories 表写数据，确保 DirectAnswerer 有新数据可用。等 observations 积累够了再迁移 DirectAnswerer。

### Risk 4 · 被砍 Skill 仍被 LocalExecutor 引用

config 里 `realtime_data.enabled=true` 时，LocalExecutor 调 `get_stock_watchlist` / `get_news_briefing`。删了 RealTimeDataSkill 但不处理引用会静默报错。

**解法**：config.yaml 里 `realtime_data.enabled: false`。LocalExecutor 的 `execute_info_query` 里如果 tool_registry.execute 返回未知 tool 错误，返回友好文案"暂不支持该功能"而不是崩溃。

---

## 不做的事（明确排除）

- ❌ L2 tactical skill（Phase 3+）
- ❌ YAML compose / DAG 编排（Phase 4）
- ❌ 夜批 hotspot 检测 + 自动编译（Phase 3）
- ❌ shadow → live 晋升（Phase 3）
- ❌ "学会X" 手动入口（Phase 3）
- ❌ reflection（Phase 4）
- ❌ intent router 加 skill_id 字段（Phase 4）
- ❌ 跨设备 tool（SSH/截图/剪贴板）（单独阶段）
- ❌ 安全加固 / canary monitoring（Phase 4）
- ❌ 修改 personality.py（人格是核心资产，不动）

---

## 执行顺序建议

```
Step 1  → 新建 memory/trace.py (TraceLog)
          verify: 单元测试通过，能写入读取 trace

Step 2  → 新建 memory/observer.py (Observer)
          verify: 给一段对话，Observer 返回正确格式的 observations

Step 3  → observations 表建在 memory/store.py 或独立文件
          verify: 能 INSERT + SELECT observations

Step 4  → 新建 memory/stable_prefix.py (StablePrefixBuilder)
          verify: 给 observations + history，输出正确格式的 prefix string

Step 5  → 重构 memory/manager.py
          verify: .write_observation() 和 .build_stable_prefix() 工作正常

Step 6  → jarvis.py 接入新记忆系统
          verify: 对话后 trace 表和 observations 表都有数据，
                  Cloud LLM 收到 stable prefix，
                  旧 memories 表仍在写入（DirectAnswerer 依赖），
                  两套记忆写入并行不报错

Step 7  → 新建 tools/__init__.py (@jarvis_tool 装饰器)
          verify: 装饰一个测试函数，能自动生成 tool definition

Step 8  → 新建 core/yaml_interpreter.py (YAMLInterpreter)
          verify: 给 weather.yaml + params，能调 API 返回结果

Step 9  → 新建 core/tool_registry.py (ToolRegistry)
          verify: 扫描到 Python tools + YAML skills，count() 正确

Step 10 → 迁移 5 个 @jarvis_tool 函数文件
          verify: 每个函数能通过 ToolRegistry.execute() 正确调用

Step 11 → 新建 skills/weather.yaml + skills/learned/exchange_rate.yaml
          verify: 通过 YAMLInterpreter 执行正确

Step 12 → jarvis.py 接入 ToolRegistry 替换 SkillRegistry
          verify: 现有功能全部正常（天气/灯控/提醒/待办/汇率）

Step 13 → ModelSwitch 逻辑确认保留在 jarvis.py escalation 部分

Step 14 → 删除旧文件 + 清理 import
          verify: grep 零命中 + pytest 全过

Step 15 → 全量 system test
          verify: 所有验证标准通过
```
