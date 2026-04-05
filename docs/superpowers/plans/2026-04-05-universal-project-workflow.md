# Universal Project Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a local skill `/project-workflow` that implements the 5-phase intelligent workflow (TRIAGE → DISCOVER → PLAN → BUILD → VERIFY → SHIP), plus configure supporting hooks and MCP.

**Architecture:** One SKILL.md file encodes the entire workflow logic including triage, phase routing, skill activation matrix, reflection system, and first-principles planning. Supporting infrastructure (Ruff hook, Context7 MCP) configured separately.

**Tech Stack:** Claude Code skills (SKILL.md), MCP (Context7), Hooks (Ruff), Bash

---

### Task 1: Install Context7 MCP

**Files:**
- Modify: `~/.claude.json` (auto-managed by `claude mcp add`)

- [ ] **Step 1: Install Context7 MCP server**

Run:
```bash
claude mcp add context7 -- npx -y @upstash/context7-mcp@latest
```

Expected: Success message, context7 added to MCP config.

- [ ] **Step 2: Verify Context7 is available**

Start a new Claude Code session or run:
```bash
claude -p "use context7 to look up the sherpa-onnx Python API" --allowedTools "mcp__context7__resolve-library-id,mcp__context7__query-docs"
```

Expected: Returns actual sherpa-onnx documentation, not training data.

- [ ] **Step 3: Commit**

No code change to commit — this is a global config change.

---

### Task 2: Configure Ruff Hook (Project-Level)

**Files:**
- Create: `/Users/alllllenshi/Projects/jarvis/.claude/settings.local.json`

- [ ] **Step 1: Check ruff is installed**

Run:
```bash
which ruff || pip install ruff
```

Expected: Path to ruff binary.

- [ ] **Step 2: Create project-level settings with Ruff hook**

Create `.claude/settings.local.json` in the jarvis project:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "if [[ \"$CLAUDE_TOOL_INPUT_FILE_PATH\" == *.py ]]; then ruff format \"$CLAUDE_TOOL_INPUT_FILE_PATH\" 2>/dev/null; ruff check --fix \"$CLAUDE_TOOL_INPUT_FILE_PATH\" 2>/dev/null; fi; exit 0"
          }
        ]
      }
    ]
  }
}
```

Note: `exit 0` at the end ensures the hook never blocks Claude even if ruff finds unfixable issues. The hook is project-level (`.claude/settings.local.json`) not global, so it only affects Python projects.

- [ ] **Step 3: Verify hook works**

Open Claude Code in the jarvis directory, edit any `.py` file, and check that ruff auto-formats it.

Run:
```bash
cd /Users/alllllenshi/Projects/jarvis && python -c "
# Create a deliberately badly formatted test file
with open('/tmp/test_hook.py', 'w') as f:
    f.write('import os,sys\\nx=1+2\\n')
print('Test file created')
"
```

- [ ] **Step 4: Commit hook config**

```bash
cd /Users/alllllenshi/Projects/jarvis
git add .claude/settings.local.json
git commit -m "chore: add Ruff auto-format hook for PostToolUse"
```

---

### Task 3: Create the `/project-workflow` Skill

**Files:**
- Create: `~/.claude/skills/project-workflow/SKILL.md`

This is the core deliverable. The SKILL.md encodes the entire workflow.

- [ ] **Step 1: Create skill directory**

```bash
mkdir -p ~/.claude/skills/project-workflow
```

- [ ] **Step 2: Write SKILL.md**

Create `~/.claude/skills/project-workflow/SKILL.md` with the full workflow content:

```markdown
---
name: project-workflow
description: Universal 5-phase project workflow with intelligent triage, first-principles planning, reflection system, and parallel execution. Combines Superpowers + ECC + Planning-with-Files.
user-invocable: true
---

# Project Workflow — Universal 5-Phase Development Process

You are executing a structured development workflow. Follow each phase exactly. Do not skip phases unless TRIAGE explicitly allows it.

## How to Use

Invoke with: `/project-workflow <task description>`
The task description is: $ARGUMENTS

If no arguments provided, ask the user: "What are we building or fixing?"

---

## Phase -1: TRIAGE（智能分诊）

