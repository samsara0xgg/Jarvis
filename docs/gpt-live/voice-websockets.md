# WebSockets

Connect server-owned audio streams over WebSockets.

Choose the API your application uses. Each API has its own authentication, session creation, and event contract.

## Connect a server to GPT-Live

Use a primary WebSocket when your server captures audio or relays an audio stream for a client. It carries audio and JSON events in both directions. Keep the project API key on that trusted server. For browser and mobile applications, start with WebRTC.

This guide covers the primary audio connection. A sideband connection lets a server observe and control an existing Live session. A Responses WebSocket connects your backend to the Responses API for reasoning and tools. Neither replaces the primary audio connection.

### Authenticate and start the session

1. Connect to `wss://api.openai.com/v1/live/sessions` with no query parameters. Authenticate with `Authorization: Bearer $OPENAI_API_KEY` and include the connection headers shown in the example.
2. Send `session.start` as the first message. Put the model, conversation instructions, audio format, voice, and delegation configuration inside the `session` object.
3. Wait for `session.started` before sending audio or application commands. It contains the resolved session configuration and session ID.

The example below uses Marin, PCM16 audio at 24 kHz, and a Responses backend with web search. Keep conversation instructions short. Configure backend instructions, tools, and tool permissions through Delegation and tools.

### Stream audio with an SDK

For Node.js, install `openai` and `ws` with `npm install openai ws` and save the JavaScript example as `client.mjs`. For Python on macOS or Linux, install `openai[realtime]` and save the Python example as `client.py`. Set `OPENAI_API_KEY` in the server environment. These examples require an SDK version with Live support. The example reads raw, mono PCM16 audio at 24 kHz from standard input and writes returned audio in the same format to standard output. Connect these streams to your application's audio capture and playback. Logs and transcript events go to standard error so they do not corrupt the audio stream.

**JavaScript example (client.mjs):**

```javascript
import OpenAI from "openai";
import { LiveWS } from "openai/resources/live/ws";

// stdin and stdout carry raw mono PCM16 audio at 24 kHz, not WAV files.
// Supply microphone bytes continuously and play stdout in the same format.
process.stdin.pause();
const ws = new LiveWS(new OpenAI());
let started = false;
let closing = false;
let finalized = false;
let pendingByte = Buffer.alloc(0);
/** @type {ReturnType<typeof setTimeout> | undefined} */
let closeTimeout;

ws.socket.on("open", () => {
  ws.send({
    type: "session.start",
    event_id: "event_start",
    session: {
      model: "gpt-live-1",
      instructions:
        "Be concise. Delegate requests needing current information to the backend, which can search the web.",
      audio: {
        format: { type: "audio/pcm", rate: 24000 },
        output: { voice: "marin" },
      },
      delegation: {
        type: "responses",
        responses: {
          model: "gpt-5.6-luna",
          tools: [{ type: "web_search" }],
          tool_choice: "auto",
        },
      },
    },
  });
});

process.stdin.on("data", (chunk) => {
  if (!started || closing || ws.socket.readyState !== 1) return;
  const bytes = Buffer.concat([pendingByte, chunk]);
  const completeLength = bytes.length - (bytes.length % 2);
  pendingByte = bytes.subarray(completeLength);
  if (completeLength) {
    ws.send({
      type: "session.input_audio.append",
      audio: bytes.subarray(0, completeLength).toString("base64"),
    });
  }
});

// Register the final-event handler before any close command can be sent.
ws.on("event", (event) => {
  if (event.type === "session.started") {
    started = true;
    console.error("Session ready", event.session.id);
    process.stdin.resume();
  } else if (event.type === "session.output_audio.delta") {
    process.stdout.write(Buffer.from(event.delta, "base64"));
  } else if (event.type === "session.closed") {
    finalized = true;
    clearTimeout(closeTimeout);
    process.stdin.pause();
    console.error("Final session usage", event.usage);
    ws.close();
  } else {
    // Includes transcript deltas and nested response.event usage.
    console.error(JSON.stringify(event));
  }
});

process.on("SIGINT", () => {
  if (closing) return;
  if (!started || ws.socket.readyState !== 1) {
    ws.socket.platformSocket.terminate();
    return;
  }
  closing = true;
  process.stdin.pause();
  ws.send({ type: "session.close" });
  closeTimeout = setTimeout(() => {
    console.error("Incomplete finalization: session.closed was not received");
    process.exitCode = 1;
    ws.socket.platformSocket.terminate();
  }, 15_000);
});
ws.on("error", (error) => {
  console.error(error.message);
  process.exitCode = 1;
});
ws.socket.on("close", () => {
  clearTimeout(closeTimeout);
  process.stdin.pause();
  if (!finalized) {
    console.error("Connection closed without final session usage");
    process.exitCode = 1;
  }
});
```

