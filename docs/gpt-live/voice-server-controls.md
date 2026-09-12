# Server-side controls | OpenAI API
URL: https://developers.openai.com/api/docs/guides/voice-server-controls?api=live
(Live tab, verbatim)

# Server-side controls

Keep session control and private tool execution on your server.

Choose the API your application uses. Each API has its own authentication, session creation, and event contract.

## Control a GPT-Live session from your server

Attach your application server to an existing GPT-Live WebRTC or SIP session when the server needs to receive conversation events, execute private tools, or update the conversation. This second connection is called a sideband WebSocket. Both connections share one session while WebRTC or SIP carries the primary audio.

The sideband carries events and commands. Your application supplies the tool execution, authorization checks, and business rules. Keep API keys and tool credentials on your server.

### Decide whether you need a sideband

For browser applications, use the WebRTC data channel for captions and local UI updates. Use a sideband when transcript processing runs on your server, such as guardrail checks, sentiment analysis, or speculative tool calls. Your server can receive events and steer the same session directly while browser audio stays on WebRTC. See React to transcript fragments for examples.

If your backend already owns the primary WebSocket connection, it already receives the session's events and can send commands.

Responses delegation also works without a sideband. The browser can forward function-call events from its data channel to an authenticated backend for execution. OpenAI-hosted tools run through the delegated backend without an application tool executor.

### Attach to the existing session

1. Save the ID of the session your backend will control. For WebRTC, use `session.id` from the JSON response to `POST /v1/live/sessions`. For SIP, accept the incoming call first, then use `data.session_id` from its webhook. Keep the ID alongside the application's user and conversation record.
2. Open a WebSocket from your server at the following URL, substituting the saved ID unchanged. Authenticate with `Authorization: Bearer $OPENAI_API_KEY` using the project authentication that created or accepted the session. Include the same connection headers required when creating the session.

`wss://api.openai.com/v1/live/sessions/{session_id}/attach`

Copy code to clipboard

3. Receive events and send commands on the attached socket. The session is already running; do not send `session.start` again.

Treat the session ID as an opaque value. Preserve its prefix and use it only for the session to which your application has authorized access. Read the ID from the Live JSON response, rather than a Realtime `Location` header or `call_id` URL parameter.

### Observe events and send commands

| Task | Events or commands |
| --- | --- |
| Follow the conversation | Receive user and assistant transcript deltas, delegation events, and nested Responses events. |
| Update backend configuration | Use `session.update` to change supported settings within the existing delegation mode. Startup settings such as the frontend model and audio configuration stay fixed. |
| Provide context | Use `session.instructions.append` for instructions, `session.thinking.append` for quiet context, and `session.commentary.append` for speakable updates. |
| Return tool results | With Responses delegation, send `response.item.create`, then `response.create` to continue backend work. |
| Control microphone input | Use `session.input_audio.mute` and `session.input_audio.unmute`. Muting input does not stop the assistant's output. |
| Finish the session | Send `session.close` and receive `session.closed` before disconnecting. |

Commands follow the same validation and delegation rules as on the primary connection. For context appends, use `delegation_id: null` for general session context; a non-null ID must identify an existing client delegation. See Delegation and tools for configuration, function execution, and the context append examples.

For browser sessions, keep microphone input and speaker output on the negotiated WebRTC media track. Use the sideband for conversation events and control. A transcript event or command acknowledgment does not prove that audio has played or that the user has heard it.

### Receive reflected audio

A sideband also receives copies of subsequent input and output audio while the primary connection carries the live media:

| Event | Audio field | Timing |
| --- | --- | --- |
| `session.input_audio.append` | `audio` | No timestamps. |
| `session.output_audio.delta` | `delta` | `start_ms` and `end_ms` describe the output's range on the session timeline. |

Both payloads are base64-encoded raw mono PCM16LE at 24 kHz, regardless of the primary transport's audio format. Neither event has an `event_id`. Reflected input contains received audio before input muting; it does not confirm that the model consumed those samples. Reflected output ranges can have gaps for dropped frames and do not indicate when the caller heard the audio.

These are server events, not permission to send audio through the sideband. Send microphone audio through the primary transport; do not send `session.input_audio.append` on the attached socket.

### Assign one owner for each action

Choose whether the browser or backend handles each action. If both connections receive a function-call event, execute the function once. Apply the same ownership rule to context updates and requests to continue backend work.

Store transcripts and tool state in your application. Attach early if the backend needs to observe the conversation from the start, and retain any history collected before attachment. Do not rely on attachment to reconstruct earlier transcripts or tool results.

A sideband does not itself make session events private from the browser. Keep sensitive tool credentials and authorization decisions in your backend, and return only the context needed for the conversation.