Before doing ANYTHING, evaluate the task on 3 dimensions. Output your assessment explicitly.

### Evaluate:

**1. Scale:**
- S (1-2 files, single module)
- M (3-10 files, multiple related files)
- L (10+ files, cross-module, new subsystem)

**2. Type:**
- bug_fix | feature | refactor | research | config

**3. Risk — does the task touch any of these?**
- Authentication, authorization, permissions
- API keys, secrets, credentials
- User input handling, command execution
- Code generation (skill_factory, eval, exec)
- External API calls, network requests

If YES to any → mark `security-required`

### Route to Path:

```
S + (bug_fix | config)           → Path S (lightweight)
M or (feature | refactor)        → Path M (standard)
L or research or unknown-tech    → Path L (full)
```

### Output format:

> **TRIAGE**: Scale=[S/M/L], Type=[type], Security=[yes/no] → **Path [S/M/L]**
> Activating: [list of skills that will be used]

### Always-On (all paths):
- Ruff hook (automatic via PostToolUse)
- python-review (any code change)
- verification-before-completion (any task completion)
- security-review (when security-required)
- docs / Context7 (when external library involved)

---

## Phase 0: DISCOVER（研究 + 设计）

> **Skip if Path S.**

### Path M: Brainstorm only

Invoke: `superpowers:brainstorming`
- Explore requirements, constraints, success criteria
- Keep it focused, 2-3 questions max

After brainstorm produces a design:

> **深度思考并优化：**
> - 需求理解对不对？遗漏了什么？
> - 有没有更优解？
> - 这个方案是最简的吗？

→ Get user approval on design.

### Path L: Brainstorm + Research in parallel

Launch in parallel:
1. `superpowers:brainstorming` — requirements exploration
2. `everything-claude-code:deep-research` — investigate existing solutions, papers, community practices
3. `everything-claude-code:docs` / Context7 — look up library APIs if external deps involved

Synthesize findings into design.

> **深度思考并优化：**
> - 需求理解对不对？遗漏了什么？
> - 有没有更优解？
> - research 发现了什么可以直接复用的？

→ Get user approval on design.

---

## Phase 1: PLAN（第一性原则 + 计划 + 可行性验证）

> **Skip if Path S.**

### Step 1: First Principles Decomposition

Before writing any plan, answer these questions explicitly:

> **第一性原则分解：**
> 1. 这个问题的本质约束是什么？（物理限制、API 限制、硬件限制）
> 2. 不可绕过的技术限制是什么？
> 3. 从零开始，如果没有任何现有代码，最优解应该长什么样？
> 4. 现有代码中哪些部分已经接近最优，哪些需要改？

> **思考并优化：**
> - 上面的分解是否触及了真正的约束，还是在重复表面假设？

### Step 2: Write the Plan

For Path M: Invoke `superpowers:writing-plans`
For Path L: Invoke `superpowers:writing-plans` AND `planning-with-files:plan` (互补)

- writing-plans → implementation steps with exact file paths and code
- planning-with-files → task_plan.md / findings.md / progress.md for tracking

Also invoke `everything-claude-code:search-first` to check if existing libraries/tools solve part of the problem before writing custom code.

> **深度思考并优化：**
> - 方案是从第一性原则推导的，还是在抄现有方案？
> - 步骤分解合理吗？依赖关系对吗？
> - 有没有过度设计或遗漏？

### Step 3: Feasibility Validation

For each critical step in the plan, verify:

> **可行性验证：**
> 1. 技术上能实现吗？有没有 API 限制、硬件约束？
> 2. 有没有隐含的依赖没列出来？
> 3. 最大风险点在哪？如果失败怎么回退？
> 4. 有没有因为惯性思维多加了不必要的步骤？

> **深度思考并优化：**
> - 这个计划真的可行吗？
> - 哪一步最可能出问题？

→ Get user approval on plan.

---

## Phase 2: BUILD（构建 + 实时质量）

### For each task in the plan:

**1. TDD: Write tests first**
Invoke: `everything-claude-code:tdd` or `everything-claude-code:python-testing`

> **思考并优化：** 测试覆盖够吗？边界情况考虑了吗？

