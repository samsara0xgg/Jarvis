# SkillFactory v2 合成建议 — 9 Agent 调研后的深度思考

*Date: 2026-04-14 · 基于 Part 1 + Part 2 + Part 3*

---

## 核心洞察：你问错了问题

**v1 在问**："怎么让 LLM 写更可靠的 Python？"

**v2 应该问**："我们实际需要多少代码生成？"

这不是语义游戏。研究数据强烈暗示：**你 80% 想"学"的 skill 根本不需要 Python**。剩下 20% 才是真正值得所有 reliability / refine / critic 武器招呼的场景。

### 为什么 v1 架构逻辑上错配了用户意图

你的目标是"以后都不用自己写 skill"。v1 实现路径是"让 LLM 替你写 Python"。

但"不用自己写 skill"的最短路径其实是**让大部分"skill"根本不存在**（不存在为 Python 文件），而是作为**配置**存在：

| 用户意图 | 真正的本质 | 现在 v1 | 更贴切的形式 |
|---|---|---|---|
| "学会查汇率" | 调 api.exchangerate-api.com | 写 `exchange_rate.py` 100 行 | 20 行 YAML，声明 URL + 参数映射 |
| "学会每天早上查天气和股票" | 调度 + 2 个现有 skill 顺序调用 | 生成新 Python + 写调度逻辑 | 10 行 compose YAML |
| "学会当股票跌 5% 提醒" | 触发器 + 比较 + 通知 | 生成新 Python class | 8 行 YAML trigger + action |
| "学会根据我心情放音乐" | 真的需要逻辑 —— 读 memory、判心情、查 API、选歌 | 写 Python | 这个才**真**需要 Python |

**第 4 行才是 SkillFactory 的 sweet spot**。前 3 行用 Python 是杀鸡用牛刀，而且是一把**经常砍歪的**牛刀（LLM 在 Python 代码上 60-80% 一次成功率）。

### 研究数据汇聚到同一结论

- **Anka DSL (Dec 2025)**：**多步管道任务 +40 pp** vs Python（100% vs 60%），Claude Haiku 零训练 95.8%
- **Microsoft 700-API DSL**：hallucinated 参数名 **-20 pp**，API 名 -6 pp
- **Anthropic `anthropics/skills` 仓库实测**：**70-90% 是 config/prose，10-30% 是预写代码**，**绝不每次用时生成代码**
- **Zapier AI Actions**：7,000 apps / 30,000 actions 跑在声明式 YAML 上，没生成一行 Python

**Python 的灵活性就是失败的来源**。同一任务有多种有效写法 + 隐式状态 = LLM 容易走偏。消除选择就消除失败。

---

## 推荐架构：SkillFactory v2

```
用户: "学会 X"
   ↓
[1] SkillTypeRouter（Groq Llama-70B，已在栈内）
    分类为：
    ├─ api_wrapper   → HTTP 调用 + JSON 解析 + 响应模板
    ├─ composition   → 串联现有 skills + 可选调度
    ├─ recall        → 读/写 memory + 触发条件
    └─ novel_code    → 真的需要任意 Python（最后选项）

[2] 分派到对应生成器：

    ─── API wrapper (预计 50% 用例) ─────
    生成 YAML：
      name: exchange_rate
      type: http
      api:
        url: "https://open.er-api.com/v6/latest/{base}"
        method: GET
      params:
        base: {type: string, default: USD}
        target: {type: string, required: true}
        amount: {type: number, default: 1}
      response:
        template: "{amount} {base} = {round(amount * $.rates[target], 4)} {target}"
      permissions:
        required_role: guest
    
    固定解释器（300 行审过一次的 Python）读 YAML 做运行时调用。
    **0% 代码生成。无失败面除了 URL / 参数 slot 填错**。

    ─── Composition (预计 25% 用例) ─────
    生成 compose YAML（Anka 风格）：
      name: morning_routine
      type: compose
      schedule: "0 8 * * *"
      steps:
        - skill: weather
          params: {location: home}
          bind: $weather
        - skill: realtime_data
          params: {symbols: [NVDA, AAPL]}
          bind: $stocks
        - skill: say
          template: "今天{$weather.condition}，{$weather.temp}度..."
    
    解释器跑 compose graph。**0% 代码生成**。

    ─── Recall (预计 15% 用例) ─────
    生成 YAML：
      name: remember_pill
      type: recall
      trigger: {keyword: "该吃药了"}
      action: {memory_key: medication_time, query: pending}
    
    **0% 代码生成**。

    ─── Novel code (预计 10% 用例) ─────
    这里才上 CC + 完整的可靠性武器（见下一节）。
    **只在前 3 类都不匹配时触发。**
```

