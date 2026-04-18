# YAML Skill 解释器 — 真实生产实现参考

> 调研日期: 2026-04-15
> 目标: 为 Jarvis 语音助手的 YAML skill 框架找到可复用的设计模式
> 来源: Home Assistant · n8n · MCP · Cloudflare Code Mode

---

## Q1. Skill/Action/Tool 定义 Schema 对比

### Home Assistant — `services.yaml`

最成熟的声明式 skill 系统。每个 integration 暴露一个 `services.yaml`。

**必有字段:**
| 字段 | 类型 | 说明 |
|------|------|------|
| `{service_name}` | dict key | 服务标识符 (如 `turn_on`) |
| `fields` | dict | 参数定义 |

**可选字段:**
| 字段 | 类型 | 说明 |
|------|------|------|
| `target` | dict | 实体目标 (entity/device/area/floor/label) |
| `fields.{name}.required` | bool | 是否必填 |
| `fields.{name}.example` | any | 示例值 |
| `fields.{name}.default` | any | 默认值 |
| `fields.{name}.advanced` | bool | 高级参数（UI 折叠） |
| `fields.{name}.filter` | dict | 条件显示（按 feature/attribute） |
| `fields.{name}.selector` | dict | **类型+验证+UI 三合一** |

**selector 类型系统** — 40+ 种，兼顾验证和 UI 渲染:
`number`, `text`, `boolean`, `entity`, `device`, `area`, `select`, `color_rgb`,
`color_temp`, `duration`, `template`, `target`, `action`, `condition`, `trigger`,
`state`, `object`, `location`, `file`, `icon`, `date`, `datetime`, `time` ...

```yaml
# 示例: light.turn_on
turn_on:
  target:
    entity:
      domain: light
  fields:
    brightness:
      required: false
      example: "120"
      filter:
        supported_features:
          - light.LightEntityFeature.SUPPORT_BRIGHTNESS
      selector:
        number:
          min: 0
          max: 255
          step: 1
          mode: slider
```

**参数验证**: Voluptuous schema，在 handler 执行前验证。

---

### n8n — Workflow JSON

**Node 定义** (必有字段):
| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 唯一 ID |
| `name` | string | 唯一名称（也是连接 key） |
| `type` | string | 节点类型 (如 `n8n-nodes-base.httpRequest`) |
| `typeVersion` | number | 版本 |
| `position` | [x, y] | UI 位置 |
| `parameters` | dict | 配置参数 |

**可选字段:**
| 字段 | 类型 | 说明 |
|------|------|------|
| `disabled` | bool | 禁用 |
| `retryOnFail` | bool | 失败重试 |
| `maxTries` | number | 最大重试 (上限 5) |
| `waitBetweenTries` | number | 重试间隔 ms (上限 5000) |
| `onError` | enum | `stopWorkflow` / `continueRegularOutput` / `continueErrorOutput` |
| `credentials` | dict | 引用的凭证 |
| `executeOnce` | bool | 只处理第一个 item |

**连接 (Connections)** — 独立于节点定义:
```json
{
  "source_node_name": {
    "main": [[{ "node": "target_name", "index": 0, "type": "main" }]]
  }
}
```

**声明式路由** — n8n 的 killer feature，无需写代码:
```typescript
{
  name: 'queryParam',
  type: 'string',
  routing: {
    send: { type: 'query', property: 'q' },   // 放进 query string
    request: { method: 'GET', url: '/search' }  // 请求目标
  }
}
```

---

### MCP — Tool Definition

**必有字段:**
| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 工具标识符 |
| `inputSchema` | JSON Schema | 参数定义，root 必须 `type: "object"` |

**可选字段:**
| 字段 | 类型 | 说明 |
|------|------|------|
| `description` | string | LLM 可读描述 |
| `title` | string | 人类可读标题 |
| `outputSchema` | JSON Schema | 结构化返回类型 |
| `annotations` | dict | 行为提示 |
| `annotations.readOnlyHint` | bool | 只读操作 (default: false) |
| `annotations.destructiveHint` | bool | 破坏性操作 (default: true) |
| `annotations.idempotentHint` | bool | 幂等 (default: false) |
| `annotations.openWorldHint` | bool | 访问外部系统 (default: true) |

```json
{
  "name": "fetch",
  "description": "Fetch a URL",
  "inputSchema": {
    "type": "object",
    "properties": {
      "url": { "type": "string", "description": "URL to fetch" },
      "max_length": { "type": "integer", "default": 5000 }
    },
    "required": ["url"]
  }
}
```

**参数验证**: Python 用 Pydantic, TypeScript 用 Zod，在 handler 入口验证。

---

### Cloudflare Code Mode — 元 MCP

不是传统 tool 定义，而是用 **2 个 tool 替代 N 个 tool**:

