# Universal Project Workflow Design Spec

*Generated: 2026-04-05 | Sources: 31+ (5-agent deep research) | Confidence: High*

## Executive Summary

一套通用的项目设计与完成流程，整合 Superpowers、ECC、Planning-with-Files 三大 skill 生态，通过智能分诊（TRIAGE）决定流程复杂度，质量/效率工具全部默认开启。每个关键步骤后加入反思优化环节，计划阶段使用第一性原则推导方案。

## 设计原则

1. **质量工具永远开启** — review、security、testing、ruff hook 不因任务小而跳过
2. **流程按复杂度伸缩** — triage 只决定 brainstorm/research/planning 是否需要，不砍质量
3. **最大化并行** — 每个阶段内独立任务并行执行，阶段间有质量门控
4. **反思驱动** — 关键步骤「深度思考并优化」，其他步骤「思考并优化」
5. **计划阶段第一性原则** — 从基本事实和约束出发推导方案，不抄现有方案
6. **持久化追踪** — task_plan.md / findings.md / progress.md 跨 session 存活

---

## Phase -1: TRIAGE（智能分诊）

每次任务开始前，花 ~500 tokens 评估三个维度，决定走哪条路径：

### 评估维度

| 维度 | 判断标准 |
|------|---------|
| **规模** | S(1-2文件, 单模块) / M(3-10文件) / L(10+文件, 跨模块) |
| **类型** | bug_fix / feature / refactor / research / config |
| **风险** | 是否涉及 auth, API, 用户输入, secrets, 代码生成, 命令执行 |

### 路径决策

```
规模=S + 类型=bug_fix/config  → 路径 S（轻量）
规模=M 或 类型=feature/refactor → 路径 M（标准）
规模=L 或 类型=research 或 涉及未知技术 → 路径 L（完整）
```

### 路径对比

|  | 路径 S（轻量） | 路径 M（标准） | 路径 L（完整） |
|--|---------------|---------------|---------------|
| Phase 0 | 跳过 | brainstorm（简短） | brainstorm + deep-research 并行 |
| Phase 1 | 跳过 | writing-plans + 第一性原则 + 可行性验证 | writing-plans + planning-with-files + 第一性原则 + 可行性验证 |
| Phase 2 | TDD → 实现 | TDD → 实现 → python-review | TDD → 并行 worktree → python-review + security-review |
| Phase 3 | pytest + verification | pytest + code-review + verification | pytest + code-review + security-scan 并行 + verification |
| Phase 4 | commit | commit + progress.md | commit + progress.md + learn-eval |

### 永远开启（不受路径影响）

- Ruff hook（PostToolUse 自动 format + lint）
- pytest hook（Stop 时必须通过）
- python-review（任何代码改动）
- verification-before-completion（任何任务完成前）
- security-review（当 triage 标记 security-required 时）
- docs / Context7（涉及外部库时）

---

## Phase 0: DISCOVER（研究 + 设计）

> 仅路径 M/L 执行

### 流程

```
┌─────────────────────┐  ┌────────────────────────┐
│ brainstorm           │  │ deep-research (仅路径L) │  ← 并行
│ (superpowers)        │  │ (ECC: exa + firecrawl)  │
│ 探索需求、约束、     │  │ 调研现有方案、论文、    │
│ 成功标准             │  │ 社区实践                │
└──────────┬───────────┘  └──────────┬─────────────┘
           │     docs (Context7) 按需补充外部库文档
           └──────────┬────────────┘
                      ▼
              Design Spec 草案
                      │
                      ▼
           ┌─────────────────────┐
           │ 深度思考并优化        │
           │ - 需求理解对不对？    │
           │ - 遗漏了什么？        │
           │ - 有没有更优解？      │
           └──────────┬──────────┘
                      ▼
              用户批准 Design Spec
```

### 调用的 Skills

| Skill | 来源 | 触发条件 |
|-------|------|---------|
| `brainstorm` | Superpowers | 路径 M/L |
| `deep-research` | ECC (exa + firecrawl MCP) | 路径 L |
| `docs` / Context7 | ECC + MCP | 涉及外部库 |

---

## Phase 1: PLAN（第一性原则 + 计划 + 可行性验证）

> 仅路径 M/L 执行

### 流程