### 10% 落到 CC 代码生成的 Skill — 用 Part 1 + Part 2 的全部武器

```python
async def generate_novel_skill(spec: dict):
    # Stage 1: Spec 结构化（AlphaCodium 式）
    spec_yaml = await groq.generate(
        prompt=spec_prompt(spec),
        format="yaml",
        fields=["intent", "inputs", "outputs", "edge_cases", 
                "error_modes", "dependencies"]
    )
    
    # Stage 2: RAG — FastEmbed 拉 top 3 相似既有 skill
    exemplars = fastembed.similarity_search(
        query=spec_yaml["intent"],
        corpus=skills_library,
        top_k=3,
        filter={"pass_rate": {"$gte": 0.9}}  # 只拉高质量样本
    )
    
    # Stage 3: 并行生成
    async with ClaudeSDKClient(
        options=ClaudeAgentOptions(
            allowed_tools=["Read", "Write", "Edit"],
            setting_sources=["project"],  # 读 CLAUDE.md
            cwd=SKILL_DIR,
        )
    ) as cc:
        code_task = cc.query(build_code_prompt(spec_yaml, exemplars))
        # 同时 Groq 按 spec 生成 6-8 pytest
        tests_task = groq.generate(build_tests_prompt(spec_yaml))
        code, tests = await asyncio.gather(code_task, tests_task)
    
    # Stage 4: Sandbox 执行（bwrap，参考 anthropic-experimental/sandbox-runtime）
    result = sandbox.run_pytest(code, tests)
    
    # Stage 5: CRITIC loop（最多 3 轮，跨模型）
    for round in range(3):
        if result.all_passed:
            break
        # Haiku 作 critic，JSON schema 强制 rubric 输出
        critique = await haiku.generate_with_schema(
            prompt=critic_prompt(code, tests, result.failures),
            schema=RUBRIC_SCHEMA,
        )
        # 重用 session 省 cache
        code = await cc.query(
            revise_prompt(code, critique, result.failures)
        )
        result = sandbox.run_pytest(code, tests)
    
    # Stage 6: 静态 gate
    gates_passed = all([
        ruff_check(code),
        bandit_scan(code, severity="high"),
        radon_mi(code) >= 65,
        description_body_similarity(spec_yaml["intent"], code) >= 0.75,
    ])
    
    # Stage 7: 入库
    if gates_passed:
        status = "ready" if result.all_passed else "experimental"
        skill_library.commit(skill_id, {
            "code": code, "tests": tests, "spec": spec_yaml,
            "pass_rate": result.pass_rate, "status": status,
            "version": 1, "created_at": now(),
        })
    else:
        return {"success": False, "reason": "static gates failed"}
```

### 真实后台 refine（不是"无限循环"，是"失败驱动"）

```python
# 只由这些信号触发（永不定时）:
async def on_skill_failure(skill_id: str, context: dict):
    """
    Triggered by:
    - Skill raised exception in live use
    - User said "that didn't work" / "不对"
    - Telemetry: success rate dropped below threshold
    """
    # 预算硬限
    if refine_budget[skill_id].calls_this_week >= 5:
        queue_for_manual_review(skill_id, context)
        return
    
    # 加失败复现测试
    new_test = groq.generate_reproducing_test(
        skill_code=load_skill(skill_id),
        failure_context=context,
    )
    tests = load_tests(skill_id) + [new_test]
    
    # 跑同步 refine loop（3 轮 max）
    v2_code = await generate_novel_skill.refine(
        current=load_skill(skill_id),
        augmented_tests=tests,
    )
    
    # Shadow mode 24 小时
    shadow_results = await shadow_test(skill_id, v2_code, hours=24)
    
    if shadow_results.success_rate > current_success_rate + 0.05:
        promote_v2(skill_id, v2_code)
    else:
        discard_v2(skill_id, v2_code)  # 绝不降级
```

---

## v1 vs v2 对照

