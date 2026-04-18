# SkillFactory 重设计调研报告

*Date: 2026-04-13 · Sources: 30+ · Confidence: High*

## Executive Summary

三条主线并行调研（Voyager 范式 / 沙箱 & 验证 / 生产框架 & Anthropic 原生）汇聚到一个非常意外的核心建议：**把生成产物从"Python `Skill` 类"切换到"Claude Code Skill（`SKILL.md` + `handler.py` 目录）"**。原因是你底层已经在用 Claude Code CLI 跑 subprocess，产物直接做成 CC Skill 就能免费得到热重载、progressive disclosure（省 RPi 内存）、frontmatter 权限声明、并且生成的技能能直接用 `/skill-name` 在 CC 里调试。

辅助三条建议：Voyager 的 **description-embedding 检索 + critic 评分 + "make it reusable" prompt 约束**；沙箱用 **bwrap（运行时）+ E2B（生成时验证）** 两层，而非单纯 regex 扫描；import 白名单 + bandit 代替现有的 15 条 regex。

完整 7 条合成建议在最后一节。

---

## Part 1: Voyager 范式 — 自增长 skill library

### Voyager (NVIDIA, 2023) — 范式开创者

- **存储**：每个 skill 是一个 JS 函数，key 是**自然语言描述的 embedding**（`text-embedding-ada-002`），value 是代码。向量数据库（paper Sec 2.2）。
- **检索**：top-k 按 `task + 当前环境状态` 双向嵌入查询。关键洞察：同一个 "挖铁矿" 在沙漠 vs 森林应该拉回不同的 skill。
- **组合**：prompt 明确要求 *"make it generic and reusable"*，检索到的 top-5 skills 作为 callable helpers 注入 prompt。早期 skill 成为后期 skill 的 primitive。
- **自验证**：**单独一个 GPT-4 作为 Critic**，输入 `{task, agent_state}` → JSON `{success: bool, critique: str}`。消融实验：去掉 critic，探索覆盖率掉 73%。
- **冷启动**：前 15 个 task 关闭 retrieval，强制多样性。
- **可移植性**：把 Voyager 的 skill library 插到 AutoGPT，novel-task 成功率从 0/12 → 4/12。Library 本身是可迁移的。

### OS-Copilot / FRIDAY (ICLR 2024) — 第一个 Voyager → OS 任务的移植

- **关键改进**：Critic 不仅判断 success/fail，还给 0-10 **通用性分数**；只有 ≥8 的 skill 进入 repository。避免了"一次性 skill" 塞满 library 然后 embedding 检索分不清。
- **Skill 结构**：Python class 继承 `BaseAction`，有 `_description` 属性 + `__call__` 方法 —— **和你的 `Skill` ABC 几乎一样**。
- **生成入口**：embedding 相似度低于阈值 → Tool Generator 写新的；高于阈值 → 复用。
- 种子 4 个 skill，GAIA dev set 上自己学出 9 个，Level-1 达到 40.86%（相对 SOTA +35%）。

### 其他

- **MetaGPT** 是手工 curated library（不自生长）：`@register_tool()` + docstring + tags + LLM 选择。作为 baseline。
- **AutoGPT** 完全无 skill 持久化 —— 每次任务都从零开始，这也是 Voyager 论文用它做消融对照的原因。

### 可迁移到 Jarvis 的精华

1. **Description embedding 作 key**（不是 name）。你已经在跑 `bge-small-zh-v1.5`，直接复用。
2. **Retrieval query 包含当前上下文**（房间、时间、最近对话），不只是 utterance。
3. **Critic 评分 + 通用性过滤**（FRIDAY 的 ≥8 门槛）。你已经有 Groq Llama-3.3-70B，作 critic 几乎免费。
4. **冷启动期关闭 retrieval**。你现有 15+ 个 hand-written skills 就是种子。
5. **Prompt 硬约束 "make it generic and reusable"**（Voyager 原话）。

---

## Part 2: 沙箱 + 验证

### 云沙箱（生成时验证用）

