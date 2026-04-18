# 声明式 AI Tool/Skill 定义框架调研

> 调研日期: 2026-04-15
> 目标: 找已有框架避免重造轮子 — YAML 定义 → LLM tool-use + 独立执行
> 来源: LangChain · Semantic Kernel · OpenAI GPTs · Voyager · AutoGPT · CrewAI · BabyAGI · HuggingGPT · GoS

---

## Q1. 有没有已有框架可以直接用或 fork？

### 结论: 没有现成的完整方案，但有可组合的零件

| 框架 | YAML 声明式 | 自动生成 function schema | 无 LLM 执行 | 适合 Jarvis? |
|------|:----------:|:----------------------:|:-----------:|:-----------:|
| LangChain | ❌ 必须写 Python | ✅ Pydantic → JSON Schema | ✅ `tool.invoke()` | 部分可用 |
| Semantic Kernel | ⚠️ YAML 但仅限 prompt | ✅ type hints → JSON Schema | ✅ `kernel.invoke()` | 部分可用 |
| OpenAI GPTs Actions | ✅ OpenAPI spec | ✅ OpenAI 内部转换 | ❌ 锁在 ChatGPT 里 | 思路可抄 |
| n8n 声明式路由 | ✅ JSON routing | ✅ 自动构建 HTTP 请求 | ✅ | ★ 最接近 |
| CrewAI SKILL.md | ✅ YAML frontmatter | ❌ 无 schema 生成 | ❌ 仅注入 prompt | 格式可参考 |
| MCP | ✅ JSON Schema | ✅ 就是 function schema | ✅ server 直接执行 | 可作为接口标准 |

**最接近的组合方案:**
1. **定义格式**: 借鉴 CrewAI SKILL.md (YAML frontmatter) + MCP inputSchema (JSON Schema 参数)
2. **Schema 生成**: OpenAI Cookbook 的 `openapi_to_functions()` (~30 行 Python)
3. **执行引擎**: n8n 声明式路由模式 (参数 → HTTP 请求部位映射)
4. **本地 action**: 保持现有 `skills.Skill` 基类

### LangChain — 代码必须，无 YAML 路径

三种定义方式都需要 Python 函数:

```python
# 方式 A: @tool decorator (自动从签名 + docstring 提参)
@tool(parse_docstring=True)
def check_weather(city: str, unit: str = "celsius") -> str:
    """查询天气.
    Args:
        city: 城市名.
        unit: 温度单位.
    """
    return requests.get(f".../{city}").text

# 方式 B: StructuredTool (显式 Pydantic schema)
class WeatherInput(BaseModel):
    city: str = Field(description="城市名")
    unit: str = Field(default="celsius", description="温度单位")

tool = StructuredTool.from_function(
    func=check_weather, args_schema=WeatherInput
)

# 方式 C: BaseTool 子类 (完全控制)
class WeatherTool(BaseTool):
    name = "check_weather"
    description = "查询天气"
    args_schema = WeatherInput
    def _run(self, city, unit="celsius"): ...
```

Schema 自动生成链路:
`@tool` → `inspect.signature()` + docstring 解析 → Pydantic model → `model_json_schema()` → `convert_to_openai_function()` → OpenAI function calling JSON

**关键限制**: 无法纯配置定义 tool。`_run` 必须是 Python 代码。

### Semantic Kernel — YAML 存在但只做 prompt

SK 的 YAML 函数是 **prompt 模板**，不是 action 定义:

```yaml
# SK YAML prompt function
name: GenerateStory
template: |
    Tell a story about {{$topic}} that is {{$length}} sentences long.
template_format: semantic-kernel
description: A function that generates a story about a topic.
input_variables:
    - name: topic
      description: The topic of the story.
      is_required: true
    - name: length
      description: The number of sentences in the story.
      is_required: true
execution_settings:
    default:
        temperature: 0.6
```

SK 还支持 **OpenAPI 导入** (一级支持):
```python
await kernel.add_plugin_from_openapi(
    plugin_name="weather",
    openapi_document_path="https://api.weather.com/openapi.json",
    execution_settings=OpenAPIFunctionExecutionParameters(
        auth_callback=my_auth_callback
    )
)
```

**结论**: SK 的 OpenAPI plugin 路径可用，但 YAML prompt function 不适合 Jarvis 的 action 需求。

### OpenAPI → Function Schema 转换 (~30 行核心)

OpenAI Cookbook 的参考实现:

```python
import jsonref

def openapi_to_functions(openapi_spec):
    functions = []
    for path, methods in openapi_spec["paths"].items():
        for method, spec_with_ref in methods.items():
            spec = jsonref.replace_refs(spec_with_ref)
            function_name = spec.get("operationId")
            desc = spec.get("description") or spec.get("summary", "")
            schema = {"type": "object", "properties": {}}

            req_body = (spec.get("requestBody", {})
                .get("content", {}).get("application/json", {}).get("schema"))
            if req_body:
                schema["properties"]["requestBody"] = req_body

            params = spec.get("parameters", [])
            if params:
                param_properties = {
                    p["name"]: p["schema"] for p in params if "schema" in p
                }
                schema["properties"]["parameters"] = {
                    "type": "object", "properties": param_properties
                }

            functions.append({
                "type": "function",
                "function": {"name": function_name, "description": desc, "parameters": schema}
            })
    return functions
```

字段映射: `operationId` → name, `summary` → description, `parameters[]` + `requestBody.schema` → parameters

---

## Q2. OpenAI GPTs Actions 的 OpenAPI Spec 做法

### 完整真实例子 (NWS Weather API)

```yaml
openapi: 3.1.0
info:
  title: NWS Weather API
  description: Access to weather data including forecasts, alerts, and observations.
  version: 1.0.0
servers:
  - url: https://api.weather.gov
    description: Main API Server
paths:
  /points/{latitude},{longitude}:
    get:
      operationId: getPointData
      summary: Get forecast grid endpoints for a specific location
      parameters:
        - name: latitude
          in: path
          required: true
          schema:
            type: number
            format: float
          description: Latitude of the point
        - name: longitude
          in: path
          required: true
          schema:
            type: number
            format: float
          description: Longitude of the point
      responses:
        '200':
          description: Successfully retrieved grid endpoints
          content:
            application/json:
              schema:
                type: object
                properties:
                  properties:
                    type: object
                    properties:
                      forecast:
                        type: string
                        format: uri
```

### 转换过程

1. 用户在 GPT Editor 的 Actions 面板粘贴 OpenAPI YAML/JSON
2. OpenAI 解析 spec，每个 `operationId` 变成一个 callable action
3. `summary`/`description` → tool 描述 (LLM 用来决定何时调用)
4. `parameters[]` + `requestBody.schema` → tool 参数 (LLM 生成参数值)
5. GPT 运行时: LLM 输出参数 → OpenAI 后端执行 HTTP 请求 → 结果返回 LLM

### 认证 (与 spec 分离)

| 类型 | 配置位置 | 细节 |
|------|---------|------|
| None | GPT Editor | 公开 API |
| API Key | GPT Editor (加密存储) | 作为 header 发送 |
| OAuth | GPT Editor | Client ID/Secret + Auth URL + Token URL + Scope |

**关键设计**: 认证信息不在 OpenAPI spec 里，而是在 GPT Editor 单独配置。spec 里的 `securitySchemes` 只告诉系统哪些端点需要 auth。

### 适不适合 Jarvis?

**适合的部分:**
- "OpenAPI spec → function schema" 的思路直接可用
- 认证与 skill 定义分离的模式很好 (对应 Jarvis 的 `config.yaml` 存 API key)

**不适合的部分:**
- GPTs Actions 只处理 HTTP API 调用，Jarvis 还需要本地 action (Hue 灯、SQLite 查询、TTS 控制)
- GPTs 的执行在 OpenAI 云端，Jarvis 需要本地执行
- 语音助手的参数来源更复杂 (ASR 文本 → 参数提取 vs 聊天界面的结构化输入)

---

## Q3. 各框架的 Tool 参数提取方式

### 核心发现: 所有框架都依赖 LLM 提参，无纯规则成功案例

| 框架 | 参数提取方式 | 细节 |
|------|------------|------|
| **LangChain** | LLM function calling | 把 tool schema 传给 LLM，LLM 输出 JSON 参数 |
| **Semantic Kernel** | LLM function calling | 同上，用 `Annotated` type hints 增强描述 |
| **OpenAI GPTs** | LLM function calling | operationId + parameters schema → LLM 生成 args |
| **Voyager** | LLM 生成完整代码 | 不提取参数，直接让 GPT-4 写调用代码 |
| **AutoGPT** | 用户手动连接 (DAG) | 人在 UI 里手动接线 |
| **HuggingGPT** | LLM task parsing | GPT-4 把自然语言 → 结构化 task JSON (few-shot) |

### LangChain 的具体链路

```
用户输入 "北京今天天气"
  → ChatModel.bind_tools([weather_tool])
  → LLM 看到 function schema: {name: "check_weather", parameters: {city: string}}
  → LLM 输出: {"name": "check_weather", "arguments": {"city": "北京"}}
  → LangChain 解析 → tool.invoke({"city": "北京"})
```

