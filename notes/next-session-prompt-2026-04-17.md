# 新 Session Prompt — 直接复制粘贴

---

# Jarvis Desktop 开发 — Session 续接（2026-04-17 之后）

## 上个 session 成就

把 Electron Pet Mode 从零做到能跑，连带修了两个跑了 13 天没人发现的后端 bug：
- Observer Gemini fallback 一直被发到 xAI 端点（400）
- Cerebras intent router 模型名错（404，merge 事故）

完整流程 / 踩坑 / 决策 / 遗留：读 `notes/desktop-electron-session-2026-04-17.md`
设计 spec：读 `docs/superpowers/specs/2026-04-16-jarvis-desktop-petmode-design.md`
Bug 总表：读 `notes/bugs.md`

## 当前 git 状态

- 已 commit：`80d80ef` (observer/cerebras) + `5f3e6ef` (desktop pet panel)
- 未 push（按规矩等明确指令）
- Working tree 大量 **M/D** 文件是上个 backend agent 的 WIP，未 commit，上个 session **没碰**

启动验证两条命令：
```bash
python -m ui.web.server           # 终端 1，必须先跑
cd desktop && npm start            # 终端 2
```

## 下一步建议（从容易到复杂）

1. **P2 修 "今天天气天气"重复字**（`notes/bugs.md` 2026-04-17 批次）
   - grep `"今天天气"\|"天气天气"` 在 `skills/weather.py` 和 `core/llm.py` info_query 模板
   - 顺带看 log 里 `requests.exceptions.HTTPError: 400` 是不是天气 API 侧问题
2. **决定 backend agent 的 WIP 怎么办**
   - 看 `git diff config.yaml core/*.py` 是否完整可用
   - 完整就 commit，半成品就按任务拆分再做
3. **GEMINI_API_KEY 刷新**（非必须，primary 活着时不走 fallback）
   - https://aistudio.google.com/apikey 生成新 key → 更新 `~/.zshrc`

## 规矩（`CLAUDE.md` 提要）

- 后端改动后跑 `python -m pytest tests/ -q`
- commit OK，**never push**（除非明说）
- 不改 `data/speechbrain_model/` 和 `data/sensevoice-small-int8/`
- 不硬编码 IP / API key / 路径，从 `config.yaml` 读
- 不用 `print`，用 `logging`
- Grep/Glob 优先于 spawn Agent

## 用户（Allen）沟通风格

- 答案先讲，探索靠后
- 不要长答（默认 1-3 段）
- 拍板前多问；拍板后执行
- 用中文回应，code/path 英文
- 不要自作主张砍掉讨论过的功能（上个 session 教训）