```
Step 1: 第一性原则分解
  │  这个问题的本质约束是什么？
  │  不可绕过的物理/技术限制是什么？
  │  从零开始，最优解应该长什么样？
  │
  ▼  思考并优化
  │
Step 2: 方案推导
  ┌─────────────────────┐  ┌────────────────────────┐
  │ writing-plans        │  │ planning-with-files     │  ← 互补
  │ (superpowers)        │  │ (仅路径L)               │
  │ → impl plan          │  │ → task_plan.md          │
  │ → 步骤分解           │  │ → findings.md           │
  │ → 文件路径+命令      │  │ → progress.md           │
  └──────────┬───────────┘  └──────────┬─────────────┘
             │                         │
             │  search-first (ECC)     │
             │  查现有方案避免重复造轮子 │
             └──────────┬──────────────┘
                        ▼
             ┌─────────────────────┐
             │ 深度思考并优化        │
             │ - 方案是从第一性原则  │
             │   推导的，还是在抄    │
             │   现有方案？          │
             │ - 步骤分解合理吗？    │
             │ - 依赖关系对吗？      │
             │ - 有没有过度设计？    │
             └──────────┬──────────┘
                        ▼
Step 3: 可行性验证
  │  对计划中的每个关键步骤：
  │  - 技术上能实现吗？有没有 API 限制、硬件约束？
  │  - 有没有隐含的依赖没列出来？
  │  - 时间估算合理吗？（对比类似历史任务）
  │  - 最大风险点在哪？如果失败怎么回退？
  │
  ▼  深度思考并优化
  │  - 这个计划真的可行吗？
  │  - 哪一步最可能出问题？
  │  - 有没有因为惯性思维多加了不必要的步骤？
  │
  ▼
  用户批准 Plan
```

### 调用的 Skills

| Skill | 来源 | 触发条件 |
|-------|------|---------|
| `writing-plans` | Superpowers | 路径 M/L |
| `planning-with-files:plan` | P-w-F | 路径 L |
| `search-first` | ECC | 写新代码前 |

---

## Phase 2: BUILD（并行构建 + 实时质量）

### 单个 Task 的执行流

```
┌───────────────────────────────────────────────────┐
│  Worktree Agent (isolated, 路径L时并行多个)         │
│                                                    │
│  1. tdd-workflow / python-testing                  │
│     写测试 → RED                                   │
│     │                                              │
│     ▼  思考并优化                                   │
│     │  测试覆盖够吗？边界情况？                      │
│     │                                              │
│  2. 写实现 → GREEN                                 │
│     [Hook: Ruff auto-format on save — 自动]        │
│     │                                              │
│     ▼  思考并优化                                   │
│     │  有更简洁的写法吗？                            │
│     │                                              │
│  3. python-review (ECC agent)                      │
│     │                                              │
│     ▼  思考并优化                                   │
│     │  review 发现的问题都修了吗？                   │
│     │                                              │
│  4. security-review (如 triage 标记 required)       │
│     │                                              │
│     ▼  思考并优化                                   │
│     │  安全问题有没有漏掉的？                        │
│     │                                              │
│  5. pytest gate (该 task 相关测试)                  │
│     PASS → 标记 task 完成, 更新 progress.md         │
│     FAIL → 回到 step 2                             │
└───────────────────────────────────────────────────┘
```

### 并行策略（路径 L）

```
task-A (worktree-A) ║ task-B (worktree-B) ║ task-C (worktree-C)
    各自独立执行上述流程
    通过 subagent-driven-development (Superpowers) 调度
    progress.md 实时更新
```

### 调用的 Skills

| Skill | 来源 | 触发条件 |
|-------|------|---------|
| `tdd-workflow` / `python-testing` | ECC | 所有路径 |
| Ruff hook | PostToolUse | 自动，零成本 |
| `python-review` | ECC agent | 所有路径 |
| `security-review` | ECC agent | security-required |
| `subagent-driven-development` | Superpowers | 路径 L，多独立 task |
| `docs` / Context7 | ECC + MCP | 遇到外部库 API 时 |
| `aside` | ECC | 用户中途提侧问时 |

---

## Phase 3: VERIFY（全局验证）

```
┌──────────────┐ ┌────────────────┐ ┌─────────────────┐
│ pytest -v    │ │ code-reviewer  │ │ security-scan   │  ← 并行
│ (全量测试)   │ │ (Superpowers)  │ │ (ECC agent)     │
│              │ │ 对照 plan 审查 │ │ OWASP Top 10    │
└──────┬───────┘ └───────┬────────┘ └────────┬────────┘
       └─────────────┬───────────────────────┘
                     ▼
          ┌─────────────────────────┐
          │ 思考并优化                │
          │ - review 和 scan 发现    │
          │   的问题都修完了吗？      │
          └────────────┬────────────┘
                       ▼
          verification-before-completion (Superpowers)
          → 必须展示实际命令输出
          → 不允许空口说 "done"
                       │
                       ▼
          ┌─────────────────────────┐
          │ 深度思考并优化            │
          │ - 整体一致性如何？        │
          │ - 有没有遗留问题？        │
          │ - 改动的副作用想清楚了吗？ │
          └────────────┬────────────┘
                       ▼
          context-budget (ECC) 检查 context 健康
          progress.md 更新验证结果
```

### 调用的 Skills

| Skill | 来源 | 触发条件 |
|-------|------|---------|
| `requesting-code-review` | Superpowers | 路径 M/L |
| `security-scan` | ECC agent | 路径 L 或 security-required |
| `verification-before-completion` | Superpowers | 所有路径 |
| `context-budget` | ECC | 路径 L 或 长 session |