| 维度 | v1 现在 | v2 提议 |
|---|---|---|
| 产物格式 | 每个 skill 都是 Python 类 | 80% YAML / 20% Python + handler.py |
| 生成路径 | 单一路径（CC 写 Python） | 4 条路径按 skill 类型分派 |
| 一次成功率 | ~60-80%（narrow Python） | 预计 90%+（大部分走 YAML 零代码生成） |
| Critic | 无 | Haiku 作 rubric critic（跨模型，$0.001/次） |
| 测试 | 偶尔有 | 每个代码 skill 6-8 pytest（Groq 生成） |
| 参考注入 | 无 | FastEmbed top-3 similar skills as few-shot |
| Refine | 无 | 失败驱动，同步 3 轮 max，shadow promote |
| 后台改进 | 无计划 | 预算化失败 refine + 累积 retrieval 库 |
| 沙箱 | Regex 扫描 | bwrap + bandit + Radon + AST 白名单 |
| Session 管理 | 裸 `subprocess.Popen` | Python `ClaudeSDKClient`（cache 友好） |
| 准入 | `enabled=true` 默认，pending_review 装饰 | `enabled=false` 默认，shadow → live staging |
| 风格统一 | 无 | 检索 few-shot 涌现（Voyager 机制） |
| 成本/skill | ~$0.02-0.05（无 critic） | ~$0.035（含 1 轮 critic） |

---

## 分阶段 rollout（2-3 个月逐步落地）

### Phase 1：修现有 v1 核心缺陷（1 周，ROI 最高）

- **修 `_learn_create_bg` 的 `enabled=false` bug**（我上次调研指出的 4.1 更严重版本）
- **加跨模型 critic**：Haiku 作 rubric reviewer，JSON schema 强制输出。成本 <$0.001/skill
- **改 `subprocess.Popen` → `ClaudeSDKClient`**：session 续接 + cache
- **加 session_id 捕获 + resume 逻辑**：让 revision 能复用 CC 的 context

**效果**：v1 一次成功率从 ~65% → 预计 ~80%。修掉现在 `fifa_tickets` 孤儿 metadata 那类 bug。

### Phase 2：测试驱动 + RAG 注入（1 周）

- **Groq 按 spec 生成 6-8 pytest**（在 CC 生成代码前或并行）
- **FastEmbed RAG**：查 top-3 相似 skill 作 full-file few-shot
- **Sandbox 执行 + anchor 语义 refine loop**（最多 3 轮）
- **静态 gate**：ruff + bandit + Radon MI + description-body similarity

**效果**：一次成功率 ~85-90%。风格自然收敛（Voyager 机制）。

### Phase 3：声明式 YAML + HTTP skill type（2 周，**最大解锁**）

- **设计 YAML spec schema**（参考 Anthropic Skills frontmatter）
- **写 HTTP 解释器**（~200-300 行固定 Python，审过一次）
- **Router 加 api_wrapper 分类**
- **改 prompt**：让 Groq 判定是 API 类型后生成 YAML 而不是 Python
- **迁移 `exchange_rate` 和 `weather` 作为首批 YAML skill 验证架构**

**效果**：**预计 API 类 skill 一次成功率 >95%**（消除了整个代码生成失败类）。50% 的未来 skill 走这条路径。

### Phase 4：Compose DSL（2 周）

- **定义 ~10 个 primitive**（http_get、jmespath、filter、map、compose、if、slot_fill、call_skill、say、remember）
- **写 DSL 解释器 + validator**
- **Router 加 composition 分类**
- **替换 `automation_rules` + `scheduler_skill` 的部分功能**

**效果**：多步 skill 表达力强 + 一次成功率 >90%（Anka +40pp 验证）。又 25% skill 走这条路径。

### Phase 5：失败驱动 refine + 级联路由（持续）

- **logging pipeline**：记录每次 skill 调用的 outcome
- **Cascade routing**：local Qwen-7B 先试简单 skill，Opus 兜底
- **失败触发 refine 管道 + shadow mode 晋升**
- **遥测 dashboard**：pass rate、调用频率、refinement 触发

### Phase 6（可能永不）：Fine-tune

- 只在累计 1000+ 稳定 skill 后考虑
- 只对最常见 skill **家族**（Hue 灯控、MQTT publish 等）LoRA
- 通用生成继续走 Claude

---

## 关键开放决策点（等你定）

### 1. Router 模型 + 路径比例

- Groq Llama-70B 分类成本几乎零，延迟可接受（后台）
- 4 个类别中途可加（recall 最晚）
- **你的预估**：每类占多少？若 api_wrapper 真占 50%，架构价值巨大；若只占 20%，收益变小

### 2. OpenAPI registry 来源

- **自建**：你手选的 API 集合（最精准但要维护）
- **Zapier AI Actions**：7K apps / 30K actions，$/按调用付费
- **RapidAPI / APIs.guru**：公共目录，免费但质量参差
- **混合**：核心自建 + fallback 到目录

### 3. DSL 设计方式

- **直接借 Anka**（arXiv:2512.23214，学术原型）
- **自研** ~10 primitive（更贴合你语音场景）
- **借 Home Assistant YAML** 的自动化语法（~60% 重叠）

