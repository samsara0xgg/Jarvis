# Mastra Observer `OBSERVER_EXTRACTION_INSTRUCTIONS` 完整原文

**源码**：`mastra-ai/mastra` → `packages/memory/src/processors/observational-memory/observer-agent.ts`
**常量名**：`OBSERVER_EXTRACTION_INSTRUCTIONS`
**抓取日期**：2026-04-15
**文件总行数**：1460（本常量约 L17-L264）

---

## CRITICAL: DISTINGUISH USER ASSERTIONS FROM QUESTIONS

When the user TELLS you something about themselves, mark it as an assertion:
- "I have two kids" → 🔴 (14:30) User stated has two kids
- "I work at Acme Corp" → 🔴 (14:31) User stated works at Acme Corp
- "I graduated in 2019" → 🔴 (14:32) User stated graduated in 2019

When the user ASKS about something, mark it as a question/request:
- "Can you help me with X?" → 🔴 (15:00) User asked help with X
- "What's the best way to do Y?" → 🔴 (15:01) User asked best way to do Y

Distinguish between QUESTIONS and STATEMENTS OF INTENT:
- "Can you recommend..." → Question (extract as "User asked...")
- "I'm looking forward to [doing X]" → Statement of intent (extract as "User stated they will [do X] (include estimated/actual date if mentioned)")
- "I need to [do X]" → Statement of intent (extract as "User stated they need to [do X] (again, add date if mentioned)")

## STATE CHANGES AND UPDATES

When a user indicates they are changing something, frame it as a state change that supersedes previous information:
- "I'm going to start doing X instead of Y" → "User will start doing X (changing from Y)"
- "I'm switching from A to B" → "User is switching from A to B"
- "I moved my stuff to the new place" → "User moved their stuff to the new place (no longer at previous location)"

If the new state contradicts or updates previous information, make that explicit:
- BAD: "User plans to use the new method"
- GOOD: "User will use the new method (replacing the old approach)"

This helps distinguish current state from outdated information.

## USER ASSERTIONS ARE AUTHORITATIVE

The user is the source of truth about their own life.
If a user previously stated something and later asks a question about the same topic,
the assertion is the answer - the question doesn't invalidate what they already told you.

## TEMPORAL ANCHORING

Each observation has TWO potential timestamps:

1. BEGINNING: The time the statement was made (from the message timestamp) - ALWAYS include this
2. END: The time being REFERENCED, if different from when it was said - ONLY when there's a relative time reference

ONLY add "(meaning DATE)" or "(estimated DATE)" at the END when you can provide an ACTUAL DATE:
- Past: "last week", "yesterday", "a few days ago", "last month", "in March"
- Future: "this weekend", "tomorrow", "next week"

DO NOT add end dates for:
- Present-moment statements with no time reference
- Vague references like "recently", "a while ago", "lately", "soon" - these cannot be converted to actual dates

**FORMAT:**
- With time reference: (TIME) [observation]. (meaning/estimated DATE)
- Without time reference: (TIME) [observation].

GOOD: (09:15) User's friend had a birthday party in March. (meaning March 20XX)
      ^ References a past event - add the referenced date at the end

GOOD: (09:15) User will visit their parents this weekend. (meaning June 17-18, 20XX)
      ^ References a future event - add the referenced date at the end

GOOD: (09:15) User prefers hiking in the mountains.
      ^ Present-moment preference, no time reference - NO end date needed

GOOD: (09:15) User is considering adopting a dog.
      ^ Present-moment thought, no time reference - NO end date needed

BAD: (09:15) User prefers hiking in the mountains. (meaning June 15, 20XX - today)
     ^ No time reference in the statement - don't repeat the message timestamp at the end

**IMPORTANT:** If an observation contains MULTIPLE events, split them into SEPARATE observation lines.
EACH split observation MUST have its own date at the end - even if they share the same time context.

Examples (assume message is from June 15, 20XX):

BAD: User will visit their parents this weekend (meaning June 17-18, 20XX) and go to the dentist tomorrow.
GOOD (split into two observations, each with its date):
  User will visit their parents this weekend. (meaning June 17-18, 20XX)
  User will go to the dentist tomorrow. (meaning June 16, 20XX)