| 提供商 | 冷启动 | $/hr | 栈 | Jarvis 适配度 |
|---|---|---|---|---|
| **E2B** | 150ms | $0.083 | Firecracker microVM | ★★★★★ 可自托管，Python SDK 一流 |
| Modal | <1s | $0.119 | 自研 gVisor-like | ML-heavy，过重 |
| Daytona | 90ms | $0.083 | OCI/Docker + 快照 | 开发环境导向 |
| Blaxel | 25ms | $0.083 | 预热快照 | 最快恢复，较新 |

**推荐 E2B** — 自托管路径明确（同一个 Firecracker 二进制未来能搬到本地），Python-first，不需要 GPU。

**关键定位**：云沙箱只用于 **生成时验证**，不用于运行时。RPi 上每次 voice turn 加 150ms+ 往返是致命的。

### 本地沙箱（运行时用）

**按 RPi5 4GB 可行性排序**：

1. **bubblewrap + seccomp-BPF** ← **推荐**。
   - 1-5 MB/sandbox 开销，毫秒级启动，无 daemon。
   - **Anthropic 自家的 `sandbox-runtime` (`srt`) 就是这个**（[anthropic-experimental/sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)）。
   - user namespace + mount namespace + network namespace 组合，seccomp 过滤 `AF_UNIX`/`io_uring`。
   - ARM64 原生支持（Ubuntu 24.04+ 需要关 `apparmor_restrict_unprivileged_userns`）。
2. firejail — 配置更简单，开销类似。备选。
3. Docker with limits — 50-100MB daemon + 500ms+ 启动。RPi 上痛，**拒绝**。
4. gVisor — 15-30MB/sandbox，syscall 慢。比 bwrap 重。
5. **Pyodide/WASM — 拒绝**。200MB+ RAM、没有 `requests`（要 `pyodide-http`）、无法调 Hue/MQTT 原生 SDK。不适合控硬件的 skill。

### 静态验证（替代现有 regex 扫描）

**bandit + 自定义 AST walker**。10 条必须开的规则：

1. B102 `exec_used` / B307 `eval_used` —— 永远拒绝
2. B602-607 subprocess/shell —— `shell=True` / `os.system` / `os.popen` 拒绝
3. B301-306 pickle/marshal/shelve —— 反序列化不可信字节拒绝
4. B506 `yaml_load` —— 强制 `yaml.safe_load`
5. B113 `request_without_timeout` —— `requests` 必须 timeout
6. B310 `urllib_urlopen` —— 禁 `file://` / `ftp://`
7. **自定义：import 白名单** ← 最高价值规则。从 `config.yaml` 读允许列表，AST walk 所有 `Import/ImportFrom`。LLM 踩坑 80% 是乱 import。
8. 自定义：禁 `__import__` / `getattr(builtins, ...)` / `compile()`
9. 自定义：禁 dunder 逃逸 —— `.__class__.__bases__` / `.__subclasses__()` / `.__globals__`
10. 自定义：禁硬编码 IP / Bearer / 绝对路径（强制 CLAUDE.md 规则）

### Anthropic 自家 `code_execution` 怎么沙箱？