| Tool | 输入 | 说明 |
|------|------|------|
| `search` | `code: string` | LLM 写 JS 查询 OpenAPI spec |
| `execute` | `code: string` | LLM 写 JS 调用 API |

Token 缩减: 2500 endpoints → 1.17M tokens 的 schema → ~1K tokens 的类型声明。

**实测数据** (WorkOS 独立基准):
- 简单任务: 32% fewer tokens
- 复杂批量: 81% fewer tokens
- 标题的 99.9% 只是初始 prompt 大小差异

---

### 横向对比

| 维度 | Home Assistant | n8n | MCP | Cloudflare Code Mode |
|------|---------------|-----|-----|---------------------|
| 必有字段 | name + fields | name + type + params | name + inputSchema | name + code |
| 类型系统 | 40 selector 类型 | routing 声明式 | JSON Schema | TypeScript 类型声明 |
| 参数验证 | Voluptuous (前置) | 运行时 | Pydantic/Zod (入口) | V8 运行时类型检查 |
| 定义格式 | YAML | JSON | JSON (Schema) | TS 类型 + 自由代码 |
| **Jarvis 适用度** | ★★★ 最直接可抄 | ★★★ 声明式路由最好 | ★★ 太 generic | ★ 我们没 2500 个 tool |

---

## Q2. 表达式语言

### 语法对比

| 系统 | 语法 | 示例 |
|------|------|------|
| Home Assistant | Jinja2 `{{ }}` | `{{ states('sensor.temp') }}` |
| n8n | `=` 前缀 + JS | `={{ $json.name.toUpperCase() }}` |
| MCP | 无 (参数是 JSON 值) | N/A |
| Cloudflare | 原生 JavaScript | `spec.paths['/zones'].get` |

### 变量引用

**Home Assistant** — 注入 Jinja2 全局变量:
```jinja
{{ states('light.kitchen') }}           → 实体状态
{{ state_attr('light.kitchen', 'brightness') }}  → 实体属性
{{ is_state('binary_sensor.door', 'on') }}       → 布尔判断
{{ expand('group.lights') }}            → 展开组
{{ closest(states.device_tracker) }}    → 最近实体
{{ distance('home', 'work') }}          → 地理距离
```

**n8n** — `$` 前缀变量 (通过 JS Proxy 实现):
```javascript
$json.email                             // 当前 item 的字段
$input.first().json.data                // 当前节点输入
$('HTTP Request').first().json.data     // 任意上游节点输出
$env.API_KEY                            // 环境变量
$workflow.name                          // 工作流元数据
$now.toISO()                            // 当前时间 (Luxon)
$jmesPath($json, 'items[?active]')     // JMESPath 查询
$vars.apiBaseUrl                        // 用户自定义变量
$secrets.provider.key                   // 外部密钥
```

### 算术支持

| 系统 | 支持 | 示例 |
|------|------|------|
| HA | 是 (Jinja2 原生) | `{{ states('sensor.price') \| float * 1.1 }}` |
| n8n | 是 (原生 JS) | `={{ $json.amount * $json.rate }}` |
| MCP | 否 | — |

### 引用前一步输出

| 系统 | 机制 |
|------|------|
| HA | `trigger.to_state` / `action.response_variable` |
| n8n | `$('NodeName').first().json.xxx` + paired item 数据溯源 |
| MCP | 无编排层，单次调用 |

### Jarvis 推荐

**Python Jinja2 子集** — 与 HA 一致:
- `{{ var }}` 变量替换
- `{{ var \| filter }}` 管道过滤
- 注入 `context` / `memory` / `devices` / `prev_step` 作为全局变量
- 用 `SandboxedEnvironment` 限制

---

## Q3. 错误处理

### API 调用失败

| 系统 | 策略 |
|------|------|
| **HA** | **不自动重试**。blocking 调用抛异常到调用方；non-blocking 记日志吞异常；automation 失败创建 UI repair issue |
| **n8n** | **可配置重试**: `retryOnFail: true`, `maxTries` (上限5), `waitBetweenTries` (上限5000ms) |
| **MCP** | **两层错误模型**: tool 级 (`isError: true` + 文本描述，LLM 可见可自纠) vs protocol 级 (JSON-RPC error，LLM 不可见) |

### 超时策略

| 系统 | 机制 |
|------|------|
| HA | `CONF_CONTINUE_ON_TIMEOUT` — 二选一: 中止 or 继续 |
| n8n | workflow 全局超时 + `AbortController` 取消所有进行中节点 |
| MCP | 无协议级超时，由 server 实现决定 |
| Cloudflare | V8 isolate 30s 硬超时 |

### 认证/密钥管理