**Python example (client.py):**

```python
import asyncio
import base64
import os
import signal
import sys

from openai import AsyncOpenAI
from openai.types.live.session_config_param import SessionConfigParam

async def main() -> None:
    # stdin/stdout carry raw mono PCM16 at 24 kHz, not WAV files.
    session: SessionConfigParam = {
        "model": "gpt-live-1",
        "instructions": "Be concise. Delegate requests needing current information to the backend, which can search the web.",
        "audio": {
            "format": {"type": "audio/pcm", "rate": 24000},
            "output": {"voice": "marin"},
        },
        "delegation": {
            "type": "responses",
            "responses": {
                "model": "gpt-5.6-luna",
                "tools": [{"type": "web_search"}],
                "tool_choice": "auto",
            },
        },
    }
    loop = asyncio.get_running_loop()
    chunks: asyncio.Queue[bytes] = asyncio.Queue()
    finalized = asyncio.Event()
    closing = False
    pending = b""
    close_task: asyncio.Task[None] | None = None

    def read_audio() -> None:
        chunk = os.read(sys.stdin.fileno(), 4800)
        if chunk:
            chunks.put_nowait(chunk)
        else:
            # EOF does not end a Live session. Use SIGINT to finalize it.
            loop.remove_reader(sys.stdin.fileno())

    async with AsyncOpenAI() as client:
        async with client.live.connect() as connection:

            async def send_audio() -> None:
                nonlocal pending
                while True:
                    chunk = pending + await chunks.get()
                    complete = len(chunk) - len(chunk) % 2
                    pending = chunk[complete:]
                    if complete and not closing:
                        await connection.session.input_audio.append(
                            audio=base64.b64encode(chunk[:complete]).decode("ascii")
                        )

            async def close_session() -> None:
                nonlocal closing
                closing = True
                loop.remove_reader(sys.stdin.fileno())
                await connection.session.close()
                try:
                    await asyncio.wait_for(finalized.wait(), timeout=15)
                except TimeoutError:
                    print(
                        "Incomplete finalization: no session.closed event",
                        file=sys.stderr,
                    )
                    await connection.close()

            def request_close() -> None:
                nonlocal close_task
                if close_task is None:
                    close_task = asyncio.create_task(close_session())

            # Start receiving before a close command can be requested.
            await connection.session.start(session=session, event_id="event_start")
            sender = asyncio.create_task(send_audio())
            try:
                async for event in connection:
                    if event.type == "session.started":
                        print("Session ready", event.session.id, file=sys.stderr)
                        loop.add_reader(sys.stdin.fileno(), read_audio)
                        loop.add_signal_handler(signal.SIGINT, request_close)
                    elif event.type == "session.output_audio.delta":
                        sys.stdout.buffer.write(base64.b64decode(event.delta))
                        sys.stdout.buffer.flush()
                    elif event.type == "session.closed":
                        print("Final session usage", event.usage, file=sys.stderr)
                        finalized.set()
                        break
                    else:
                        print(event.model_dump_json(), file=sys.stderr)
            finally:
                loop.remove_reader(sys.stdin.fileno())
                loop.remove_signal_handler(signal.SIGINT)
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                if close_task is not None:
                    await close_task
            if not finalized.is_set():
                raise RuntimeError("Connection closed without final session usage")

if __name__ == "__main__":
    asyncio.run(main())
```

