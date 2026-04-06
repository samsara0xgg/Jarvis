# ActionResponse 设计记录 — 2026-04-02

## 状态：待验证（Allen 不确定是否想要这个方案）

## 当前实现

`core/local_executor.py` 所有 execute_* 方法返回 `ActionResponse(action, text)`：

- **RESPONSE** — 直接 TTS 播报，不过 LLM，零随机
  - 开灯/关灯 → "好的，灯开了。"
  - 几点了 → "现在是8点30分。"
  - 自动化操作 → "好的，晚安模式已创建。"

- **REQLLM** — 把原始数据交给 LLM，让小月用自己的话转述
  - 新闻/股票/天气 → skill 返回数据 → LLM 用小月语气说给用户

`jarvis.py` 根据 action 类型分流：
- RESPONSE → 直接 TTS
- REQLLM → 发给 LLM，prompt 是"用你自己的话简短转述以下信息给用户：{data}"

## 解决的问题

之前所有本地处理的请求，成功后用 Groq 路由器随手生成的 response，每次措辞不同。
现在确定性操作给固定文本，只有需要"说人话"的数据才过 LLM。

## 待确认

- RESPONSE 的固定文本会不会太死板？要不要允许一定程度的变化？
- REQLLM 的转述 prompt 是否足够好？是否需要更严格的格式控制？
- 这个方案跟"云端只提供知识，小月本地转述"的长期目标是否匹配？