| 系统 | 机制 |
|------|------|
| HA | `AuthManager` — JWT HS256, refresh token, MFA, 权限策略编译为 callable |
| n8n | **AES-256-CBC 加密存储**, 运行时解密注入。Credential 按节点类型限制，HTTP Request 节点有 `fullAccess` |
| MCP | STDIO: env var; HTTP: OAuth 2.1 + PRM discovery + 动态客户端注册 |

### n8n 节点级错误处理模式 (最可复用)

```yaml
# 三种 onError 策略
onError: stopWorkflow          # 默认: 停止整个流程
onError: continueRegularOutput # 失败时传递输入数据，假装成功
onError: continueErrorOutput   # 失败 item 路由到专用错误输出口
```

### Jarvis 推荐

```yaml
# 每个 YAML skill step 可配置:
steps:
  - name: fetch_weather
    action: http_get
    url: "https://api.weather.com/..."
    on_error: retry          # retry (默认) | skip | abort | fallback
    max_retries: 3
    retry_delay_ms: 1000
    fallback: "天气服务暂时不可用"
    timeout_ms: 5000
```

MCP 的 `isError` + 文本描述模式也值得借鉴 — 错误信息返回给 LLM 让它自纠。

---

## Q4. 安全沙箱

### 声明式 skill 的能力边界

| 系统 | 边界 | 实现 |
|------|------|------|
| **HA** | Jinja2 `ImmutableSandboxedEnvironment` — 禁止 `__` 属性访问; limited mode 禁止所有状态函数; 输出上限 256KB; 源码上限 5MB | Python Jinja2 内置沙箱 |
| **n8n** | 表达式在 VM sandbox 执行; `$env` 通过 `EnvProviderState` 控制可见变量 | Node.js `vm` 模块 |
| **MCP** | 协议本身不强制沙箱; 建议: STDIO 命令沙箱、危险模式警告、per-client 同意存储 | 依赖 client 实现 |
| **Cloudflare** | **V8 isolate** — 无网络访问(fetch/connect 抛异常); 无 env/文件系统; API token 在 server 侧注入; 30s 超时; console 捕获 | Cloudflare Workers RPC |

### SSRF 防护

| 系统 | 机制 |
|------|------|
| HA | 无显式 SSRF 防护（本地运行，信任网络） |
| n8n | 无显式 URL 限制 |
| MCP | 规范建议 "block private IP ranges" |
| Cloudflare | `globalOutbound: null` 阻断所有网络访问，仅允许 `cloudflare.request()` 代理 |

### URL 白名单

**没有找到任何系统实现了声明式 URL 白名单。** HA 和 n8n 信任本地网络；MCP 只是建议；Cloudflare 走代理模式从根本上绕过了问题。

### Jarvis 推荐

```yaml
# config.yaml 中的 skill 安全策略
skill_security:
  allowed_domains:           # URL 白名单
    - "api.weather.com"
    - "api.xai.com"
    - "*.philips-hue.com"
  blocked_ip_ranges:         # SSRF 防护
    - "10.0.0.0/8"
    - "172.16.0.0/12"
    - "192.168.0.0/16"       # 除非 hue bridge 明确例外
    - "127.0.0.0/8"
  max_response_size: 256KB
  timeout_ms: 10000
  template_sandbox: true     # Jinja2 ImmutableSandboxedEnvironment
```

---

## Q5. 组合/编排 (Compose)

### 多步 Skill 的 DAG 定义

**Home Assistant** — 线性 action 列表 + 条件分支:
```yaml
automation:
  trigger: ...
  action:
    - service: light.turn_on
      target: { entity_id: light.kitchen }
    - delay: "00:00:05"
    - if:
        - condition: state
          entity_id: binary_sensor.motion
          state: "on"
      then:
        - service: light.turn_on
          target: { entity_id: light.hallway }
    - parallel:              # 并行执行
        - service: notify.phone
          data: { message: "Lights on" }
        - service: scene.turn_on
          target: { entity_id: scene.evening }
```

执行模式: `single` | `parallel` | `queued` | `restart`

**n8n** — 扁平节点 + 独立连接图:
```json
{
  "nodes": [
    { "name": "Trigger", "type": "n8n-nodes-base.webhook", ... },
    { "name": "Fetch", "type": "n8n-nodes-base.httpRequest", ... },
    { "name": "Format", "type": "n8n-nodes-base.set", ... }
  ],
  "connections": {
    "Trigger": { "main": [[{ "node": "Fetch", "index": 0 }]] },
    "Fetch": { "main": [[{ "node": "Format", "index": 0 }]] }
  }
}
```

执行引擎: FIFO 栈，节点完成后 push 下游节点。多输入节点等所有输入到齐才执行。

**MCP** — 无编排层，单次 tool 调用，由 LLM 在对话循环中组合。

### 步骤间变量传递