---

## Phase 4: SHIP（交付 + 学习）

```
Step 1: finishing-a-development-branch (Superpowers)
  │  → commit（不自动 push，等用户要求）
  │  → 或创建 PR
  │
  ▼  思考并优化
  │  commit message 准确反映了改动吗？
  │
Step 2: progress.md 标记完成
  │
Step 3: learn-eval (ECC, 路径 L)
  │  提取本次可复用的模式
  │
  ▼  深度思考并优化
  │  - 提取的模式真的通用吗？
  │  - 这次改动的长期影响？
  │  - 下次做类似任务，哪里可以更快？
  │
  ▼
  Done.
```

### 调用的 Skills

| Skill | 来源 | 触发条件 |
|-------|------|---------|
| `finishing-a-development-branch` | Superpowers | 所有路径 |
| `learn-eval` | ECC | 路径 L |

---

## Skill 总览：三层架构

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1: 流程编排 (Superpowers)                          │
│ brainstorm → writing-plans → executing-plans             │
│ → code-review → verification → finishing-branch          │
│ 控制: 阶段门控、用户批准点、反思环节                       │
├─────────────────────────────────────────────────────────┤
│ Layer 2: 持久化追踪 (Planning-with-Files)                │
│ task_plan.md / findings.md / progress.md                 │
│ 控制: 跨 session 状态、/clear 后恢复、进度可见            │
├─────────────────────────────────────────────────────────┤
│ Layer 3: 专业工具 (ECC + MCP)                            │
│ python-review, security-review, tdd, deep-research,      │
│ docs/Context7, search-first, learn-eval, context-budget  │
│ + Ruff hook, pytest hook (自动化质量门)                   │
│ 控制: 代码质量、安全、测试、文档、学习                     │
└─────────────────────────────────────────────────────────┘
```

## 反思系统

两级反思贯穿全流程：

| 级别 | 适用场景 | 关注点 |
|------|---------|--------|
| **深度思考并优化** | 设计决策、计划制定、可行性验证、最终验证、交付学习 | 方向对不对？有没有更优解？长期影响？ |
| **思考并优化** | 实现、测试、review、文档 | 当前步骤做到位了吗？有没有遗漏？ |

### 深度思考触发点（6处）

1. Phase 0: Design Spec 完成后
2. Phase 1: 方案推导后（第一性原则检验）
3. Phase 1: 可行性验证后
4. Phase 3: 最终验证后（整体一致性）
5. Phase 4: 最终交付后（长期影响）
6. Phase 4: learn-eval 后（模式通用性）

### 思考并优化触发点（8处）

1. Phase 0: deep-research 后
2. Phase 1: 第一性原则分解后
3. Phase 2: 写测试后（覆盖率）
4. Phase 2: 写实现后（简洁性）
5. Phase 2: python-review 后
6. Phase 2: security-review 后
7. Phase 3: 并行验证汇总后
8. Phase 4: commit 后

---

## 需要的配置动作

### 1. 安装 Context7 MCP

```bash
claude mcp add context7 -- npx -y @upstash/context7-mcp@latest
```

### 2. 配置 Ruff Hook（.claude/settings.json）

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "if [[ \"$CLAUDE_TOOL_INPUT_FILE_PATH\" == *.py ]]; then ruff format \"$CLAUDE_TOOL_INPUT_FILE_PATH\" && ruff check --fix \"$CLAUDE_TOOL_INPUT_FILE_PATH\" 2>/dev/null; fi"
          }
        ]
      }
    ]
  }
}
```

### 3. 启用 Agent Teams（可选）

在 `~/.claude/settings.json` 中加:

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

---

## 并行策略总览

```
Phase 0:  brainstorm ║ deep-research ║ docs-lookup      (最多3路)
Phase 1:  writing-plans ║ planning-with-files            (2路)
Phase 2:  task-A(worktree) ║ task-B(worktree) ║ task-C   (N路)
Phase 3:  pytest ║ code-review ║ security-scan            (3路)
```

---

## 适用范围

- **语言**: 当前优化为 Python (Ruff hook, python-review)，换语言改对应 reviewer 即可
- **项目类型**: 通用，从单人 side project 到团队项目
- **任务类型**: 通过 TRIAGE 自动适配 bug fix / feature / refactor / research
- **不适用**: 纯文档任务、纯运维操作（这些不需要走开发流程）

---

## 研究来源

本设计基于 5 个并行研究 agent 的综合分析（31+ 来源）：

1. Superpowers (135K stars) — 流程编排最佳实践
2. Everything Claude Code (138K stars) — 专业工具生态
3. Planning-with-Files (9.2K stars) — 持久化追踪模式
4. Trail of Bits, Snyk, Anthropic Security Review — 安全实践
5. 社区工作流分享 (Builder.io, DEV Community, Medium, Reddit)
6. Claude Code 官方文档 (Best Practices, Hooks Guide, Agent Teams)