### 4. 沙箱部署时机

- Phase 1-2 阶段用 **bandit + AST 白名单**（无沙箱）够吗？
- 还是一开始就上 **bwrap**（参考 Anthropic srt）？
- 云 **E2B** 只用于生成时验证阶段？

### 5. 你愿意接受的"experimental" skill 比例

- 代码生成路径 3 轮 refine 后还失败 → 标 experimental 入库 vs 整个 reject？
- experimental skill 是否进入 RAG few-shot 池（作反面教材）？

### 6. MCP 作为 skill 分发协议要不要

- 长期把成熟 skill 包装成 MCP server，让其他 CC / Cursor 实例复用
- 这是 Phase 6 级的优化，现在可以完全忽略

---

## 什么是 "useful ideas"（直接答你的问题）

1. **"80% 声明式 20% 代码" 是这个调研的唯一 ★★★★★ 观点**。其他所有技术都是优化代码生成路径。这条是 reframe，值最多。

2. **你现有栈内有 3 个没用起来的宝贝**：
   - **FastEmbed**（memory 用了）→ SkillFactory 拿来作 RAG few-shot 零成本
   - **Groq Llama-70B**（intent router 用了）→ 作 critic / test generator / skill type 分类全部免费
   - **Python ClaudeSDKClient**（没用）→ 自动 session + cache，比 subprocess 强

3. **"生成可用版 → 后台 refine 到收敛" 这个心智模型是错的**。正确心智模型是：
   - 生成时用所有武器保一次 80-95% 成功
   - 失败时只因真实 signal 触发精确 refine（加复现测试）
   - "统一格式" 靠 RAG few-shot 涌现，不靠模板
   - 库质量靠 **cascade 路由** 和 **usage data** 累积，不靠 fine-tune

4. **Cross-model critic 成本几乎为零**（Haiku $0.001 vs Opus $0.02），但消除 same-model echo chamber。Phase 1 最容易落的优化。

5. **OpenAPI registry 对语音助手是最大杠杆解**。"学会查 X"的 X 80% 已经有公开 API。**Zapier 已经解决 30K 个**，你只要决定是重用还是自建索引。

6. **Claude Code Skills 格式**（SKILL.md + handler.py）这事**变成 tier-2 优化**，不是核心设计。先做 YAML spec，再考虑是否套 SKILL.md 外衣。先内容后皮囊。

7. **没有任何生产 coding agent 做 Voyager 式 skill 自积累**（Aider/Cursor/Devin/OpenHands/Codex/Cline 全没做）。你做这个是**pioneering** —— 要接受"没 playbook 可抄"的风险，但也意味着做成了是真的领先。

8. **mini-swe-agent 的警告**：100 LOC + 裸 bash + Opus 能跑 74% SWE-bench Verified。**花哨 scaffolding 大部分是幻觉价值**。先确保基础（测试、RAG、cross-critic）稳固，再加层。

9. **具体成本账**（10 skills/week 规模）：
   - Phase 1-2：Opus 生成 + Haiku critic + Groq tests = ~$0.035/skill × 10 × 52 = **$18/年**
   - Phase 3-4：70% 走 YAML 路径（~$0.002/skill）+ 30% 代码 = **<$10/年**
   - **完全可以接受的家庭预算**

10. **最大风险**（应该直面）：**Router 分类错**。如果"学会查汇率"被错分到 novel_code，体验没变差；如果 novel_code 被错分到 api_wrapper，用户收到"我不知道哪个 API"然后失败。**错分成本不对称，应该偏向把模糊情况塞到 novel_code**。

---

## 一句话总结

**v1 的问题不是"LLM 写 Python 不够好"，是"大部分 skill 根本不该是 Python"**。v2 用 80% YAML 声明式 + 20% 带完整武器库的代码生成替代 100% 代码生成路径 —— 把可靠性问题从"让模型写对"变成"让模型少写"。

---

## 下一步

**如果认可这个架构**：

1. 我写一个 Phase 1 的正式 implementation plan（`docs/superpowers/plans/2026-04-14-skillfactory-v2-phase1.md`），包含测试驱动 + 文件改动清单
2. 先补 Phase 1 的 3 个具体改动（critic / SDK / bug fix），验证单 skill 成功率提升
3. 再讨论 Phase 3 的 YAML schema 设计

**如果有不同想法**（比如不信"80% 不该是 Python"）：

我愿意具体挑战。给我 5 个你想"学"但现在没学的 skill，咱们当场看哪些真需要 Python、哪些能用 YAML。
