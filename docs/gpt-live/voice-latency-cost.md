## GPT-Live usage and costs

GPT-Live separates the voice conversation from the backend that reasons and runs tools. Estimate these two costs separately: the voice session depends on duration, while backend costs depend on the models and tools you use.

### Voice session costs

GPT-Live voice sessions are billed per second at the current model rate. Session duration is not rounded up to the next whole minute.

Active session time includes time when the user speaks, the assistant speaks, both are silent, or the backend is working.

For estimates, count the active session from start through closure. Use the duration reported by the API instead of timing only the audio you play. Muting microphone input does not close the session. When the conversation is finished, close the session and collect its final usage.

See API pricing for backend model and tool prices.

### WebRTC initialization charges

A `POST /v1/live/sessions` request to create a WebRTC session bills 15 seconds of voice duration while the session initializes. That amount is credited against duration charges once the session starts running. Don't add another 15 seconds to the running session's duration when estimating its cost.

For example, the 90-second session below already includes the 15 seconds billed at initialization. It is not billed as 105 seconds. Account for session-creation charges when evaluating reconnects or applications that create sessions before the user is ready to speak.

### Backend costs

Backend calls are billed separately from the voice session, just as they are in applications without voice. Include model input and output tokens, cached input where supported, and any applicable image or tool charges. If your application calls other services, include their costs in your estimate too.

You can optimize this work separately from the voice frontend. Use the general cost optimization guide to reduce requests and token usage. Use prompt caching for eligible backend models by keeping reusable instructions, tool definitions, and other stable content at the beginning of the prompt.

Backend choices can also change the length of the conversation. Compare the combined cost when an optimization makes the user wait longer or changes how reliably the assistant completes the task.

### Estimate conversation costs

For a conversation with one voice session:

Total cost = (billable voice seconds ÷ 60 × voice rate per minute) + backend costs

For example, at an illustrative voice rate of $0.05 per minute, a 90-second voice session costs $0.075. If the backend model and tool costs total $0.02, the conversation costs $0.095:

| Component | Calculation | Cost |
| --- | --- | --- |
| Voice session | 90 seconds ÷ 60 × $0.05 | $0.075 |
| Backend work | Total model and tool costs | $0.02 |
| Conversation total | $0.075 + $0.02 | $0.095 |

The rates and backend cost above are examples; use the current voice rate, your measured backend usage, and the applicable model and tool rates. If the task spans multiple voice sessions, add their durations and include backend work performed between sessions.

### Optimization strategies

Focus on helping the user complete the task with less unnecessary conversation and waiting. Keep the confirmations and checks the task requires.

#### Provide relevant context before the session

Gather information your application already has permission to use before starting the voice session. For example, an assistant helping with an order can start with the order number and current status, so the user does not need to repeat them or wait for another lookup.

Keep this context current and focused on the task. Give the voice model the information it needs for the conversation; keep detailed records and workflows in the backend. See session configuration and delegation and tools.

#### Reduce time spent waiting for tools

Shorter waits can improve the user experience and reduce voice-session costs. For example, suppose your backend uses `gpt-5.6-luna` with Fast mode and runs independent tool calls in parallel. If these optimizations help the user finish and close the voice session one minute sooner, you save $0.05 in voice charges. The total cost falls if the additional backend cost is less than that saving.

You can also start a speculative lookup from transcript fragments before a delegation event arrives. Include unused speculative work in your backend cost measurements.

See Reduce backend latency for model, connection, streaming, and tool optimizations. Validate useful spoken response time and task success with voice agent evaluations.

#### Close the session during long tasks

The voice frontend and your application-managed backend can run independently. With client delegation, your backend worker can keep running while the voice session is open or closed. Save the task state and conversation context before closing the voice session.

For an ambient agent, close the voice session while the backend handles a long-running task, such as coding in goal mode. Offer a button labeled Resume conversation to start a new voice session when the user returns, or use a backend completion event to start a new session and notify the user that the result is ready.

Restore the conversation by starting a new session with saved context and the verified task result in `input`. For example, send this startup event over a new WebSocket connection:

```
{
  "type": "session.start",
  "session": {
    "model": "gpt-live-1",
    "instructions": "Help the user review completed work and delegate follow-up tasks.",
    "input": [
      {
        "type": "message",
        "role": "developer",
        "content": [
          {
            "type": "input_text",
            "text": "Saved task: add CSV export. Result: code is ready for review."
          }
        ]
      }
    ],
    "delegation": { "type": "client" }
  }
}
```

Wait for `session.started` before streaming audio. See seed a session with prior conversation for the supported history format.

If the earlier session was stored with `store: true`, you can also fork that session. Keep the verified backend task state in your application whichever approach you use.

Closing saves $0.05 per minute of idle voice time; compare that saving with reconnection costs and the interruption to the user's experience.

#### Choose the right backend model

Start with models that meet the task's accuracy and reliability requirements. Then compare total conversation cost, including voice duration, model usage, tool calls, and retries. The model selection guide describes how to balance these tradeoffs.

A larger backend model can cost less overall if it completes the task faster and the voice-session savings exceed its additional token costs. A cheaper model can cost more overall if it takes longer, repeats tool calls, or fails the task.

Compare cost per successful task alongside completion rate and time to completion. Include failed attempts and retries in the total so a cheaper configuration does not look better because it completes less work. Use the voice agent evaluation Cookbook when planning your comparison.

### Monitor actual usage

Record voice duration and backend usage separately for each session. GPT-Live reports cumulative voice duration in seconds:

```
{
  "type": "session.usage.updated",
  "event_id": "event_usage_1",
  "usage": { "seconds": 12 },
  "context_window": { "usage_ratio": 0.42 }
}
```

Each update replaces the previous duration snapshot. Do not sum the snapshots. After sending `session.close`, keep receiving events until `session.closed` and record its final `usage.seconds` once. Follow the graceful-close procedure so your application can collect final usage before disconnecting.

For Responses delegation, read the backend response's `usage` from nested `response.completed` events delivered through `response.event`. Count each backend response once, using its response ID, and retain the input, output, and cached-token details needed to apply that model's rates. For backend work your application runs independently, collect usage from those requests too.

Compare estimated and actual totals across representative conversations. Keep evaluation-only model calls separate from application usage, and review cost together with task success.

---

(Note: the page also has a "Realtime API costs" section covering the Realtime API — not transcribed here per task scope.)
