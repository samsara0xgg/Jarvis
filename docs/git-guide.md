# Git 工作流完整指南

给未来的 Allen + CC 查阅。CLAUDE.md 只保留硬规则，细节和示例看这里。

## 1. 日常 loop

1. 改代码 → `python -m pytest tests/ -q` → 通过才 commit
2. `git status` + `git diff --stat` 看一眼改动
3. 一件事一个 commit（fix 不混 feat，refactor 不夹新功能）
4. 不主动 push，不 `--no-verify`，不 force push 到 main

## 2. Commit 消息格式

Conventional Commits: `type(scope): English description`

| type | 用法 | 例 |
|---|---|---|
| `feat` | 新功能 | `feat(tts): add MiniMax emotion mapping` |
| `fix` | bug 修复 | `fix(observer): Gemini fallback endpoint` |
| `refactor` | 重构（行为不变）| `refactor(memory): split MemoryManager` |
| `test` | 只改测试 | `test(interrupt): add soft-stop regression` |
| `docs` | 文档 / 笔记 | `docs(notes): 2026-04-17 session` |
| `chore` | 杂项（依赖 / gitignore / 清理）| `chore(cleanup): drop obsolete tests` |
| `perf` | 性能 | `perf(asr): preheat model, save 200ms` |
| `data` | 数据文件 | `data(bench): add observer_cn fixtures` |

常见 scope：`tts` `asr` `memory` `desktop` `bench` `observer` `router` `interrupt` `vad` `personality` `wake`。

**规则**：
- 标题和 body 都用英文（Allen 偏好，2026-04-17）
- 标题 ≤ 72 字符
- body 写**为什么**不是**做了什么**（diff 会告诉你做了什么）
- 不加 `Co-Authored-By`

## 3. 分支

- 日常开发 → 直接 `main`
- 大重构 / 实验 / 可能失败 → `feat/xxx` branch，完工 merge 回 main
- `push origin main` 正常推，**禁止** `push --force` 到 main

## 4. gitignore 原则

加新路径前自问：
- **runtime 产物**（logs / cache / runs / build）→ ignore
- **本地机密**（`.env` / api key / credentials）→ ignore
- **可重新生成**（node_modules / `__pycache__` / `*.pyc`）→ ignore
- **> 10 MB 二进制**（模型 / 数据集）→ ignore，入库用 git-lfs

新类别时在 `.gitignore` 注释里写**为什么**忽略（以后自己会忘）。

## 5. 救命 cheatsheet

```bash
# 撤最后一次 commit（未 push），保留改动
git reset --soft HEAD~1

# 扔掉改动（无法恢复，谨慎）
git restore <file>

# 改上一条 commit 消息（仅限未 push）
git commit --amend

# 拆 commit：交互式挑 hunk 进 stage
git add -p

# 看改动
git status
git diff --stat
git diff <file>

# 看历史
git log --oneline -20
git log --stat <file>         # 某文件历史
git blame <file>              # 每行谁改的
```

## 6. 不要做

- `git push --force` 到 `main`
- `git add .` / `git add -A`（易误提交 .env / 大文件，用具体文件名）
- `git commit -am` 混提交多件事
- 提交机密、模型文件、runtime 产物
- `--no-verify` 跳过 pre-commit hook

## 7. 出事了怎么办

- **已 commit 但发现机密泄露**：立刻告诉 Allen，不要自己 rewrite history；可能需要 rotate key + force push（仅这一种情况允许）
- **pre-commit hook fail**：修好 underlying issue，重新 stage，再 commit 一次（**不要** `--amend`——那 commit 根本没发生，amend 会改动上一条）
- **merge conflict**：手动解决，不要 `checkout --` 或 `reset --hard` 偷懒
- **不认识的文件 / 分支**：先调查再删，可能是另一个 session 的 in-progress 工作