**2. Implement → GREEN**
Write minimal code to pass the tests.
[Ruff hook auto-formats on save — automatic]

> **思考并优化：** 有更简洁的写法吗？

**3. Review**
Invoke: `everything-claude-code:python-review` (agent)

> **思考并优化：** review 发现的问题都修了吗？

**4. Security (if security-required)**
Invoke: `everything-claude-code:security-review` (agent)

> **思考并优化：** 安全问题有没有漏掉的？

**5. Test gate**
Run: `python -m pytest tests/ -q`
PASS → mark task done, update progress.md
FAIL → fix and re-run

### Parallel execution (Path L only):
If plan has independent tasks, use `superpowers:subagent-driven-development` to dispatch parallel worktree agents, each executing the above flow independently.

---

## Phase 3: VERIFY（全局验证）

Run these in parallel:

1. **Full test suite**: `python -m pytest tests/ -v`
2. **Code review**: `superpowers:requesting-code-review` — review against the original plan
3. **Security scan** (if security-required): `everything-claude-code:security-scan`

> **思考并优化：** review 和 scan 发现的问题都修完了吗？

Then invoke: `superpowers:verification-before-completion`
- MUST run actual commands and show real output
- NO claiming "done" without evidence

> **深度思考并优化：**
> - 整体一致性如何？
> - 有没有遗留问题？
> - 改动的副作用想清楚了吗？

For long sessions, run `everything-claude-code:context-budget` to check context health.
Update progress.md with verification results.

---

## Phase 4: SHIP（交付 + 学习）

**1. Commit**
Invoke: `superpowers:finishing-a-development-branch`
- Commit (不自动 push，等用户要求)
- Or create PR if on feature branch

> **思考并优化：** commit message 准确反映了改动吗？

**2. Update tracking**
Update progress.md to mark completion.

**3. Learn (Path L only)**
Invoke: `everything-claude-code:learn-eval`
- Extract reusable patterns from this session

> **深度思考并优化：**
> - 提取的模式真的通用吗？
> - 这次改动的长期影响？
> - 下次做类似任务，哪里可以更快？

---

## Reflection System Summary

| Level | When | Focus |
|-------|------|-------|
| **深度思考并优化** (6x) | Design spec, plan review, feasibility, final verify, ship, learn | Direction, optimality, long-term impact |
| **思考并优化** (8x) | Research, first-principles, tests, implementation, reviews, security, verify-aggregate, commit | Completeness, correctness of current step |

---

## Quick Reference: Skill Sources

| Skill | From |
|-------|------|
| brainstorming, writing-plans, executing-plans, subagent-driven-development, requesting-code-review, verification-before-completion, finishing-a-development-branch | Superpowers |
| tdd, python-testing, python-review, security-review, security-scan, deep-research, search-first, docs, learn-eval, context-budget, aside | ECC |
| plan (task_plan.md/findings.md/progress.md) | Planning-with-Files |
| Context7 (resolve-library-id, query-docs) | MCP |
| Ruff auto-format | Hook (PostToolUse) |
```

- [ ] **Step 3: Verify skill is discoverable**

Run:
```bash
claude -p "list available skills that contain 'workflow'" --allowedTools "Skill"
```

Or start a new session and type `/project-workflow test` to verify it loads.

- [ ] **Step 4: Smoke test — Path S**

In a new Claude Code session in the jarvis project:
```
/project-workflow fix a typo in config.yaml comments
```

Expected: TRIAGE outputs `Scale=S, Type=config, Security=no → Path S`, skips Phase 0 and 1, goes straight to BUILD.

- [ ] **Step 5: Smoke test — Path M**

```
/project-workflow add a new skill that queries weather API
```

Expected: TRIAGE outputs `Scale=M, Type=feature, Security=no → Path M`, runs brainstorm (short), then writing-plans, then TDD build.

---

### Task 4: Create Reference Doc for the Skill

**Files:**
- Create: `~/.claude/skills/project-workflow/reference.md`

- [ ] **Step 1: Write reference doc**

Create `~/.claude/skills/project-workflow/reference.md` with the full skill activation matrix, triage decision tree, and parallel strategies. This is loaded on demand to save context when the main SKILL.md is sufficient.

```markdown
# Project Workflow Reference