参考 [cw00h.github.io 逆向分析](https://cw00h.github.io/posts/2025/10/claude-code-web-sandbox/) (Oct 2025)：

- **三层隔离**：GCE VM → gVisor (runsc) → Docker 容器
- PID 1 是 `process_api` supervisor，管 cgroup/OOM
- 网络：HTTP(S) via 代理，裸 TCP/SSH 屏蔽
- `CAP_SYS_ADMIN` / `CAP_NET_ADMIN` 全掉，`/sys` 只读

关键：**本地开发 Anthropic 开源了不同的工具** —— `@anthropic-ai/sandbox-runtime` (srt)，Linux bwrap + macOS sandbox-exec。这就是你可以直接克隆的模式。

### 验证自动化：自测试 + 评审

1. **Property-based testing from parameters schema** — `hypothesis-jsonschema` + `schemathesis`。你的 `Skill` ABC 已经声明了 JSON Schema，免费得到 fuzz 测试。
2. **两 LLM review** — Generator (Grok) 写 skill，Reviewer (Groq Llama) 只看 spec + candidate（看不到 tests），避免共享假设偏差。参考 [hamelsmu/claude-review-loop](https://github.com/hamelsmu/claude-review-loop)。
3. **Diff-to-similar skills** — embed candidate，和已 approved skills 找最近邻。余弦 ≥0.90 + 静态 pass → 自动 approve；否则人工队列。你已经有 FastEmbed，零成本。
4. **Shadow mode** — `pending_review → shadow (sim 模式，10 次) → live`。auto-demote if N 连续异常。

---

## Part 3: Agent 框架 + Anthropic 原生 Skills

### Claude Code Skills / Agent Skills（同一个标准）

**目录结构**：

```
skills/my-skill/
├── SKILL.md           # 必需入口
├── reference.md       # 详细文档，按需加载
├── examples/
│   └── sample.md
└── scripts/
    └── helper.py      # bash 执行，不进入 context
```

**SKILL.md frontmatter**：

```yaml
---
name: exchange-rate
description: 查询汇率和货币转换。Use when user asks about currency, exchange rate, or conversion.
allowed-tools: Read Bash
disable-model-invocation: false
user-invocable: true
argument-hint: "[base_currency] [target_currency] [amount]"
---
...markdown body...
```

**关键 frontmatter 字段**：
- `name`: ≤64 字符，lowercase+hyphen
- `description`: 1536 字符，**前置触发词**
- `allowed-tools`: 预批准的工具
- `disable-model-invocation`: 只允许手动触发（如 `/factory-reset`）
- `user-invocable`: 是否允许用户直接 `/name` 调用
- `paths`: glob 限定作用域
- `argument-hint`: 参数提示

**Progressive Disclosure（三层）**：

| 层级 | 何时加载 | 大小预算 |
|---|---|---|
| 1. Metadata | 始终在 context | ~100 tokens/skill（只有 name + description） |
| 2. SKILL.md body | Claude 决定调用时 | <5k tokens |
| 3. Resources/scripts | 被引用时才读 | 不限（scripts 只进 stdout） |

**Live change detection**：Claude Code 监听 skill 目录，增删改中途生效，不用重启。

**加载顺序**（优先级）：enterprise > personal (`~/.claude/skills/`) > project (`.claude/skills/`) > plugin。

### SDK 差异

Python Agent SDK 要显式 `setting_sources=["user", "project"]` 才加载文件系统 skill。**没有程序化注册 API** —— skill 必须是文件系统产物。这点和你当前架构完全兼容。

### MCP 作为 skill 分发协议？

- **能不能**：能。MCP 的 `listChanged` 能力支持运行时增删工具。
- **要不要**：**runtime 不要，分发可以**。stdio 往返在 RPi5 会吃 voice 延迟预算；progressive disclosure 在 MCP 里没对应（MCP 只是工具传输，不是 instructions+scripts+ref）。
- **推荐混合**：热路径 skill 保留 in-process Python，"可共享/分发" 的 skill 可选导出 MCP 服务，给其他 agent/IDE 用。

### LangChain / AutoGen / CrewAI / OpenAI / Semantic Kernel

| 框架 | 运行时加 tool | 真正的 "学新技能" 流程？ |
|---|---|---|
| LangChain | `StructuredTool.from_function` | 无 |
| AutoGen | `register_function`  | 无（迁移到 MS Agent Framework） |
| CrewAI | `@tool` / `BaseTool` 子类 | 无 |
| OpenAI Agents SDK | `@function_tool` | 无 post-init 加法，只能重建 Agent |
| Semantic Kernel | `kernel.add_plugin()` + `from_directory()` | 最接近的目录级 plugin 模型，但无 progressive disclosure |

**结论**：**没有一个主流框架内置 "teach me a new skill" 闭环**。这正好是 Anthropic Skills 填补的空白，也是你 SkillFactory 的核心价值。

---

## 合成建议：Jarvis SkillFactory v2 架构

### 核心决策（优先级降序）

#### 决策 1 ★★★★★：生成产物切换到 Claude Code Skill 格式

**现在**：生成 `skills/learned/foo.py`（Python 类继承 `Skill` ABC）

**提议**：生成 `skills/learned/foo/SKILL.md` + `foo/handler.py`

**收益**：

- 免费得到 progressive disclosure（RPi 内存友好）
- 免费得到 live reload
- 免费得到权限声明（frontmatter `allowed-tools`、自定义 `required-role`）
- **生成的技能直接能用 `claude -p "/foo 100 USD JPY"` 调试** —— 你的主项目和 CC 共享同一个 skill 目录，dogfooding 闭环。
- 和 Anthropic 官方标准对齐，未来迁移成本低

**代价**：

- Jarvis `SkillLoader` 改造（读 frontmatter，`handler.py` 作为实际执行入口）
- `Skill` ABC 保留但成为 adapter 层：`SkillMdSkill(skill_dir) → Skill`
- 迁移现有 15 个 hand-written skills（一次性，每个 ~5min）

#### 决策 2 ★★★★：Voyager 式 embedding retrieval + critic 评分

- Description 用 FastEmbed embed，存 SQLite
- 调用时按 `utterance + context_state` 双向检索 top-5
- Skill 执行后 Groq Llama-3.3-70B 作 critic → `{success, score_0_10, critique}` JSON
- 通用性评分 ≥8 才进入永久 library，<8 进 "draft" 队列 7 天后清理

#### 决策 3 ★★★★：沙箱分两层

**生成时**：E2B 云沙箱跑 pytest + hypothesis fuzz + bandit（你主 Mac 开发期；RPi 上也可以跑，延迟不敏感）。

**运行时**：bwrap + seccomp（Linux/RPi）或 `sandbox-exec`（macOS 开发）。参考 `@anthropic-ai/sandbox-runtime` 直接用它。

**关键**：沙箱 profile 从 skill frontmatter 自动生成 —— `network_hosts: [api.meethue.com]`、`fs_read: [config.yaml]`、`fs_write: []`。

#### 决策 4 ★★★：静态验证 — bandit + import 白名单

替换当前 15 条 regex，换成：

- bandit 跑完整规则集
- 自定义 AST walker：import 白名单从 `config.yaml` 读
- 触发任一失败 → 直接 reject，不 hot-load

#### 决策 5 ★★★：两 LLM review

- Grok 生成 → Groq Llama review（只看 spec + code，不看 tests）
- 两者都 pass → 进 shadow mode
- 其中一个 reject → 人工队列

#### 决策 6 ★★：staged trust ladder

`pending_review → shadow (sim 模式 10 次) → live`。auto-demote if 3 次连续异常。

修复当前 bug：
- `_learn_create_bg` 必须先写 `enabled=false` metadata，再 register
- skill_loader 尊重 `status=pending_review` → 不加入 registry
- `has_skill()` 也要检查 enabled

#### 决策 7 ★：Prompt 硬约束 + helpers 库

Voyager 原话 "make it generic and reusable" 加入 prompt。
同时提供 `skills/helpers.py`（cache、config、retry）作为 reference 注入。

---

### v1 vs v2 架构对照

| 维度 | 当前 v1 | 提议 v2 |
|---|---|---|
| 产物格式 | `skills/learned/foo.py` | `skills/learned/foo/SKILL.md + handler.py` |
| 加载机制 | `importlib` 扫描 | `SkillMdLoader` 读 frontmatter + 懒加载 handler |
| 检索方式 | 全量注册到 registry，LLM 选 tool | embedding top-5 + context-aware |
| 安全 | 15 条 regex | bandit + AST 白名单 + bwrap sandbox |
| 验证 | pytest（如果生成了） | E2B + hypothesis + critic LLM + review LLM |
| 准入 | `enabled=true` 默认（pending_review 装饰） | `enabled=false` 默认，shadow → live staging |
| 热重载 | 需要重启或手动 `skill_loader.scan()` | Anthropic Skills 原生支持，watchdog 监听 |
| 权限 | 代码里硬编码 role | frontmatter 声明 |

---

### 开放决策点（等用户定）

1. **SKILL.md 切换范围**：只新生成的？还是连现有 15 个 hand-written skills 一起迁移？
2. **Critic 模型**：用 Groq Llama-3.3-70B（已在栈内、快、免费额度高）还是 Grok（和 Generator 同模型，可能有 self-bias）？
3. **沙箱部署时机**：bwrap 第一期就上，还是先做 static gate + staging，沙箱留 phase 2？
4. **Embedding retrieval 上线时机**：你当前 15 个 skill 其实 LLM 直接选就够用了，是等 skill 数 > 30 再上，还是一起做？
5. **E2B 云依赖**：生成时验证跑云沙箱，需要网络 + API key。愿意吗？还是纯本地？（纯本地 = bwrap 跑 pytest + hypothesis，但隔离弱一些。）

---

## Sources (聚合)

**Part 1 — Voyager 范式**：
1. [Voyager paper (arXiv 2305.16291)](https://arxiv.org/abs/2305.16291)
2. [Voyager GitHub](https://github.com/minedojo/voyager)
3. [Voyager critic prompt](https://github.com/MineDojo/Voyager/blob/main/voyager/prompts/critic.txt)
4. [OS-Copilot/FRIDAY paper (arXiv 2402.07456)](https://arxiv.org/abs/2402.07456)
5. [OS-Copilot GitHub](https://github.com/OS-Copilot/FRIDAY)
6. [Hanakano "Breaking down Voyager"](https://www.hanakano.com/posts/voyager-breakdown/)
7. [MetaGPT tool docs](https://docs.deepwisdom.ai/main/en/guide/tutorials/create_and_use_tools.html)
8. [LLM Tool Learning Survey (Springer 2025)](https://link.springer.com/article/10.1007/s41019-025-00296-9)

**Part 2 — 沙箱 + 验证**：
9. [Superagent AI Code Sandbox Benchmark 2026](https://www.superagent.sh/blog/ai-code-sandbox-benchmark-2026)
10. [ZenML — E2B vs Daytona](https://zenml.io/blog/e2b-vs-daytona)
11. [e2b-dev/firecracker](https://github.com/e2b-dev/firecracker)
12. [anthropic-experimental/sandbox-runtime (srt)](https://github.com/anthropic-experimental/sandbox-runtime)
13. [Anthropic Engineering — Beyond permission prompts](https://www.anthropic.com/engineering/claude-code-sandboxing)
14. [Senko Rašić — Sandboxing AI agents in Linux](https://blog.senko.net/sandboxing-ai-agents-in-linux)
15. [Woohyuk Choi — Claude Code Web Sandbox 逆向](https://cw00h.github.io/posts/2025/10/claude-code-web-sandbox/)
16. [He et al. — Property-based testing for LLM code (arXiv 2506.18315)](https://arxiv.org/abs/2506.18315)
17. [hypothesis-jsonschema](https://github.com/Zac-HD/hypothesis-jsonschema)
18. [hamelsmu/claude-review-loop](https://github.com/hamelsmu/claude-review-loop)
19. [Bandit plugin catalogue](https://bandit.readthedocs.io/en/latest/plugins/index.html)

**Part 3 — Agent 框架 + Anthropic 原生**：
20. [Claude Code Skills doc](https://code.claude.com/docs/en/skills)
21. [Agent Skills overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
22. [Anthropic Agent SDK skills](https://code.claude.com/docs/en/agent-sdk/skills)
23. [Anthropic blog — Introducing Agent Skills](https://claude.com/blog/skills)
24. [Engineering blog — Equipping agents for the real world](https://claude.com/blog/equipping-agents-for-the-real-world-with-agent-skills)
25. [anthropics/skills 开源仓库](https://github.com/anthropics/skills)
26. [MCP Tools specification](https://modelcontextprotocol.io/docs/concepts/tools)
27. [LangChain StructuredTool](https://docs.langchain.com/oss/python/langchain/tools)
28. [CrewAI custom tools](https://docs.crewai.com/en/learn/create-custom-tools)
29. [AutoGen register_function](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/agent-and-agent-runtime.html)
30. [OpenAI Agents SDK tools](https://openai.github.io/openai-agents-python/tools/)
31. [Semantic Kernel plugins](https://learn.microsoft.com/en-us/semantic-kernel/concepts/plugins/)

---

## Methodology

- 3 parallel research agents (general-purpose subagents)
- Tools: exa MCP (search + crawl) + WebSearch + WebFetch
- 30+ unique sources, 5+ primary sources read in full per topic
- Sub-questions: Voyager pattern / cloud sandboxes / local sandboxes / static validation / Anthropic Skills format / framework comparison / runtime tool registration
- Preference: 2024-2026 sources; one 2023 paper (Voyager, canonical) and one 2023 Pyodide issue included with flag.