Run `node client.mjs` or `python client.py` with your audio source and player attached. After `Session ready` appears, supply a continuous microphone stream paced at its recorded sample rate. Piping an entire file at once does not simulate a live microphone. EOF on the audio source does not end the conversation. Send `SIGINT` to the process to request a graceful close.

The example connects the audio streams; your application handles capture, buffering, playback, and resampling when needed. Test these parts with your devices and network before evaluating model behavior.

### Choose the audio format

Set `session.audio.format` at startup. One format applies to both input and output and cannot change during the session.

- `{"type":"audio/pcm","rate":24000}`: mono signed 16-bit little-endian PCM at 24 kHz; the default.
- `{"type":"audio/pcm","rate":16000}`: mono signed 16-bit little-endian PCM at 16 kHz.
- `{"type":"audio/pcmu","rate":8000}`: G.711 μ-law at 8 kHz, one byte per sample.
- `{"type":"audio/pcma","rate":8000}`: G.711 A-law at 8 kHz, one byte per sample.

Base64-encode raw bytes without a WAV or other container header. PCM chunks must contain complete 16-bit samples, so their byte length must be even. The example carries a trailing byte into the next input chunk. Chunk boundaries are otherwise arbitrary: preserve a continuous, ordered stream.

Resample audio when its sample rate differs from the configured rate. Changing the format setting does not convert your input bytes. To adapt the example for G.711, forward each chunk's codec bytes without the PCM-specific two-byte alignment logic, and configure the output player for the same codec. A matching G.711 stream can pass through without conversion to PCM. See Telephony integrations for connecting a phone call.

### Send and receive events

Send each event as a JSON text message. Audio travels as base64 inside those messages.

- Send audio: send `session.input_audio.append` with raw, base64-encoded bytes in `audio`. Audio appends have no acknowledgment.
- Receive audio: decode `delta` from each `session.output_audio.delta` event and queue the audio for playback in order, using the configured format.
- Receive transcripts: append the text in `delta` from `session.input_transcript.delta` and `session.output_transcript.delta` to the corresponding transcript.
- Receive backend events: when using Responses delegation, process the nested `event` in each `response.event` envelope.
- Handle errors: handle rejected commands and session errors from `error` events. Use `error.client_event_id`, when present, to identify the command.

Output audio events have no timing fields, and GPT-Live does not emit an output-audio-done event. Track your playback queue to know which received audio has played. Transcript timestamps describe intervals on the session timeline; they do not mark audio playback completion. A backend response completing also does not mean the assistant has finished speaking.

GPT-Live manages when to listen and speak as audio streams. It does not use Realtime's input-buffer commit and `response.create` voice-turn loop. In Live, `response.create` starts or continues delegated backend work. See Delegation and tools for that workflow.

### Configure an ongoing session

The Live model, initial conversation instructions, audio format, voice, and delegation mode are fixed at startup. Use `session.update` for supported settings within the existing delegation mode; omitted settings keep their current values. A successful update returns `session.updated` with the resolved session configuration.

Use `session.instructions.append` to add conversation instructions, and `session.input_audio.mute` or `session.input_audio.unmute` to control incoming audio. Muting input does not cancel backend work or stop generated speech. See Managing sessions for context updates, transcripts, input controls, and usage.

### Close the session

Send `session.close` when the conversation ends. Install the `session.closed` listener first, keep receiving until that event arrives, and then release the connection. The example waits up to 15 seconds and reports incomplete finalization if the terminal event never arrives.

Preserve the final voice usage from `session.closed` and the backend usage events already received. Voice-duration updates are cumulative snapshots; do not add them together. A transport failure or timeout before `session.closed` leaves final usage unconfirmed. See Managing sessions for the full lifecycle.
