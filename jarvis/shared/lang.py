"""The one zh + en table: every fixed sentence Jarvis says or shows on its own.

Jarvis answers in the user's language. What a model writes follows the
user's words; what the runtime writes itself (confirmation asks, Tier 0
replies, spoken time and date, limitation and error lines, report headings,
date markers, GPT-Live's persona and appends) is looked up here by key, in
the language picked by ``language`` in ``settings.yaml`` (default: the
system language; the composition root calls :func:`set_language`, and the
desktop settings switch calls it again at run time).

Text for models is English and lives with its caller, telling the model to
answer in the user's language. Input matchers (regexes, word lists,
punctuation sets) read what the user said and keep their Chinese forms next
to the English ones. ``tests/canary/test_canary_cjk_only_in_lang_table.py``
holds every other Chinese string literal in ``jarvis/`` to zero.

Adding a sentence: one key, both languages. A missing key or language is a
``KeyError`` at the call site, never a silent fallback.

Layer rules: stdlib only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, get_args

if TYPE_CHECKING:
    from datetime import date, datetime

Language = Literal["zh", "en"]
LANGUAGES: Final[tuple[Language, ...]] = get_args(Language)

_current: Language = "zh"


def normalize(code: object) -> Language | None:
    """``zh`` / ``zh-Hans-CA`` / ``zh_CN`` -> ``zh``; ``en-CA`` -> ``en``; else None."""
    if not isinstance(code, str):
        return None
    head = code.strip().lower().replace("_", "-").split("-")[0]
    return head if head in LANGUAGES else None


def set_language(code: str) -> Language:
    """Make ``code`` the language of every lookup from now on."""
    global _current  # noqa: PLW0603 — the one process-wide language switch.
    lang = normalize(code)
    if lang is None:
        msg = f"language must be one of {', '.join(LANGUAGES)}, got {code!r}"
        raise ValueError(msg)
    _current = lang
    return lang


def language() -> Language:
    """The language fixed text is written in right now."""
    return _current


def language_name(lang: Language | None = None) -> str:
    """The language's English name, for telling a model what to write in."""
    return {"zh": "Simplified Chinese", "en": "English"}[lang or _current]


def say_voice() -> str:
    """The macOS ``say`` voice for the current language (the no-network fallback)."""
    return {"zh": "Tingting", "en": "Samantha"}[_current]


def t(key: str, /, *, lang: Language | None = None, **fields: object) -> str:
    """The sentence ``key`` in ``lang`` (default: the current language), formatted."""
    text = TEXT[key][lang or _current]
    return text.format(**fields) if fields else text


def variants(key: str, lang: Language | None = None) -> tuple[str, ...]:
    """Every wording of a sentence that has several (``VARIANTS``)."""
    return VARIANTS[key][lang or _current]


# --- What a confirmed tool does (ADR 0062) ----------------------------------

# (what the card and the ask call it, the line once it ran) per tool.
_ACTIONS: Final[dict[str, dict[Language, tuple[str, str]]]] = {
    "mcp__gmail__gmail_send": {"zh": ("发这封邮件", "已发送"), "en": ("send this email", "Sent")},
    "mcp__gmail__gmail_sendDraft": {"zh": ("发出这封草稿", "已发送"), "en": ("send this draft", "Sent")},
    "mcp__gmail__gmail_modify": {
        "zh": ("改这封邮件的标签", "标签已改"),
        "en": ("change this email's labels", "Labels changed"),
    },
    "mcp__gmail__gmail_batchModify": {
        "zh": ("改这些邮件的标签", "标签已改"),
        "en": ("change these emails' labels", "Labels changed"),
    },
    "mcp__gmail__gmail_modifyThread": {
        "zh": ("改这组邮件的标签", "标签已改"),
        "en": ("change this thread's labels", "Labels changed"),
    },
    "spawn_worker": {
        "zh": ("派这个后台任务", "已派出去"),
        "en": ("start this background task", "Started"),
    },
}


def spoken_tool_name(tool_name: str) -> str:
    """``mcp__notion__notion-fetch`` -> ``notion notion-fetch`` (spec §3.5.6 naming)."""
    parts = tool_name.split("__")
    return f"{parts[1]} {parts[2]}" if len(parts) == 3 and parts[0] == "mcp" else tool_name  # noqa: PLR2004


def action(tool_name: str, lang: Language | None = None) -> tuple[str, str]:
    """What ``tool_name`` does and the line once it ran; a tool not listed goes by its name."""
    lang = lang or _current
    named = _ACTIONS.get(tool_name)
    if named is not None:
        return named[lang]
    spoken = spoken_tool_name(tool_name)
    return (f"执行 {spoken}", f"已执行 {spoken}") if lang == "zh" else (f"run {spoken}", f"Ran {spoken}")


def letter_to(arguments: object) -> str | None:
    """The recipients when a tool call's arguments are a letter (to, subject, body), else None."""
    if not isinstance(arguments, dict):
        return None
    to, subject, body = arguments.get("to"), arguments.get("subject"), arguments.get("body")
    if not isinstance(subject, str) or not isinstance(body, str):
        return None
    if isinstance(to, list) and to and all(isinstance(one, str) for one in to):
        return ", ".join(to)
    return to if isinstance(to, str) and to else None


# --- Dates and times --------------------------------------------------------