## Skill Activation Matrix

|                              | Path S | Path M | Path L |
|------------------------------|--------|--------|--------|
| brainstorm                   | -      | yes    | yes    |
| deep-research                | -      | -      | yes    |
| docs / Context7              | ?      | ?      | ?      |
| search-first                 | -      | ?      | yes    |
| writing-plans                | -      | yes    | yes    |
| planning-with-files          | -      | -      | yes    |
| tdd-workflow / python-testing| yes    | yes    | yes    |
| python-review                | yes    | yes    | yes    |
| security-review              | ?      | ?      | ?      |
| subagent / worktree          | -      | -      | yes    |
| code-reviewer                | -      | yes    | yes    |
| security-scan                | -      | -      | yes    |
| verification-before-done     | yes    | yes    | yes    |
| context-budget               | -      | -      | yes    |
| learn-eval                   | -      | -      | yes    |
| aside                        | ?      | ?      | ?      |

yes = always | ? = conditional | - = skip

## Parallel Strategies

Phase 0: brainstorm || deep-research || docs-lookup (up to 3-way)
Phase 1: writing-plans || planning-with-files (2-way)
Phase 2: task-A(worktree) || task-B(worktree) || task-C (N-way)
Phase 3: pytest || code-review || security-scan (3-way)

## Adapting to Other Languages

Replace per-language tools:
- Python: ruff hook + python-review + python-testing
- Go: gofmt hook + go-review + go-test
- Rust: rustfmt hook + rust-review + rust-test
- TypeScript: prettier hook + typescript-reviewer + tdd-workflow
- Java: google-java-format hook + java-review + springboot-tdd
- Kotlin: ktlint hook + kotlin-review + kotlin-test
- C++: clang-format hook + cpp-review + cpp-test
```

- [ ] **Step 2: Commit the skill**

```bash
cd ~ && git -C .claude add skills/project-workflow/SKILL.md skills/project-workflow/reference.md
```

Note: ~/.claude may not be a git repo. If not, skip this step — the files are already in place.

---

### Task 5: Update Memory

**Files:**
- Modify: `~/.claude/projects/-Users-alllllenshi-Projects-jarvis/memory/MEMORY.md`

- [ ] **Step 1: Add reference memory for the workflow skill**

Add to MEMORY.md:
```markdown
- [Workflow Skill](reference_workflow_skill.md) — /project-workflow skill 在 ~/.claude/skills/project-workflow/
```

- [ ] **Step 2: Create memory file**

Create `reference_workflow_skill.md`:
```markdown
---
name: Workflow Skill Location
description: Universal project workflow skill installed at ~/.claude/skills/project-workflow/
type: reference
---

/project-workflow skill 安装在 ~/.claude/skills/project-workflow/SKILL.md
- 5 阶段: TRIAGE → DISCOVER → PLAN → BUILD → VERIFY → SHIP
- 整合 Superpowers + ECC + Planning-with-Files
- 设计 spec: docs/superpowers/specs/2026-04-05-universal-project-workflow-design.md
- Ruff hook: .claude/settings.local.json (project-level)
- Context7 MCP: 全局配置
```

---

## Self-Review Checklist

1. **Spec coverage**: TRIAGE ✓, Phase 0 (DISCOVER) ✓, Phase 1 (PLAN + first principles + feasibility) ✓, Phase 2 (BUILD + TDD + parallel) ✓, Phase 3 (VERIFY) ✓, Phase 4 (SHIP + learn) ✓, reflection system (6 deep + 8 normal) ✓, skill activation matrix ✓, parallel strategies ✓, local skill creation ✓
2. **Placeholder scan**: No TBD/TODO. All steps have concrete commands or file contents.
3. **Type consistency**: Skill names consistent between spec and SKILL.md (`python-review`, `security-review`, `tdd`, `deep-research`, etc.)
4. **Feasibility**: Context7 is a standard MCP install. Ruff hook uses documented PostToolUse API. SKILL.md follows standard skill format. All referenced skills (Superpowers, ECC, P-w-F) are already installed.
