# Progress: 记忆系统优化

## Session 1 — 2026-04-04/05

### Deep Research 完成
- 5 个并行研究 agent: Mem0/MemGPT/ChatGPT+Gemini/学术论文/工程实践
- 配置 Exa + Firecrawl MCP
- 综合分析写入 `.claude/plans/functional-crafting-crab.md`
- 31 篇来源

### 关键发现
1. bge-small-zh 原版异常分布 → 升级 v1.5
2. Mem0 DELETE 操作是核心差距 → C6
3. Letta: 简单工具 > 复杂架构 → 验证小月方向
4. function calling > JSON → C5

### 方案确定
- 4 Phase, 13 项 (C1-C13)
- task_plan.md / findings.md / progress.md 创建

---
## Session 2 — 2026-04-05
### Phase 1 完成 ✅ (754 passed, +12 tests)
- C1: `date('now','localtime',?)` 
- C2: 阈值 0.75→0.55, margin 0.08, embedder 已是 v1.5
- C3: sweep_expired + backfill_expires + pending cleanup
- C4: 预算 1200→2000, 使用原则→personality.py
- Review: 删死代码, maintain()返回值补全
---
## Session 3 — 2026-04-05
### Phase 2 完成 ✅ (767 passed, +13 tests)
- C5: function calling 提取 + JSON fallback + postprocess(key/expires/importance)
- C6: DELETE 操作 + top-10 候选 + deactivate_memory_by_id
- C7: 校准 same_cat 0.55→0.65 (duplicates 0.855, related 0.632, unrelated 0.296)
- Merge: 修冲突(阈值+死代码), 修测试mock(_call_openai_json→_call_llm_extract)
---
## Session 4 — 2026-04-05
### Phase 3 完成 ✅ (785 passed)
- C8: 渐进检索 100→20阈值, 动态top_k(budget/25)
- C9: 冷启动检测 — cosine 0.40→0.60 when all access=0
- C10: eval_memory.py 基线: Retriever MRR@5=1.00, DA=5%, 负面拒绝=100%
- 关键发现: DA 0.55 阈值对自然中文查询仍太高(典型cosine 0.48)
- 修复: 日期滚动测试改为相对日期, 动态top_k测试断言修正
### Phase 3 深度验证
- 冷启动权重提取为类常�� W_COLD_*
- <=20 记忆按 importance 排序（预算溢出防护）
- 重新测评基线: DA=60%(非5%), Retriever=100%, 负面=100%
- DA 未命中集中在人名/实体查询(cosine ~0.50 < 0.55)
- 785 passed
---
## Session 5 — 2026-04-05
### Phase 4 完成 ✅ (806 passed, +49 tests from Phase 0)
- C11: episode Jaccard dedup + episode_digests 表 + weekly compression
- C12: save() accepts detected_emotion, ASR overrides LLM mood
- C13: memory_relations 表 + regex 提取 (X的Y叫Z等3模式) + 6 tests
- 手动 merge C13 (cherry-pick 冲突太复杂，直接编辑)
### 全部 13 项改进完成
---
## Session 6 — 2026-04-05
### Phase 5 完成 ✅ (811 passed)
- C14: DA 改用 retriever 多信号评分 → 60%→85%
- C15: 真实 LLM 提取测试脚本（98% checks passed）
- 深度验证发现：relationship 不在 _ANSWERABLE_CATEGORIES → 85%→**95%**
- 剩余 2 miss 是语义边界（"运动"↔"跑步" cos=0.445）
### 最终基线: DA=95%, Retriever=100%, 负面拒绝=100% 🎉
---
## Session 7 — 2026-04-05
### 任务 1: Hue 真实设备对接 ✅
- Bridge 配对 (IP 192.168.1.79, curl 手动配对)
- `pip install phue`, mode: sim → live
- 5 灯 + 2 灯组别名映射 (desk_lightstrip/bedroom_lamp_1,2/desk_play_1,2/all_lights/desk_lights)
- Live2D 资源 symlink 修复 (xiaozhi 项目)
- CORS 中间件 + TTS tuple 解包修复
- 意图路由支持 live 设备列表 + color_capable 区分
- 821 tests passed

### 智能家居路由重构 (进行中)
- set_color/set_color_temp/set_effect → 强制走 LLM + tool calling（避免 Groq 丢信息）
- turn_on/turn_off/set_brightness → 本地快速执行 + Groq 自然回复
- confidence >= 0.95 阈值，低于走 LLM
- HueLight 支持 hex RGB → CIE xy 转换
- tool description 强制 hex 颜色
- 本地执行后也触发记忆提取
- 意图路由加对话上下文（最近 2 轮）
- 颜色/色温操作时注入当前设备状态
- 记忆提取 prompt 不再跳过颜色偏好
- **待改进**: 相对指令（"淡一点"）需要更好的状态感知; 记忆学习颜色后复用