_WEEKDAYS: Final[dict[Language, tuple[str, ...]]] = {
    "zh": ("周一", "周二", "周三", "周四", "周五", "周六", "周日"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}
_MONTHS_EN: Final = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
# (exclusive upper-bound hour, zh-CN day-period label), ascending.
_DAY_PERIODS_ZH: Final = ((6, "凌晨"), (9, "早上"), (12, "上午"), (13, "中午"), (18, "下午"), (24, "晚上"))
# Minutes below this take the spoken 零 filler (10点零2分).
_ZH_MINUTE_FILLER_BELOW: Final = 10


def weekday(day: date, lang: Language | None = None) -> str:
    """``周四`` / ``Thursday``."""
    return _WEEKDAYS[lang or _current][day.weekday()]


def month_day(day: date, lang: Language | None = None) -> str:
    """``9月24日`` / ``September 24``."""
    if (lang or _current) == "zh":
        return f"{day.month}月{day.day}日"
    return f"{_MONTHS_EN[day.month - 1]} {day.day}"


def day_marker(day: date, lang: Language | None = None) -> str:
    """The line that opens a day in the conversation history: ``[9月24日 周四]``."""
    if (lang or _current) == "zh":
        return f"[{month_day(day, 'zh')} {weekday(day, 'zh')}]"
    return f"[{weekday(day, 'en')}, {month_day(day, 'en')}]"


def spoken_date(moment: date, lang: Language | None = None) -> str:
    """``9月24日周四`` / ``Thursday, September 24``, for TTS."""
    if (lang or _current) == "zh":
        return f"{month_day(moment, 'zh')}{weekday(moment, 'zh')}"
    return f"{weekday(moment, 'en')}, {month_day(moment, 'en')}"


def spoken_time(moment: datetime, lang: Language | None = None) -> str:
    """The clock as it is said aloud: ``上午10点零2分`` / ``10:02 AM``.

    Chinese follows speech, not digits: minute 0 is 整 (上午10点整), minutes
    1-9 take the 零 filler (10点零2分), and hour 0 is 零点 (凌晨零点30分) —
    the naive 12-hour wrap would say 凌晨12点, which reads as noon.
    """
    hour, minute = moment.hour, moment.minute
    twelve = hour % 12 or 12
    if (lang or _current) == "en":
        suffix = "AM" if hour < 12 else "PM"  # noqa: PLR2004 — noon.
        return f"{twelve} {suffix}" if minute == 0 else f"{twelve}:{minute:02d} {suffix}"
    period = next(label for upper, label in _DAY_PERIODS_ZH if hour < upper)
    hour_label = "零" if hour == 0 else str(twelve)
    if minute == 0:
        return f"{period}{hour_label}点整"
    if minute < _ZH_MINUTE_FILLER_BELOW:
        return f"{period}{hour_label}点零{minute}分"
    return f"{period}{hour_label}点{minute}分"


# --- The table --------------------------------------------------------------

# Fixed replies never carry completion-class words (完成 / done / verified):
# they state what the runtime observed and nothing stronger.
TEXT: Final[dict[str, dict[Language, str]]] = {
    # ADR-0012 / ADR 0033 confirmation asks and answers.
    "confirm.ask_write": {
        "zh": "待确认：{tool_name} → `{canonical_target}`（{mode}，{content_bytes} 字节，风险 {risk_level}）。"
        "回复「可以」执行，「不要」取消。",
        "en": "To confirm: {tool_name} → `{canonical_target}` ({mode}, {content_bytes} bytes, risk"
        ' {risk_level}). Say "yes" to run it or "no" to cancel.',
    },
    # ADR 0062: the ask under a card is one spoken line; the card shows the rest.
    "confirm.ask_tool": {"zh": "要{action}吗？", "en": "Shall I {action}?"},
    "confirm.ask_letter": {
        "zh": "信写好了，发给 {to}，主题「{subject}」。要发吗？",
        "en": 'The email to {to} is ready, subject "{subject}". Send it?',
    },
    "confirm.rejected": {"zh": "好，不{action}了。", "en": "OK, I won't {action}."},
    "confirm.content_mismatch": {
        "zh": "暂存内容校验失败，写入未执行。",
        "en": "The staged content failed its check; nothing was written.",
    },
    "confirm.tool_gone": {
        "zh": "无法执行：工具已不可用，未执行。",
        "en": "Can't run it: the tool is no longer available. Nothing was run.",
    },
    "confirm.reproposal_refused": {
        "zh": "已取消：重新检查未通过（{outcome}），未执行。",
        "en": "Cancelled: the recheck did not pass ({outcome}). Nothing was run.",
    },
    "confirm.dispatch_error": {"zh": "执行出错：{error}", "en": "It failed: {error}"},
    "confirm.write_ran": {
        "zh": "write_file 已执行：`{path}`（{bytes_written} 字节）",
        "en": "write_file ran: `{path}` ({bytes_written} bytes)",
    },
    "confirm.tool_ran": {"zh": "{done}。", "en": "{done}."},
    "confirm.stale": {
        "zh": "这张卡已经处理过或被换掉了，没有执行。",
        "en": "That card was already handled or replaced; nothing ran.",
    },
    "confirm.accepted": {
        "zh": "该确认已接纳。执行状态请以结果为准。",
        "en": "Confirmation accepted. The result will show whether it ran.",
    },
    # Tier 0 (spec §17): the fixed-path errors, then one reply per row
    # (``template`` in config/tier0_patterns.yaml names the key).
    "tier0.gate_refused": {
        "zh": "这条指令被 Pre-action Gate 拦下，未执行。",
        "en": "The Pre-action Gate stopped this command; it did not run.",
    },
    "tier0.tool_error": {
        "zh": "这条指令执行出错，未产生结果。",
        "en": "This command failed and produced no result.",
    },
    "tier0.template_broken": {
        "zh": "指令 {pattern_id} 已执行，但响应模板变量缺失。",
        "en": "Command {pattern_id} ran, but its reply is missing a value.",
    },
    "tier0.time_now": {"zh": "现在是{spoken_time}。", "en": "It's {spoken_time}."},
    "tier0.date_today": {"zh": "今天是{spoken_date}。", "en": "Today is {spoken_date}."},
    "tier0.read_clipboard": {"zh": "剪贴板里是：{content}。", "en": "Your clipboard says: {content}"},
    "tier0.screen_look": {"zh": "屏幕上：{description}。", "en": "On your screen: {description}"},
    "tier0.opening": {"zh": "正在打开{opened_name}。", "en": "Opening {opened_name}."},
    "tier0.opening_in_vscode": {
        "zh": "正在用 VS Code 打开{opened_name}。",
        "en": "Opening {opened_name} in VS Code.",
    },
    "tier0.note_captured": {"zh": "已录入。", "en": "Noted."},
    "tier0.note_list": {"zh": "{rendered}", "en": "{rendered}"},
    "tier0.night": {"zh": "{spoken}", "en": "{spoken}"},
    "memo.none": {"zh": "还没有备忘录。", "en": "No memos yet."},
    # ADR 0093: the night run's spoken lines; its cards show the times.
    "night.started": {
        "zh": "好，挂到{until}。{seconds}秒后熄屏，晚安。",
        "en": "Okay, it keeps running until {until}. The screen goes off in {seconds} seconds."
        " Good night.",
    },
    "night.started_unguarded": {
        "zh": "好，挂到{until}，{seconds}秒后熄屏。不过没拿到防睡，Mac 可能会自己睡着。",
        "en": "Okay, until {until}; the screen goes off in {seconds} seconds. I couldn't keep"
        " the Mac awake, so it may fall asleep.",
    },
    "night.already": {"zh": "已经在挂着了，到{until}。", "en": "It's already running until {until}."},
    "night.ended": {"zh": "好，不挂了。", "en": "Okay, the night run is over."},
    "night.ended_restored": {
        "zh": "好，不挂了，亮度和声音调回来了。",
        "en": "Okay, the night run is over; brightness and sound are back.",
    },
    "night.ended_brightness": {
        "zh": "好，不挂了，亮度调回来了。",
        "en": "Okay, the night run is over; brightness is back.",
    },
    "night.ended_volume": {
        "zh": "好，不挂了，声音调回来了。",
        "en": "Okay, the night run is over; the sound is back.",
    },
    "night.cancelled": {"zh": "好，不挂了，屏幕和声音都没动。", "en": "Okay, cancelled. Nothing was changed."},
    "night.none": {"zh": "现在没在挂机。", "en": "No night run is on."},
    "night.unavailable": {
        "zh": "夜间挂机要后台在跑时才能开。",
        "en": "A night run needs the background service running.",
    },
    # Action terminal limitations (B-0003c, ADR-0008 D9).
    "limitation.timeout": {"zh": "Codex 超时，未完成", "en": "Codex timed out before finishing"},
    "limitation.failed": {"zh": "Codex 跑挂了，没新 diff", "en": "Codex crashed with no new diff"},
    "limitation.cancelled": {"zh": "任务已停止，未完成", "en": "The task was stopped before finishing"},
    # GPT-Live (ADR-0016): the persona in the official template's fixed labels
    # (https://developers.openai.com/api/docs/guides/live-prompting: written in the language the
    # model speaks), then the appends. Backend rules and permissions stay in the
    # backend; only ADR-0016 D6's read-only lookups are listed.
    "live.instructions": {
        "zh": "You are {assistant}, 用户的私人语音助手。\n"
        "语言：默认自然、简短的中文口语；用户说英文时切换到英文。\n"
        "始终使用中国大陆标准普通话回答。发音自然、清晰，不带英语口音；使用普通话声调和中文语流。\n"
        "英文缩写和数字按照中国大陆常见读法朗读。\n"
        "节奏：像面对面聊天，一次只说一两句，不长篇大论，不重复解释。\n"
        'Backchannel policy: Use moderate backchannels. 简短的"嗯""好"即可。\n'
        "Interruption policy: Stop speaking when the user interrupts. Listen to what they say.\n"
        '被要求"别说了"时立刻停下，等用户再开口再回应。\n'
        "Delegation policy:\n"
        "Backend tools:\n"
        "- 后台只能查，不能做：搜网页、读网页、查笔记、查过去的对话记录、看当前时间。\n"
        "Delegate to the backend when:\n"
        "- 用户要查资料、查最新或动态信息、回忆以前说过的事、要一个需要核实的事实。\n"
        "Do not delegate to the backend when:\n"
        "- 闲聊、寒暄、你自己就能答的常识；用户要执行操作时直接说明这一版后台只能查不能做。\n"
        "Do not guess the result while waiting. 等后台结果时可以继续聊别的，\n"
        "但不要编造查询结果，也不要说已经查到了。\n",
        "en": "You are {assistant}, the user's personal voice assistant.\n"
        "Language: speak natural, short, conversational English by default; when the user"
        " speaks another language, switch to it.\n"
        "Pace: talk as in a face-to-face chat, one or two sentences at a time; no long"
        " speeches, no repeated explanations.\n"
        'Backchannel policy: Use moderate backchannels. A short "mm-hm" or "okay" is enough.\n'
        "Interruption policy: Stop speaking when the user interrupts. Listen to what they say.\n"
        "When asked to stop talking, stop at once and wait until the user speaks again.\n"
        "Delegation policy:\n"
        "Backend tools:\n"
        "- The backend can only look things up, not act: search the web, read web pages,"
        " search notes, look up past conversations, check the current time.\n"
        "Delegate to the backend when:\n"
        "- The user wants something looked up, wants current or changing information, wants"
        " to recall something said before, or wants a fact that needs checking.\n"
        "Do not delegate to the backend when:\n"
        "- Small talk, greetings, or general knowledge you can answer yourself; when the user"
        " wants an action taken, say plainly that this version's backend can only look"
        " things up.\n"
        "Do not guess the result while waiting. You can keep talking about other things,\n"
        "but do not make up a result and do not say it has been found.\n",
    },
    "live.no_request": {
        "zh": "后台没有捕获到要查的内容，请让用户再说一遍要查什么。",
        "en": "The backend did not catch what to look up; ask the user to say again what to"
        " look up.",
    },
    "live.looking_up": {
        "zh": "正在查：{request}。还没有结果，不要猜。",
        "en": "Looking up: {request}. No result yet; do not guess.",
    },
    "live.failed": {
        "zh": "刚才那个查询失败了，后台没有拿到结果。",
        "en": "That lookup failed; the backend got no result.",
    },
    "live.no_backend": {
        "zh": "这个会话没有接后台，查不了。",
        "en": "This session has no backend, so nothing can be looked up.",
    },
    "live.timeout": {
        "zh": "刚才那个查询还没拿到结果，拿到后再说。",
        "en": "That lookup has no result yet; it will come once it arrives.",
    },
    "live.long_result": {
        "zh": "查到了，但结果太长不适合口述，完整结果在界面上。",
        "en": "Found it, but the result is too long to say aloud; the full result is on screen.",
    },
    # ADR 0026: an outcome no session heard, told to the next one.
    "live.undelivered": {
        "zh": "上次连接关闭前没来得及说的结果。问的是：{request}。结果：{result}",
        "en": "A result the last session closed before telling. The question was: {request}."
        " The result: {result}",
    },
    "live.undelivered_failed": {
        "zh": "上次问的{request}，后台没有查到结果。",
        "en": "The backend found no result for the earlier question: {request}.",
    },
    # Plugin panel (ADR 0035 / 0038): why a plugin cannot be used, and the
    # fixed errors a connection shows (transport errors can hold credentials).
    "plugin.needs_connector_gateway": {
        "zh": "此插件依赖尚未接入的连接器网关",
        "en": "This plugin needs a connector gateway Jarvis does not have yet",
    },
    "plugin.nothing_supported": {
        "zh": "此插件没有 Jarvis 支持的工具或技能",
        "en": "This plugin has no tools or skills Jarvis supports",
    },
    "plugin.connection_unsupported": {
        "zh": "此插件的连接配置暂不受支持",
        "en": "This plugin's connection setup is not supported yet",
    },
    "plugin.has_symlinks": {
        "zh": "此插件包含需要手动检查的文件链接",
        "en": "This plugin contains file links that need a manual check",
    },
    "plugin.tool_name_clash": {
        "zh": "插件工具名称冲突，请检查连接配置",
        "en": "Two plugin tools have the same name; check the connection setup",
    },
    "plugin.not_connected": {
        "zh": "尚未接入，请连接或重新授权",
        "en": "Not connected yet; connect or sign in again",
    },
    "plugin.unknown": {
        "zh": "找不到这个插件，请从插件列表选择",
        "en": "Plugin not found; pick one from the plugin list",
    },
    "plugin.busy": {
        "zh": "请先完成或取消当前连接",
        "en": "Finish or cancel the current connection first",
    },
    "plugin.shutting_down": {"zh": "Jarvis 正在关闭", "en": "Jarvis is shutting down"},
    "plugin.request_replaced": {
        "zh": "连接请求已更新，请重新打开插件",
        "en": "The connection request changed; open the plugin again",
    },
    "plugin.no_pending_login": {
        "zh": "当前没有等待授权的页面",
        "en": "No sign-in page is waiting",
    },
    "plugin.unknown_operation": {"zh": "未知的插件操作", "en": "Unknown plugin operation"},
    "plugin.bad_credentials_input": {"zh": "凭证输入无效", "en": "The credentials are not valid"},
    "plugin.bad_approval": {"zh": "无效的操作审批设置", "en": "That approval setting is not valid"},
    "plugin.credentials_required": {
        "zh": "请填写连接所需的凭证",
        "en": "Fill in the credentials this connection needs",
    },
    "plugin.cannot_connect": {
        "zh": "无法连接服务，请检查网络、凭证或重新授权",
        "en": "Could not reach the service; check the network or credentials, or sign in again",
    },
    "plugin.login_unfinished": {
        "zh": "授权尚未完成，请重新连接",
        "en": "Sign-in did not finish; connect again",
    },
    "plugin.connect_failed": {
        "zh": "连接未完成，请检查网络、凭证或重新授权后重试",
        "en": "The connection did not finish; check the network or credentials, or sign in"
        " again, then retry",
    },
    "plugin.bad_request": {"zh": "无效的插件请求", "en": "Invalid plugin request"},
    # ADR 0023 work state: what the material left out (the model reads these,
    # and the dashboard shows them under uncertainties).
    "work.limit.windows": {
        "zh": "今天的窗口共 {total} 个，只列出时长最长的 {shown} 个",
        "en": "{total} windows today; only the {shown} longest are listed",
    },
    "work.limit.recent": {
        "zh": "最近窗口内有 {total} 条屏幕内容，只保留最新的 {shown} 条",
        "en": "{total} screen captures in the recent window; only the newest {shown} are kept",
    },
    "work.limit.recent_text": {
        "zh": "最近屏幕内容的文字超出预算，较早的条目只剩标题",
        "en": "Recent screen text went over its budget; older entries keep only their titles",
    },
    "work.limit.earlier": {
        "zh": "今天较早的屏幕内容涉及 {total} 个窗口，只列出最常见的 {shown} 个",
        "en": "Earlier screen content today spans {total} windows; only the {shown} most frequent"
        " are listed",
    },
    "timesink.unreadable": {
        "zh": "TimeSink 不可读：没有应用、窗口和屏幕数据",
        "en": "TimeSink is unreadable: no app, window or screen data",
    },
    "work.limit.state": {
        "zh": "状态事件共 {total} 条，只列出最后 {shown} 条",
        "en": "{total} state events; only the last {shown} are listed",
    },
    "work.limit.app_unavailable": {"zh": "TimeSink 应用来源不可用", "en": "TimeSink's app source is unavailable"},
    "work.limit.screen_unavailable": {
        "zh": "TimeSink 屏幕来源不可用",
        "en": "TimeSink's screen source is unavailable",
    },
    "work.limit.state_unavailable": {
        "zh": "TimeSink 状态事件来源不可用",
        "en": "TimeSink's state event source is unavailable",
    },
    "work.limit.related_screen": {
        "zh": "与问题相关的更早屏幕内容只取了最新的 {shown} 条",
        "en": "Only the newest {shown} earlier screen captures matching the question were taken",
    },
    "work.limit.records_unreadable": {
        "zh": "对话记录库不可读：没有对话材料",
        "en": "The conversation records are unreadable: no conversation material",
    },
    "work.limit.records": {
        "zh": "近 {hours} 小时的对话记录超过 {shown} 条，只保留最新的",
        "en": "More than {shown} conversation records in the last {hours} hours; only the newest"
        " are kept",
    },
    "work.limit.related_records": {
        "zh": "与问题相关的更早对话记录只取了最新的 {shown} 条",
        "en": "Only the newest {shown} earlier conversation records matching the question were"
        " taken",
    },
    "work.limit.records_text": {
        "zh": "对话记录的文字超出预算，较早的条目被截断",
        "en": "Conversation text went over its budget; older entries were cut",
    },
    "work.limit.todo": {
        "zh": "未完成待办共 {total} 条，只列出 {shown} 条（与问题相关的优先）",
        "en": "{total} open to-dos; only {shown} are listed (those matching the question first)",
    },
    "work.limit.knowledge": {
        "zh": "知识条目共 {total} 条，只列出 {shown} 条（与问题相关的优先）",
        "en": "{total} knowledge items; only {shown} are listed (those matching the question"
        " first)",
    },
    "work.limit.no_repos": {
        "zh": "没有配置被观察的 Git 仓库：没有 Git 活动数据",
        "en": "No watched Git repositories are configured: no Git activity data",
    },
    "work.downgraded": {
        "zh": "有 {count} 条结论缺少明确依据，已按推断处理",
        "en": "{count} conclusions lacked clear grounds and count as inferences",
    },
    "work.inferred": {"zh": "部分结论是推断，未经确认", "en": "Some conclusions are inferences, not confirmed"},
    "material.scope": {"zh": "材料范围：{limit}", "en": "Material scope: {limit}"},
    # Separators and quotes the report's composed lines are joined with.
    "sep.list": {"zh": "、", "en": ", "},
    "sep.items": {"zh": "、", "en": "; "},
    "sep.clause": {"zh": "；", "en": "; "},
    "quote": {"zh": "「{text}」", "en": '"{text}" '},
    # ADR 0024 / 0028 daily work report: the saved text. A heading here is also
    # what the report's reader looks for, in both languages
    # (state/daily_report.summary_section), so a report saved in either reads.
    "report.title": {"zh": "# 工作日报 {day}（{zone}）", "en": "# Work report {day} ({zone})"},
    "report.generated": {
        "zh": "生成于 {at}；证据窗口 {start} 到 {end}{partial}；模型 {model}。",
        "en": "Generated {at}; evidence window {start} to {end}{partial}; model {model}.",
    },
    "report.partial": {"zh": "，这一天尚未结束", "en": ", the day is not over yet"},
    "report.h.summary": {"zh": "## 核心摘要", "en": "## Summary"},
    "report.h.items": {"zh": "## 工作事项", "en": "## Work items"},
    "report.h.decisions": {"zh": "## 重要决定与方案变化", "en": "## Decisions and changes of plan"},
    "report.h.open": {"zh": "## 未完成、阻碍与待确认", "en": "## Unfinished, blocked and to confirm"},
    "report.h.next": {"zh": "## 用户明确表达的下一步", "en": "## Next steps the user stated"},
    "report.h.suggestions": {
        "zh": "## 建议（模型提出，非用户承诺）",
        "en": "## Suggestions (from the model, not the user's commitments)",
    },
    "report.h.coverage": {"zh": "## 数据覆盖与不确定性", "en": "## Data coverage and uncertainty"},
    "report.h.sources": {"zh": "## 证据引用", "en": "## Sources"},
    "report.h.plan": {
        "zh": "## {day} 的日程与待办（微软日历与 To Do，生成时读取）",
        "en": "## Calendar and to-dos for {day} (Microsoft Calendar and To Do, read at generation)",
    },
    "report.item": {"zh": "### {index}. {title} — {status}", "en": "### {index}. {title} — {status}"},
    "report.no_items": {
        "zh": "- 材料中未能归并出明确的工作事项。",
        "en": "- No clear work items could be drawn from the material.",
    },
    "report.no_decisions": {
        "zh": "材料中未见明确的决定或方案变化。",
        "en": "No clear decisions or changes of plan in the material.",
    },
    "report.no_open": {
        "zh": "材料中未见明确的未完成事项或阻碍。",
        "en": "No clear unfinished items or blockers in the material.",
    },
    "report.no_next": {
        "zh": "材料中没有用户本人明确表达的下一步。",
        "en": "No next steps the user stated in the material.",
    },
    "report.none": {"zh": "- 无。", "en": "- None."},
    "report.refs": {"zh": "引用：{refs}", "en": "Sources: {refs}"},
    "report.refs_none": {"zh": "引用：无", "en": "Sources: none"},
    "report.bullet": {"zh": "{line}（{refs}）", "en": "{line} ({refs})"},
    "report.rationale": {"zh": "　理由：{rationale}", "en": " Reason: {rationale}"},
    "report.part": {"zh": "{part}：", "en": "{part}: "},
    "report.table_line": {"zh": "{index} {title}（{status}）", "en": "{index} {title} ({status})"},
    "report.where": {"zh": "第 {index} 项", "en": "Item {index}"},
    "report.where_part": {"zh": "第 {index} 项（{part}）", "en": "Item {index} ({part})"},
    "report.unproven_entry": {"zh": "{where}：{text}", "en": "{where}: {text}"},
    # Part statuses: the unchecked ones, then what a ruling or a check settles.
    "report.status.browsed": {"zh": "浏览", "en": "browsed"},
    "report.status.discussed": {"zh": "讨论", "en": "discussed"},
    "report.status.attempted": {"zh": "尝试/进行中", "en": "attempted / in progress"},
    "report.count.attempted": {"zh": "进行中 {count} 项", "en": "{count} in progress"},
    "report.count.discussed": {"zh": "讨论 {count} 项", "en": "{count} discussed"},
    "report.count.browsed": {"zh": "浏览 {count} 项", "en": "{count} browsed"},
    "report.claim.merged": {"zh": "已合并到 main（{commits}）", "en": "merged to main ({commits})"},
    "report.claim.invalid": {
        "zh": "声称完成，引用无效：{reason}",
        "en": "claimed done, citation invalid: {reason}",
    },
    "report.claim.self_report": {
        "zh": "据代理自述已完成，尚未核实（{keys}）",
        "en": "done by the agent's own account, not yet verified ({keys})",
    },
    "report.claim.unchecked": {"zh": "声称完成，未核实（{why}）", "en": "claimed done, not verified ({why})"},
    "report.claim.unsupported_shows": {
        "zh": "声称完成，引用不支持（原文显示：{shows}）",
        "en": "claimed done, the citation does not support it (the original shows: {shows})",
    },
    "report.claim.unsupported": {
        "zh": "声称完成，引用不支持",
        "en": "claimed done, the citation does not support it",
    },
    "report.claim.partial": {"zh": "部分完成：{shows}", "en": "partly done: {shows}"},
    "report.claim.user": {"zh": "用户确认完成（{keys}）", "en": "the user confirmed it done ({keys})"},
    "report.claim.committed": {"zh": "已提交（{commits}）", "en": "committed ({commits})"},
    "report.claim.page": {"zh": "页面显示已完成（{keys}）", "en": "a page shows it done ({keys})"},
    "report.claim.screen": {"zh": "屏幕显示已完成（{keys}）", "en": "the screen shows it done ({keys})"},
    "report.commits": {"zh": "提交 {shas}，{main}", "en": "commits {shas}, {main}"},
    "report.commits_mixed": {"zh": "提交 {commits}", "en": "commits {commits}"},
    "report.commit_main": {"zh": "{sha}（{main}）", "en": "{sha} ({main})"},
    "report.main.on": {"zh": "已在 main", "en": "on main"},
    "report.main.off": {"zh": "未进 main", "en": "not on main"},
    "report.main.unknown": {"zh": "main 未知", "en": "main unknown"},
    "report.unchecked.budget": {"zh": "核查预算耗尽", "en": "check budget used up"},
    "report.unchecked.failed": {"zh": "核查失败", "en": "check failed"},
    # Why the program ruled a completed part's citation invalid.
    "report.reason.no_keys": {"zh": "没有引用材料里的键", "en": "it cites no key from the material"},
    "report.reason.late_commit": {
        "zh": "引用的是当天才看到的旧提交，不是当天的工作",
        "en": "it cites an old commit first seen that day, not that day's work",
    },
    "report.reason.question": {
        "zh": "引用的是用户的提问或请求，不是确认",
        "en": "it cites the user's question or request, not a confirmation",
    },
    "report.reason.not_proof": {
        "zh": "引用的记录不能证明完成（Jarvis 自己的话或窗口时段）",
        "en": "the cited records cannot prove it done (Jarvis's own words or window time)",
    },
    "report.reason.not_on_main": {"zh": "提交 {shas} 未进 main", "en": "commits {shas} are not on main"},
    "report.reason.commit_not_deploy": {
        "zh": "提交不能证明部署、上线或重启发生了",
        "en": "a commit cannot prove a deployment, release or restart happened",
    },
    # Summary.
    "report.main_line_titles": {"zh": "这一天的主要事项：{titles}。", "en": "The day's main items: {titles}."},
    "report.main_line_none": {
        "zh": "这一天没有归并出工作事项。",
        "en": "No work items could be drawn from the day.",
    },
    "report.evidenced": {"zh": "有实证：{items}", "en": "Evidenced: {items}"},
    "report.unverified": {
        "zh": "声称完成但未核实或不成立：{items}",
        "en": "Claimed done but unverified or not upheld: {items}",
    },
    "report.rest": {"zh": "另有{items}，见工作事项。", "en": "Also {items}; see Work items."},
    # Data coverage and uncertainty.
    "report.scale": {
        "zh": "- 材料规模：窗口 {windows} 个、截屏 {screen} 条、对话记录 {records} 条、Git 提交 {git} 个、"
        "Codex 会话 {agent} 个、状态事件 {state} 条{observed}。没有记录不代表没有活动。",
        "en": "- Material: {windows} windows, {screen} screen captures, {records} conversation"
        " records, {git} Git commits, {agent} Codex sessions, {state} state events{observed}."
        " No record does not mean no activity.",
    },
    "report.last_observed": {"zh": "；最后观察到 {at}", "en": "; last observed {at}"},
    "report.not_verified": {
        "zh": "- 每个标为完成的部分都对照它引用的原文核查过，核查结论写在状态里；"
        "浏览/讨论/进行中是报告作者的判断，运行时不核实。",
        "en": "- Every part marked done was checked against the originals it cites, and the"
        " finding is its status; browsed / discussed / in progress are the author's judgment"
        " and are not verified.",
    },
    "report.unknown_refs": {
        "zh": "- 有 {count} 处引用不是材料里的键，已丢弃。",
        "en": "- {count} citations were not keys in the material and were dropped.",
    },
    "report.unproven": {
        "zh": "- 声称完成但没有实证的部分：{parts}。",
        "en": "- Parts claimed done without evidence: {parts}.",
    },
    "report.sha_unknown": {
        "zh": "- 第 {index} 项正文提到的提交号 {sha} 不在当天的提交里。",
        "en": "- Item {index} mentions commit {sha}, which is not among that day's commits.",
    },
    "report.sha_uncited": {
        "zh": "- 第 {index} 项正文提到提交 {sha}（{key}）但没有引用它。",
        "en": "- Item {index} mentions commit {sha} ({key}) but does not cite it.",
    },
    "report.moved_next": {
        "zh": "- 模型把 {count} 条内容当作用户明确表达的下一步，但引用的不是用户的原话，已从该节移除：{quoted}",
        "en": "- The model gave {count} entries as next steps the user stated, but they do not"
        " cite the user's own words, so they were removed from that section: {quoted}",
    },
    "report.sources": {
        "zh": "- 共 {count} 个来源，正文按编号引用；其中前 {kept} 个另存为 source_refs（字段上限）；"
        "用 read_activity / read_records 回查原文。",
        "en": "- {count} sources, cited by number in the text; the first {kept} are also saved as"
        " source_refs (the field's limit); look up the originals with read_activity /"
        " read_records.",
    },
    # The next day's calendar and To Do, written by the program (ADR 0036).
    "report.plan_unread": {"zh": "微软日历与待办：没有读取", "en": "Microsoft Calendar and To Do: not read"},
    "report.calendar": {"zh": "日程：", "en": "Calendar:"},
    "report.calendar_empty": {"zh": "- 日历上没有日程。", "en": "- Nothing on the calendar."},
    "report.todos_open": {"zh": "待办（未完成 {count} 条）：", "en": "To-dos ({count} open):"},
    "report.todos_none": {"zh": "- 没有未完成的待办。", "en": "- No open to-dos."},
    "report.todos_more": {"zh": "- 另有 {count} 条未列出，见 To Do。", "en": "- {count} more not listed; see To Do."},
    "report.todos_done": {"zh": "{day} 完成的待办：", "en": "To-dos done on {day}:"},
    "report.todo_done_line": {"zh": "- {title}（{list}）", "en": "- {title} ({list})"},
    "report.todo": {"zh": "- {title}（{list}，{state}{important}）", "en": "- {title} ({list}, {state}{important})"},
    "report.todo_important": {"zh": "，重要", "en": ", important"},
    "report.todo.done": {"zh": "已完成", "en": "done"},
    "report.todo.no_due": {"zh": "无截止日期", "en": "no due date"},
    "report.todo.overdue": {"zh": "已逾期，截止 {due}", "en": "overdue, due {due}"},
    "report.todo.due": {"zh": "截止 {due}", "en": "due {due}"},
    "report.event_where": {"zh": "（{location}）", "en": " ({location})"},
    "report.all_day": {"zh": "全天", "en": "all day"},
    # What each source held (the material's first lines and the report's coverage).
    "report.served.app": {
        "zh": "应用/窗口：{spans} 段、{windows} 个窗口，全部列出",
        "en": "Apps / windows: {spans} spans, {windows} windows, all listed",
    },
    "report.served.app_none": {"zh": "应用/窗口：这一天没有记录", "en": "Apps / windows: nothing recorded that day"},
    "report.served.screen": {
        "zh": "屏幕内容：采集 {total} 条，去掉同一窗口连续近似重复的 {folded} 条后 {kept} 条全文列出",
        "en": "Screen content: {total} captures; after folding {folded} near-repeats of the same"
        " window, {kept} listed in full",
    },
    "report.served.screen_none": {
        "zh": "屏幕内容：这一天没有采集到（截屏未运行或不可用）",
        "en": "Screen content: nothing captured that day (capture not running or unavailable)",
    },
    "report.served.records": {
        "zh": "对话记录：{count} 条，全文列出",
        "en": "Conversation records: {count}, listed in full",
    },
    "report.served.records_none": {"zh": "对话记录：这一天没有记录", "en": "Conversation records: none that day"},
    "report.served.records_unreadable": {
        "zh": "对话记录：记录库不可读",
        "en": "Conversation records: the store is unreadable",
    },
    "report.served.git": {
        "zh": "Git 提交：当天 {same} 个，另有 {late} 个当天才看到的旧提交，全部列出",
        "en": "Git commits: {same} that day, plus {late} older commits first seen that day, all"
        " listed",
    },
    "report.served.git_none": {"zh": "Git 提交：这一天没有提交", "en": "Git commits: none that day"},
    "report.served.git_no_repos": {
        "zh": "Git 提交：没有配置被观察的仓库，只有历史记录里观察到的提交",
        "en": "Git commits: no watched repositories are configured; only commits seen in the"
        " history",
    },
    "report.served.plan": {
        "zh": "微软日历与待办：{events} 个日程（{day} 与次日）、{open} 条未完成待办、{done} 条当天完成，全部给出",
        "en": "Microsoft Calendar and To Do: {events} events ({day} and the next day), {open} open"
        " to-dos, {done} done that day, all given",
    },
    "report.served.plan_missing": {
        "zh": "微软日历与待办：{why}，没有日程和待办",
        "en": "Microsoft Calendar and To Do: {why}; no events or to-dos",
    },
    "report.plan.not_connected": {"zh": "未接入", "en": "not connected"},
    "report.plan.read_failed": {"zh": "读取失败（{error}）", "en": "read failed ({error})"},
    "report.served.agent": {
        "zh": "代理会话：Codex {sessions} 个会话，{rows} 个逐轮全文列出{shims}",
        "en": "Agent sessions: {sessions} Codex sessions, {rows} listed turn by turn in full{shims}",
    },
    "report.served.agent_shims": {
        "zh": "，另 {count} 个是 Codex 自动生成的审批会话，只记数不列出",
        "en": ", plus {count} approval sessions Codex made itself, counted but not listed",
    },
    "report.served.agent_none": {
        "zh": "代理会话：这一天没有 Codex 会话",
        "en": "Agent sessions: no Codex sessions that day",
    },
    "report.served.agent_unreadable": {
        "zh": "代理会话：Codex 本机会话目录不可读",
        "en": "Agent sessions: the local Codex session folder is unreadable",
    },
    # What the material left out.
    "report.limit.unwatched": {
        "zh": "当天还观察到 {count} 个仓库的活动，但它们已不在被观察列表里，未列入：{repos}",
        "en": "{count} more repositories had activity that day but are no longer watched, so they"
        " are left out: {repos}",
    },
    "report.limit.dropped": {
        "zh": "Git 观察器追上积压时跳过了 {count} 个更早的提交，没有写进事件日志；当天的提交清单以本地仓库记录为准",
        "en": "The Git observer skipped {count} older commits while catching up and did not log"
        " them; the day's commit list comes from the local repositories",
    },
    "report.limit.unreadable_repos": {
        "zh": "无法读取 {count} 个仓库的本地 git 记录（路径不存在或不是仓库）：{repos}",
        "en": "Could not read the local git history of {count} repositories (path missing or not"
        " a repository): {repos}",
    },
    "report.limit.budget": {
        "zh": "材料超出模型容量（{budget} 字），{what}退到索引：{count} 条只保留开头 {chars} 字，"
        "共 {withheld} 字采集到了但没有给全，可用 search_material 检索、request_details 取原文",
        "en": "The material went over the model's capacity ({budget} characters), so {what} fell"
        " back to an index: {count} entries keep only their first {chars} characters, {withheld}"
        " characters captured but not given in full; search_material finds them and"
        " request_details fetches the originals",
    },
    "report.fallback.screen": {"zh": "屏幕内容", "en": "the screen content"},
    "report.fallback.records": {"zh": "对话记录", "en": "the conversation records"},
    "report.fallback.agent_answers": {
        "zh": "Codex 回复（每个会话最后一条除外）",
        "en": "the Codex replies (except each session's last)",
    },
    "report.fallback.agent_prompts": {"zh": "Codex 提问", "en": "the Codex prompts"},
    # ADR-0002 § Daemon / CLI contract: the ack printed before the fork.
    "cli.quick_ack": {"zh": "好的，跑起来了。", "en": "OK, it's running."},
    # Desktop panels.
    "codex.session_ended": {"zh": "会话已结束", "en": "Session ended"},
    "codex.turn_stopped": {"zh": "已停下", "en": "Stopped"},
    "codex.asks": {"zh": "Codex 问你：{question}", "en": "Codex asks: {question}"},
    "usage.window_hours": {"zh": "{hours} 小时", "en": "{hours} h"},
    "usage.window_days": {"zh": "{days} 天", "en": "{days} days"},
    "usage.window_week_total": {"zh": "7 天 · 总", "en": "7 days · total"},
    "usage.window_week_model": {"zh": "7 天 · {name}", "en": "7 days · {name}"},
    "usage.needs_admin_key": {"zh": "需要 Admin key", "en": "Needs an Admin key"},
    # ADR 0030: the answer request after the tool budget failed or said nothing.
    "tool_budget.exhausted": {
        "zh": "这一轮工具调用次数用完了，还没整理出答案。请把问题拆小一点再问一次。",
        "en": "I used up this turn's tool calls before I had an answer. Try asking a smaller"
        " part of the question.",
    },
    # Why a turn failed, one per llm.failure_reason; the desktop shows it in
    # place of an answer.
    "failure.missing_key": {
        "zh": "还没有填模型的 API 密钥，我没法回答。请在设置里填一个。",
        "en": "There is no API key for the model yet, so I can't answer. Add one in Settings.",
    },
    "failure.unauthorized": {
        "zh": "模型服务拒绝了这个密钥（401），可能填错了或已失效。请在设置里换一个。",
        "en": "The model service rejected the key (401). It may be mistyped or revoked;"
        " replace it in Settings.",
    },
    "failure.model_denied": {
        "zh": "这个密钥没有使用这个模型的权限。",
        "en": "This key is not allowed to use this model.",
    },
    "failure.quota": {
        "zh": "账户额度用完了，充值后再试。",
        "en": "The account is out of credit. Add credit, then try again.",
    },
    "failure.rate_limited": {
        "zh": "请求太频繁，被限流了。稍等一下再试。",
        "en": "Too many requests right now. Wait a moment, then try again.",
    },
    "failure.network": {
        "zh": "连不上模型服务，检查一下网络。",
        "en": "I can't reach the model service. Check the network.",
    },
    "failure.timeout": {
        "zh": "模型太久没有回应，这一轮放弃了。可以再说一次。",
        "en": "The model took too long to answer. Try again.",
    },
    "failure.error": {
        "zh": "这一轮出错了，没有完成。可以再说一次。",
        "en": "Something went wrong and this turn did not finish. Try again.",
    },
    # First-run setup: the line a picked voice says when previewed.
    "setup.preview": {
        "zh": "你好，我是 {assistant}。今天想先做点什么？",
        "en": "Hi, I'm {assistant}. What would you like to do first today?",
    },
    # The voices first-run setup offers, per language (runtime/settings.py
    # SETUP_VOICES): a name and a few words on how each sounds.
    "voice.Chinese (Mandarin)_Warm_Bestie": {"zh": "暖心闺蜜", "en": "Warm Bestie"},
    "voice.Chinese (Mandarin)_Warm_Bestie.note": {"zh": "温暖，清楚", "en": "warm, clear"},
    "voice.Chinese (Mandarin)_Sweet_Lady": {"zh": "甜美女声", "en": "Sweet Lady"},
    "voice.Chinese (Mandarin)_Sweet_Lady.note": {"zh": "温柔，甜", "en": "tender, sweet"},
    "voice.Chinese (Mandarin)_Mature_Woman": {"zh": "御姐音", "en": "Mature Woman"},
    "voice.Chinese (Mandarin)_Mature_Woman.note": {"zh": "成熟，有魅力", "en": "mature, charming"},
    "voice.Chinese (Mandarin)_Reliable_Executive": {"zh": "稳重精英", "en": "Reliable Executive"},
    "voice.Chinese (Mandarin)_Reliable_Executive.note": {
        "zh": "沉稳，可靠", "en": "steady, reliable",
    },
    "voice.Chinese (Mandarin)_Gentle_Youth": {"zh": "温和青年", "en": "Gentle Youth"},
    "voice.Chinese (Mandarin)_Gentle_Youth.note": {"zh": "轻松，像朋友", "en": "easy, like a friend"},
    "voice.English_radiant_girl": {"zh": "明亮女孩", "en": "Radiant Girl"},
    "voice.English_radiant_girl.note": {"zh": "活泼，明亮", "en": "lively, bright"},
    "voice.English_CalmWoman": {"zh": "舒缓女声", "en": "Calm Woman"},
    "voice.English_CalmWoman.note": {"zh": "平和，舒缓", "en": "soothing"},
    "voice.English_FriendlyPerson": {"zh": "友好男声", "en": "Friendly Guy"},
    "voice.English_FriendlyPerson.note": {"zh": "自然，像朋友", "en": "natural, like a friend"},
    "voice.English_Trustworth_Man": {"zh": "可靠男声", "en": "Trustworthy Man"},
    "voice.English_Trustworth_Man.note": {"zh": "浑厚，真诚", "en": "resonant, sincere"},
}

# Sentences with several wordings of one observed truth; the caller picks one
# stably (ADR-0008 D6 commentary, spoken in the language of the user's words).
VARIANTS: Final[dict[str, dict[Language, tuple[str, ...]]]] = {
    "commentary.dispatched": {
        "zh": ("这就去办。", "好，我来办。"),
        "en": ("On it.", "I'll take care of it."),
    },
    "commentary.running": {
        "zh": ("任务已经在运行。", "这件事正在做。", "还在跑着。"),
        "en": ("It's running now.", "That's in progress.", "Still working on it."),
    },
    "commentary.result_observed": {
        "zh": ("结果回来了，我整理一下。", "拿到结果了，我看一下。", "数据回来了，我过一遍。"),
        "en": (
            "The results are back, one moment.",
            "Got the results, let me look.",
            "The data is in, going through it.",
        ),
    },
    "commentary.failed": {
        "zh": ("这一步失败了，我告诉你具体原因。", "这一步没成，我说说原因。", "这里出错了，我讲一下怎么回事。"),
        "en": (
            "That step failed; I'll tell you why.",
            "That didn't work, here's why.",
            "Something went wrong there; let me explain.",
        ),
    },
    "commentary.lookup": {
        "zh": ("我查一下。", "我去看看。", "稍等，我查查。"),
        "en": ("Let me check.", "Looking it up.", "One sec, checking."),
    },
    "commentary.codex": {
        "zh": ("我让 Codex 去做。", "交给 Codex 去办。"),
        "en": ("I'll hand this to Codex.", "Passing this to Codex."),
    },
}