内部用 `convert_to_openai_function(tool)` 把 Pydantic schema 转成 LLM 可读格式。

### "不用 LLM 纯规则提参"的案例?

**未找到生产级成功案例。** 但 Jarvis 已有的 intent router + slot filling 其实就是轻量版:

- **Groq Llama-3.3-70B intent router**: 识别 intent (轻量 LLM，不是纯规则但很快)
- **Rasa NLU (开源)**: 基于 regex + CRF 的 slot filling，但准确率低于 LLM
- **Jarvis DirectAnswer**: 从 memory 直接回答，完全跳过 LLM

**实际建议**: 热路径 (farewell/direct answer) 用规则，其余走 LLM function calling。这已经是 Jarvis 的架构。

---

## Q4. Voyager 的 Skill 存储格式

### Skill 是 JS 函数，不是结构化描述

```javascript
// 存储格式: 独立 .js 文件
async function mineWoodLog(bot) {
    let axe = bot.inventory.findInventoryItem(mcData.itemsByName["wooden_axe"].id);
    // ... 完整实现 ...
}
```

### 元数据 (`skills.json`)

```json
{
  "mineWoodLog": {
    "code": "async function mineWoodLog(bot) { ... }",
    "description": "The function mines wood logs by first checking for an axe..."
  }
}
```

### 存储结构

```
ckpt_dir/skill/
  code/              # .js 文件 — 每 skill 一个
    craftPlanks.js
    mineWoodLog.js
  description/       # .txt 文件 — LLM 生成的自然语言描述
    craftPlanks.txt
    mineWoodLog.txt
  skills.json        # 主索引: {name: {code, description}}
  vectordb/          # ChromaDB 持久化目录
```

### 索引 + 检索

**向量嵌入 + ChromaDB:**
- 嵌入的是 **LLM 生成的自然语言描述**，不是代码本身
- 嵌入模型: OpenAI `text-embedding-ada-002`
- 检索: `similarity_search_with_score(query, k=5)` → top-5 skill 代码注入 prompt

```python
def retrieve_skills(self, query):
    k = min(self.vectordb._collection.count(), self.retrieval_top_k)
    if k == 0:
        return []
    docs_and_scores = self.vectordb.similarity_search_with_score(query, k=k)
    return [self.skills[doc.metadata["name"]]["code"] for doc, _ in docs_and_scores]
```

**关键设计**: 描述嵌入是语义桥梁。代码不直接嵌入 — LLM 先把代码翻译成自然语言，然后嵌入自然语言。检索空间和实现空间解耦。

### 复用 vs 新建决策

**没有显式决策逻辑。** 系统总是检索 top-5 相似 skill 作为 context 注入 prompt，由 GPT-4 自行决定:
- 直接调用已有 skill
- 基于已有 skill 修改
- 从零写新 skill

Prompt 指示: "Reuse provided utility functions."

### 验证才存储

Voyager 的 3-agent 验证循环:
1. **ActionAgent** (GPT-4) 生成 JS 代码
2. 在 Minecraft 环境中执行
3. **CriticAgent** (独立 LLM 调用) 检查成功/失败
4. 失败 → critique 反馈 → 重试 (max retries)
5. **只有成功才保存** skill

### 对 Jarvis 的启发

Jarvis 已有 memory 系统 (SQLite + FastEmbed bge-small-zh-v1.5)，可复用:
- Skill description 嵌入到现有向量库
- 检索 query = 用户 ASR 文本 + intent
- Top-k skill 注入 LLM prompt 的 tools 列表

---

## Q5. 跨框架 Tool Schema 共性字段

### 8 个框架的字段横向对比

| 字段 | LangChain | SK | OpenAI | MCP | HA | n8n | CrewAI | Voyager |
|------|:---------:|:--:|:------:|:---:|:--:|:---:|:------:|:-------:|
| **name** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **description** | ✅ | ✅ | ✅ | ✅ (可选) | ❌ | ✅ | ✅ | ✅ |
| **parameters** | ✅ Pydantic | ✅ type hints | ✅ JSON Schema | ✅ JSON Schema | ✅ selectors | ✅ routing | ✅ Pydantic | ❌ |
| parameters.type | ✅ | ✅ | ✅ | ✅ | ✅ (40 types) | ✅ | ✅ | ❌ |
| parameters.required | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| parameters.description | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ |
| parameters.default | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| parameters.enum | ✅ | ✅ | ✅ | ✅ | ✅ (select) | ✅ | ✅ | ❌ |
| **return_type** | ✅ | ✅ | ❌ | ✅ outputSchema | ❌ | ✅ | ❌ | ❌ |
| **error_handling** | ❌ | ❌ | ❌ | ✅ isError | ✅ exceptions | ✅ onError | ❌ | ✅ critic |
| **auth/credentials** | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| **retry** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | ✅ loop |
| **annotations/hints** | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **categories/tags** | ❌ | ✅ | ❌ | ❌ | ✅ domain | ✅ type | ✅ | ❌ |