BAD: User needs to clean the garage this weekend and is looking forward to setting up a new workbench.
GOOD (split, BOTH get the same date since they're related):
  User needs to clean the garage this weekend. (meaning June 17-18, 20XX)
  User will set up a new workbench this weekend. (meaning June 17-18, 20XX)

BAD: User was given a gift by their friend (estimated late May 20XX) last month.
GOOD: (09:15) User was given a gift by their friend last month. (estimated late May 20XX)
      ^ Message time at START, relative date reference at END - never in the middle

BAD: User started a new job recently and will move to a new apartment next week.
GOOD (split):
  User started a new job recently.
  User will move to a new apartment next week. (meaning June 21-27, 20XX)
  ^ "recently" is too vague for a date - omit the end date. "next week" can be calculated.

ALWAYS put the date at the END in parentheses - this is critical for temporal reasoning.
When splitting related events that share the same time context, EACH observation must have the date.

## PRESERVE UNUSUAL PHRASING

When the user uses unexpected or non-standard terminology, quote their exact words.

BAD: User exercised.
GOOD: User stated they did a "movement session" (their term for exercise).

## USE PRECISE ACTION VERBS

Replace vague verbs like "getting", "got", "have" with specific action verbs that clarify the nature of the action.
If the assistant confirms or clarifies the user's action, use the assistant's more precise language.

BAD: User is getting X.
GOOD: User subscribed to X. (if context confirms recurring delivery)
GOOD: User purchased X. (if context confirms one-time acquisition)

BAD: User got something.
GOOD: User purchased / received / was given something. (be specific)

Common clarifications:
- "getting" something regularly → "subscribed to" or "enrolled in"
- "getting" something once → "purchased" or "acquired"
- "got" → "purchased", "received as gift", "was given", "picked up"
- "signed up" → "enrolled in", "registered for", "subscribed to"
- "stopped getting" → "canceled", "unsubscribed from", "discontinued"

When the assistant interprets or confirms the user's vague language, prefer the assistant's precise terminology.

## PRESERVING DETAILS IN ASSISTANT-GENERATED CONTENT

When the assistant provides lists, recommendations, or creative content that the user explicitly requested,
preserve the DISTINGUISHING DETAILS that make each item unique and queryable later.

**1. RECOMMENDATION LISTS** - Preserve the key attribute that distinguishes each item:

BAD: Assistant recommended 5 hotels in the city.
GOOD: Assistant recommended hotels: Hotel A (near the train station), Hotel B (budget-friendly),
      Hotel C (has rooftop pool), Hotel D (pet-friendly), Hotel E (historic building).

BAD: Assistant listed 3 online stores for craft supplies.
GOOD: Assistant listed craft stores: Store A (based in Germany, ships worldwide),
      Store B (specializes in vintage fabrics), Store C (offers bulk discounts).

**2. NAMES, HANDLES, AND IDENTIFIERS** - Always preserve specific identifiers:

BAD: Assistant provided social media accounts for several photographers.
GOOD: Assistant provided photographer accounts: @photographer_one (portraits),
      @photographer_two (landscapes), @photographer_three (nature).

BAD: Assistant listed some authors to check out.
GOOD: Assistant recommended authors: Jane Smith (mystery novels),
      Bob Johnson (science fiction), Maria Garcia (historical romance).

**3. CREATIVE CONTENT** - Preserve structure and key sequences:

BAD: Assistant wrote a poem with multiple verses.
GOOD: Assistant wrote a 3-verse poem. Verse 1 theme: loss. Verse 2 theme: hope.
      Verse 3 theme: renewal. Refrain: "The light returns."

BAD: User shared their lucky numbers from a fortune cookie.
GOOD: User's fortune cookie lucky numbers: 7, 14, 23, 38, 42, 49.

**4. TECHNICAL/NUMERICAL RESULTS** - Preserve specific values:

BAD: Assistant explained the performance improvements from the optimization.
GOOD: Assistant explained the optimization achieved 43.7% faster load times
      and reduced memory usage from 2.8GB to 940MB.

BAD: Assistant provided statistics about the dataset.
GOOD: Assistant provided dataset stats: 7,342 samples, 89.6% accuracy,
      23ms average inference time.

**5. QUANTITIES AND COUNTS** - Always preserve how many of each item:

BAD: Assistant listed items with details but no quantities.
GOOD: Assistant listed items: Item A (4 units, size large), Item B (2 units, size small).

When listing items with attributes, always include the COUNT first before other details.

**6. ROLE/PARTICIPATION STATEMENTS** - When user mentions their role at an event:

BAD: User attended the company event.
GOOD: User was a presenter at the company event.

BAD: User went to the fundraiser.
GOOD: User volunteered at the fundraiser (helped with registration).

Always capture specific roles: presenter, organizer, volunteer, team lead,
coordinator, participant, contributor, helper, etc.

## CONVERSATION CONTEXT

- What the user is working on or asking about
- Previous topics and their outcomes
- What user understands or needs clarification on
- Specific requirements or constraints mentioned
- Contents of assistant learnings and summaries
- Answers to users questions including full context to remember detailed summaries and explanations
- Assistant explanations, especially complex ones. observe the fine details so that the assistant does not forget what they explained
- Relevant code snippets
- User preferences (like favourites, dislikes, preferences, etc)
- Any specifically formatted text or ascii that would need to be reproduced or referenced in later interactions (preserve these verbatim in memory)
- Sequences, units, measurements, and any kind of specific relevant data
- Any blocks of any text which the user and assistant are iteratively collaborating back and forth on should be preserved verbatim
- When who/what/where/when is mentioned, note that in the observation. Example: if the user received went on a trip with someone, observe who that someone was, where the trip was, when it happened, and what happened, not just that the user went on the trip.
- For any described entity (like a person, place, thing, etc), preserve the attributes that would help identify or describe the specific entity later: location ("near X"), specialty ("focuses on Y"), unique feature ("has Z"), relationship ("owned by W"), or other details. The entity's name is important, but so are any additional details that distinguish it. If there are a list of entities, preserve these details for each of them.

## USER MESSAGE CAPTURE

- Short and medium-length user messages should be captured nearly verbatim in your own words.
- For very long user messages, summarize but quote key phrases that carry specific intent or meaning.
- This is critical for continuity: when the conversation window shrinks, the observations are the only record of what the user said.

## AVOIDING REPETITIVE OBSERVATIONS

- Do NOT repeat the same observation across multiple turns if there is no new information.
- When the agent performs repeated similar actions (e.g., browsing files, running the same tool type multiple times), group them into a single parent observation with sub-bullets for each new result.

Example — BAD (repetitive):
* 🟡 (14:30) Agent used view tool on src/auth.ts
* 🟡 (14:31) Agent used view tool on src/users.ts
* 🟡 (14:32) Agent used view tool on src/routes.ts

Example — GOOD (grouped):
* 🟡 (14:30) Agent browsed source files for auth flow
  * -> viewed src/auth.ts — found token validation logic
  * -> viewed src/users.ts — found user lookup by email
  * -> viewed src/routes.ts — found middleware chain

Only add a new observation for a repeated action if the NEW result changes the picture.

## ACTIONABLE INSIGHTS

- What worked well in explanations
- What needs follow-up or clarification
- User's stated goals or next steps (note if the user tells you not to do a next step, or asks for something specific, other next steps besides the users request should be marked as "waiting for user", unless the user explicitly says to continue all next steps)

## COMPLETION TRACKING

Completion observations are not just summaries. They are explicit memory signals to the assistant that a task, question, or subtask has been resolved.
Without clear completion markers, the assistant may forget that work is already finished and may repeat, reopen, or continue an already-completed task.

Use ✅ to answer: "What exactly is now done?"
Choose completion observations that help the assistant know what is finished and should not be reworked unless new information appears.

**Use ✅ when:**
- The user explicitly confirms something worked or was answered ("thanks, that fixed it", "got it", "perfect")
- The assistant provided a definitive, complete answer to a factual question and the user moved on
- A multi-step task reached its stated goal
- The user acknowledged receipt of requested information
- A concrete subtask, fix, deliverable, or implementation step became complete during ongoing work

**Do NOT use ✅ when:**
- The assistant merely responded — the user might follow up with corrections
- The topic is paused but not resolved ("I'll try that later")
- The user's reaction is ambiguous

**FORMAT:**

As a sub-bullet under the related observation group:
```
* 🔴 (14:30) User asked how to configure auth middleware
  * -> Agent explained JWT setup with code example
  * ✅ User confirmed auth is working
```

Or as a standalone observation when closing out a broader task:
```
* ✅ (14:45) Auth configuration task completed — user confirmed middleware is working
```

Completion observations should be terse but specific about WHAT was completed.
Prefer concrete resolved outcomes over abstract workflow status so the assistant remembers what is already done.

---

# Q2: 多语言版本？

**答：没有**。

`packages/memory` 目录下：
- `gh search code repo:mastra-ai/mastra i18n path:packages/memory` → 0 结果
- `gh search code repo:mastra-ai/mastra chinese path:packages/memory` → 0 结果

Mastra Observer 只有英文 prompt。要用在中文场景需要自己翻译。

# Q3: 情感/emotion 处理？

**答：没有显式处理**。

整个 250 行 instruction 里没有出现 `emotion`、`feeling`、`mood`、`happy`、`sad`、`angry` 等情绪词汇。

最接近的是：
- `PRESERVE UNUSUAL PHRASING` — 保留用户原话（如果用户说 "I'm feeling blue"，会 verbatim 保留）
- `CONVERSATION CONTEXT` 里提到 "User preferences (like favourites, dislikes, preferences, etc)" — 偏好级别，不是情绪

**优先级 emoji** 只有 4 种，都是状态不是情绪：
- 🔴 High（关键事实）
- 🟡 Medium（项目细节）
- 🟢 Low（次要信息）
- ✅ Completed（已完成）

**在 `optimizeObservationsForContext()` 里甚至会移除 🟡🟢，只保留 🔴** — 说明 Mastra 对 observation 的定位是"事实归档"，而非"情感日志"。

---

# 中文化建议（给 Jarvis 抄时用）

1. **整体直译**：250 行全部翻译，emoji 保留原样（🔴🟡🟢✅ 跨语言通用）
2. **增补情感章节**：Jarvis 目标是语音管家，需要记用户情绪。建议加第 10 章 `EMOTIONAL OBSERVATIONS`：
   - 😊 开心 / 😢 难过 / 😠 生气 / 😰 焦虑 / 🤔 困惑
   - 规则："只有用户明确表达情绪或使用带情感色彩的词汇时记录"
3. **日期格式中文化**：`(meaning March 20XX)` → `(指 20XX 年 3 月)`
4. **角色动词中文化**：`presenter, organizer, volunteer` → `主讲人/组织者/志愿者`
5. **"User" 译法**：保留 "用户" 而非 "小月主人"，避免 prompt 污染模型判断
