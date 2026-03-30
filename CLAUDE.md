# Jarvis — 声纹驱动的 AI 语音助手

## 快速参考

```bash
# 运行
python jarvis.py --no-wake        # 开发（按键录音）
python jarvis.py                  # 生产（唤醒词 "Hey Jarvis"）
python main.py                    # 旧版确定性指令模式

# 测试
python -m pytest tests/ -v
python -m pytest tests/test_<module>.py -v  # 单模块

# 其他
python ui/dashboard.py            # Gradio Web UI
python setup_hue.py               # Hue Bridge 配对
python -m remote.agent            # Mac 远程控制 Agent
```

## 技术栈

Python 3.11 · venv/ · config.yaml 统一配置
ASR: Whisper (openai-whisper) · 声纹: SpeechBrain ECAPA-TDNN · LLM: OpenAI/Anthropic
TTS: edge-tts (备用 pyttsx3) · 唤醒词: Porcupine · UI: Gradio + OLED (luma.oled)
设备: Philips Hue (phue2) + MQTT (paho-mqtt) + 模拟 · 远程: WebSocket

## 架构

```
jarvis.py 主循环:
  麦克风 → wake_word → audio_recorder → [speaker_verifier + speech_recognizer 并行]
  → llm (技能调用) → tts → 喇叭
```

- `core/` — 录音、ASR、声纹、LLM、TTS、事件总线、调度器、自动化引擎
- `skills/` — LLM function calling 技能（继承 Skill ABC，在 SkillRegistry 注册）
- `devices/` — SmartDevice ABC → sim/ hue/ mqtt/ 三种实现，DeviceManager 统一调度
- `auth/` — 声纹注册 + JSON 存储 + 角色权限 (owner > family > member > guest)
- `memory/` — 对话历史 + 用户偏好
- `ui/` — Gradio 仪表盘 + OLED 显示（事件总线驱动状态切换）
- `remote/` — WebSocket 协议控制 Mac（agent.py 被控端，client.py 控制端）
- `esp32/` — MicroPython 固件模板（传感器节点 + 继电器节点）
- `deploy/` — Pi 部署（install.sh + systemd + mosquitto）

## 编码规范

- Type Hints 全覆盖 · Google style docstring
- `logging` 模块，不用 `print`
- 配置从 config.yaml 读，不硬编码路径/阈值/API key
- 异常处理完善：硬件不可用时优雅降级
- 数据库操作用 context manager
- 新技能继承 `skills.Skill`，实现 `name/description/parameters/execute`

## 关键文件

- `config.yaml` — 所有可调参数（音频、ASR、声纹、LLM、TTS、设备、OLED、MQTT、远程）
- `skills/__init__.py` — Skill ABC + SkillRegistry（新技能参考此接口）
- `devices/base_device.py` — SmartDevice ABC（新设备参考此接口）
- `core/event_bus.py` — 全局事件总线（模块间解耦通信）

## 不要做

- 不要修改 `data/speechbrain_model/` 下的模型文件
- 不要在代码中硬编码 IP、API key、文件路径
- 不要用 `print` 替代 `logging`
- 不要绕过 `permission_manager` 直接执行设备操作

## 代码修改原则

- 修改代码时做最小化的定向改动，不要从头重写文件
- 如果觉得需要换方案，先问用户，不要自行决定
- 用户说"简化"意味着在原代码基础上简化，不是用另一种方式重写