### 所有框架都有的 (100% 覆盖)

1. **`name`** — 唯一标识符
2. **`description`** — 自然语言描述 (LLM 用来决定何时调用)

### 几乎所有框架都有的 (87%+)

3. **`parameters`** — 参数定义 (JSON Schema / Pydantic / type hints)
4. **`parameters.type`** — 参数类型
5. **`parameters.required`** — 是否必填
6. **`parameters.description`** — 参数描述
7. **`parameters.default`** — 默认值

### 有价值的独有/少见字段

| 字段 | 来源 | 价值 |
|------|------|------|
| `annotations.readOnlyHint` / `destructiveHint` | MCP | **★★★** 权限分级 — 只读 skill 不需要确认，破坏性 skill 需要 |
| `selector` (40 种 UI 类型) | HA | **★★** 验证+UI 三合一，但 Jarvis 无 UI |
| `routing.send` (参数 → HTTP 请求映射) | n8n | **★★★** 声明式 API 包装的关键 |
| `on_error` (stop/continue/error-branch) | n8n | **★★★** 多步 skill 必需 |
| `allowed_tools` | CrewAI SKILL.md | **★** skill 嵌套调用白名单 |
| `example_tasks` | GoS (2026) | **★★** 提升检索精度 |
| `one_line_capability` | GoS (2026) | **★★** 比 description 更适合嵌入 |
| `test_input` / `test_output` | AutoGPT | **★★** 内置测试用例 |
| `supports_response` | HA | **★** 标记是否有返回值 |

---

## 附: 新发现 — Graph of Skills (GoS, 2026-04)

来自 UPenn/Maryland/Brown 的最新研究，直接针对 Voyager 平面嵌入检索的不足:

**核心创新**: 在 skill 之间建立 **有类型的依赖图** (dependency / workflow / semantic / alternative edges)，用 **Personalized PageRank** 替代纯 embedding retrieval。

**结果**: vs 全量加载 +43.6% reward, -37.8% tokens。在 Claude Sonnet / GPT-5.2 / MiniMax 上测试。

**对 Jarvis 的意义**: 当 skill 库超过 ~50 个时，纯 embedding 检索会漏掉前置依赖 skill。GoS 的 dependency edge 确保"部署 ML 模型"时也拉出"序列化模型"和"配置端点"。

---

## 置信度评估

| 来源 | 置信度 | 说明 |
|------|--------|------|
| LangChain | ★★★★★ | 直接读 langchain_core 源码 + 官方文档 |
| Semantic Kernel | ★★★★★ | 直接读 GitHub 源码 + MS Learn 文档 |
| OpenAI GPTs Actions | ★★★★★ | 官方文档 + Cookbook 参考实现 |
| Voyager | ★★★★★ | 直接读 `voyager/agents/skill.py` 源码 |
| AutoGPT | ★★★★☆ | 读了 Block 基类和示例，但平台变化快 |
| CrewAI SKILL.md | ★★★★☆ | 较新功能，文档还在补全中 |
| BabyAGI | ★★★★☆ | 读了 functionz 框架源码 |
| HuggingGPT | ★★★★★ | 直接读 `server/` 源码 |
| GoS | ★★★★☆ | arXiv 论文 (2026-04)，尚无开源实现 |

---

## 综合结论: Jarvis 不需要 fork 现有框架

**原因:**
1. 没有框架同时满足 "YAML 声明 + function schema 生成 + 本地执行 + HTTP 执行"
2. 核心转换逻辑 (YAML → JSON Schema → OpenAI function) 只需 ~50 行 Python
3. Jarvis 已有 `skills.Skill` 基类 + `permission_manager` + memory 向量库，自建 YAML 解释器更轻

**推荐路径:**
1. 定义 Jarvis YAML skill schema (综合 MCP + HA + n8n 的最佳字段)
2. 写一个 `YAMLSkillLoader` — 读 YAML → 生成 Pydantic model → 输出 function calling JSON
3. 对 HTTP 类 skill: 借鉴 n8n 声明式路由，YAML 里写 url/method/query/headers
4. 对本地 action 类 skill: 保持现有 Python `Skill` 子类
5. Skill 检索: 复用现有 FastEmbed + SQLite，嵌入 skill description