| 系统 | 机制 |
|------|------|
| HA | `response_variable` 存 action 返回值; trigger 数据自动注入 `trigger.*` |
| n8n | `$('NodeName').first().json.*` + paired item 自动溯源 |
| MCP | N/A (LLM 管上下文) |

### 条件分支

| 系统 | 语法 |
|------|------|
| HA | `if/then/else`, `choose` (多条件分支), `condition` (单条件守卫) |
| n8n | IF 节点 (true/false 两个输出口), Switch 节点 (多路) |

### 错误传播

| 系统 | 机制 |
|------|------|
| HA | `_HaltScript` 异常族: `_AbortScript` (意外) / `_ConditionFail` (条件假) / `_StopScript` (显式停止+返回值) |
| n8n | `onError` 三策略 + error output 端口 + workflow 级 AbortController |

### 并行执行

| 系统 | 机制 |
|------|------|
| HA | `parallel:` action 类型 → `asyncio.gather()`. `ScriptRunVariables` 用 `non_parallel_scope` 防竞态 |
| n8n | 多输出口连接到不同节点 = 隐式并行。`Promise.allSettled()` |

### Jarvis 推荐 — 简化版 DAG

```yaml
name: morning_routine
description: "早安流程"
steps:
  - name: get_weather
    action: http_get
    url: "https://api.weather.com/current?city={{ config.city }}"
    timeout_ms: 5000

  - name: get_calendar
    action: http_get
    url: "https://api.google.com/calendar/today"
    depends_on: []            # 空依赖 = 可与 get_weather 并行

  - name: compose_greeting
    action: llm_generate
    depends_on: [get_weather, get_calendar]
    prompt: |
      天气: {{ steps.get_weather.response.temp }}°C
      日程: {{ steps.get_calendar.response.events | join(', ') }}
      生成一句早安问候

  - name: speak
    action: tts
    depends_on: [compose_greeting]
    text: "{{ steps.compose_greeting.response }}"

on_error:
  default: skip_and_continue
  notify_user: true
```

---

## 置信度评估

| 来源 | 置信度 | 说明 |
|------|--------|------|
| Home Assistant | ★★★★★ | 直接阅读 core repo 源码，services.yaml/core.py/template.py/auth/ |
| n8n | ★★★★★ | 直接阅读 workflow/src/*.ts 接口定义和执行引擎 |
| MCP | ★★★★★ | 直接阅读官方 spec schema + SDK 源码 + 多个 server 实现 |
| Cloudflare Code Mode | ★★★★★ | 直接阅读 cloudflare/mcp 和 cloudflare/agents 源码 + WorkOS 独立基准 |

所有结论均来自源码和官方文档，未使用营销材料或二手描述。

---

## 对 Jarvis 的综合建议

### 1. Schema 设计: 借鉴 HA selector + n8n routing

```yaml
name: check_weather
description: "查询天气"
parameters:
  city:
    type: text                # HA-style selector type
    required: true
    description: "城市名"
  unit:
    type: select
    options: ["celsius", "fahrenheit"]
    default: "celsius"
action:
  type: http_get
  url: "https://api.weather.com/v1/current"
  query:                      # n8n-style routing
    q: "{{ parameters.city }}"
    units: "{{ parameters.unit }}"
  headers:
    Authorization: "Bearer {{ secrets.weather_api_key }}"
response:
  extract: "{{ response.json.main.temp }}"
  template: "{{ parameters.city }}现在{{ extracted }}度"
```

### 2. 表达式: Python Jinja2 SandboxedEnvironment

与 HA 一致，成熟可靠。注入变量:
- `parameters.*` — 用户输入
- `steps.*` — 前序步骤输出
- `config.*` — config.yaml 值
- `secrets.*` — 密钥 (运行时从 env 注入)
- `memory.*` — 记忆查询结果
- `devices.*` — 设备状态

### 3. 错误处理: n8n 三策略 + MCP isError 模式

- 每步可配 `on_error`: retry / skip / abort / fallback
- 错误信息以文本返回 LLM，让它自纠 (MCP pattern)
- 不自动重试的 HA 模式太简陋，n8n 的上限约束 (max 5 tries, 5s delay) 很合理

### 4. 安全: HA sandbox + URL 白名单

- Jinja2 `ImmutableSandboxedEnvironment`
- `config.yaml` 里配 `allowed_domains` 白名单
- 屏蔽私网 IP
- 所有密钥通过 `secrets.*` 注入，YAML 里不出现明文

### 5. 编排: 简化 DAG

- `depends_on` 声明依赖 → 自动并行无依赖步骤
- `if/then/else` 条件分支
- `steps.{name}.response` 引用前序输出
- 执行引擎: 拓扑排序 → asyncio.gather 并行组 → 顺序执行有依赖步