## Apply conversation guardrails

Use your server's connection to monitor the conversation, check requests against your application's policies, and intervene when a check triggers. A sideband gives your server access to session events and commands; your application runs the checks and enforces their results. The same workflow applies when your server already owns the primary WebSocket connection.

### Run checks alongside the conversation

Guardrails are one use of processing transcript fragments as they arrive. The same stream can start a speculative lookup or update the UI alongside these checks.

1. Monitor transcripts. Accumulate `session.input_transcript.delta` fragments to check user requests for jailbreak attempts, sensitive information, or policy violations. Use `session.output_transcript.delta` to check assistant speech for unsupported claims or responses outside your application's scope. Keep each check associated with the transcript and application request it evaluated.
2. Run checks concurrently. A fast, lightweight model can evaluate requests while the conversation continues. Return a small structured result, such as `{"triggered": true}`, that your application can act on. Keep actions that require approval blocked until their checks pass; a timeout or failed check is not approval.
3. Block affected actions. When a check triggers, mark the request as blocked in application state. Check that state before executing a tool or committing a change, including work already queued. A spoken refusal does not prevent a tool from running.
4. Stop related work. Cancel application-owned jobs where your backend supports cancellation, and discard late results from blocked or superseded requests. With Responses delegation, stop executing affected custom functions and do not send `response.create` to continue blocked work. This does not cancel an already-running hosted response or stop frontend speech.
5. Record and redirect. Log the decision with the affected request and delegation IDs, then send a corrective instruction. An event name such as `guardrail.triggered` belongs to your application's telemetry; it is not a GPT-Live API event.

See Transcript deltas for collecting fragments and Delegation and tools for keeping backend results aligned with the current task.

### Redirect the conversation

Use `session.instructions.append` for guardrail steering. It can interrupt speech in progress and apply a new instruction. For example, after your application blocks a request, send:

```js
/**
 * @param {import("openai/resources/live/ws").LiveWS | import("openai/resources/live/sideband/ws").SidebandWS} connection
 */
export function sendUpdate(connection) {
  connection.send({
    type: "session.instructions.append",
    event_id: "guardrail_block_17",
    delegation_id: null,
    content:
      "Stop speaking immediately. Do not continue or act on the last request. Refuse briefly, then wait.",
  });
}
```

```python
from openai.resources.live.live import AsyncLiveConnection
from openai.resources.live.sideband import AsyncSidebandConnection

async def send_update(
    connection: AsyncLiveConnection | AsyncSidebandConnection,
) -> None:
    await connection.session.instructions.append(
        event_id="guardrail_block_17",
        delegation_id=None,
        content=(
            "Stop speaking immediately. Do not continue or act on the last request. "
            "Refuse briefly, then wait."
        ),
    )
```

Keep the instruction application-authored. Do not copy untrusted user text into it as an instruction. Use `delegation_id: null` for this session-wide correction, and keep `content` within 500 tokens.

Match `session.instructions.appended` to your command through `client_event_id`. The acknowledgment arrives after estimated context injection; it does not prove that the assistant stopped speaking or that queued audio stopped playing. Corrective instructions cannot retract audio the user has already heard.

For disclosures that request specific spoken wording, also use instructions. See Deliver a disclosure for an example and playback considerations.

### Control playback when needed

Test corrective instructions and action blocking first. If your application also needs to block model audio, control output at the client or media relay: temporarily mute or drop the output, discard locally queued audio, send the corrective instruction, and resume playback according to your application's recovery policy. Clear stale audio before resuming. A sideband alone does not control the media path, and an instruction acknowledgment is not a signal to resume playback.

`session.input_audio.mute` controls the caller's microphone input. It does not mute model output or cancel delegated work.

GPT-Live streams transcript fragments while speaking. If a check must finish before the user hears the audio, your application needs to buffer and approve audio before playback. This adds latency. Suppressed audio can also leave the model's conversation context ahead of what the user heard, so test how the conversation resumes.

### Test the intervention

Test allowed and blocked requests, false positives, slow or failed checks, a trigger during speech, a trigger while a tool is running, and late results from canceled work. Verify action blocking, application state, corrective speech, and actual playback separately. If you control output, include queued audio and recovery in the test. Use the voice agent evaluation Cookbook to compare task success and spoken response time.

## Finish cleanly

Keep receiving events while the backend owns tool execution or final usage collection. Register the `session.closed` handler before sending `session.close`, and keep the WebRTC connection, data channel, and sideband open while pending work drains. Save the final session usage and any backend usage received in Responses events before cleanup. If the connection fails before the final event arrives, record finalization as incomplete. See Managing sessions for the close sequence.
