# Cross-Device Control Research: RPi5 Headless → Mac Remote Control

*Generated: 2026-04-15 | Sources: 60+ | Agents: 5 parallel*

## Executive Summary

For Jarvis (RPi5) controlling a Mac on the same LAN, the recommended stack is:

1. **Primary control plane**: Hammerspoon HTTP server on Mac (20-70ms, free, extensible)
2. **Structured commands**: MQTT pub/sub via Mosquitto (already in stack, 1-10ms, multi-device ready)
3. **Complex operations**: SSH + osascript fallback (150-500ms, full shell access)
4. **Pre-built workflows**: Apple Shortcuts CLI over SSH (500-2000ms startup)
5. **Future**: MCP accessibility bridge (ghost-os) for AI-driven GUI control
6. **Security**: `command=` restriction in authorized_keys + dedicated `jarvis` user on Mac

SSH latency on LAN (2-10ms) is negligible vs LLM API calls (500-5000ms). The bottleneck is always the LLM, not the transport.

---

## 1. SSH-Based Remote Control for AI Agents

### Key Projects

| Project | Stars | Lang | Architecture |
|---------|-------|------|-------------|
| **ssh-mcp-server** (uarlouski) | 35 | TS | MCP server for SSH command execution |
| **pty-mcp** (raychao-oao) | 4 | Go | Real PTY sessions over MCP, `ai-tmux` daemon, `wait_for` pattern, `send_secret` for passwords |
| **SSH-MCP** (Harsh-2002) | 1 | Go | Persistent sessions, 3 isolation modes, jump host tunneling, 43 tools |
| **mcp-ssh-bridge** (shashikanth-gs) | — | Python | FastMCP bridge, OAuth 2.0, multi-host, credential isolation |
| **mcp-ssh-interactive** (qnxqnxqnx) | — | Python | tmux backend, fire-and-check pattern for long tasks |
| **sshDCommander** | — | Python | Commercial, claims <50ms overhead, persistent daemon |
| **pi-mono** (badlogic) | 33k | TS | SSH extension redirects ALL tool ops (read/write/edit/bash) over SSH |

### Major Agent Frameworks

- **OpenHands** (70k stars): Does NOT use SSH. Uses Docker + HTTP ActionExecutionServer (FastAPI)
- **pi-mono**: Has explicit `ssh.ts` extension — cleanest example of "agent runs locally, SSHes into remote"
- **Open Interpreter**: Local execution, async server mode, no native SSH transport
- **promptcmd** (tgalal): Inverts pattern — LLM runs locally, commands appear on remote server

### SSH Latency Measurements (via `sshping`)

| Environment | Min | Median | Avg | Jitter |
|-------------|-----|--------|-----|--------|
| LAN (wired 1Gbps) | 1.3ms | 2.2ms | 2.3ms | 0.5ms |
| LAN (WiFi) | 2.2ms | 4.2ms | 9.4ms | 27ms |
| WAN (remote) | 60ms | 65-77ms | 80-100ms | 15-76ms |

Quality thresholds: Excellent <60ms/jitter<10ms. Acceptable <100ms/jitter<75ms.

### Persistent Session Patterns

**autossh** — self-healing SSH tunnels:
```bash
autossh -M 0 -fN \
  -o "ServerAliveInterval 30" \
  -o "ServerAliveCountMax 3" \
  -L 5432:db.internal:5432 user@host
```

**SSH ControlMaster** — connection pooling (eliminates ~2s login per command):
```
Host mac
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 600
```

**tmux + SSH** — dominant pattern for AI agent workflows. Session survives SSH disconnects.

### 4 Architectural Patterns

| Pattern | Description | Best For |
|---------|-------------|----------|
| **A: Agent SSHes out** | Agent on RPi uses SSH to execute on Mac | Small device → powerful host (our case) |
| **B: MCP SSH bridge** | MCP server as gateway, agent never sees credentials | Multi-server, security-conscious |
| **C: Agent on remote** | Claude Code inside tmux on powerful server, SSH in to interact | Single dev machine |
| **D: Reverse execution** | LLM runs locally, remote commands pipe data back | Air-gapped servers |

### Practical Limitations

1. **Statefulness is hard** — SSH commands are stateless by default; every project solves persistence differently (tmux, PTY pools, session managers)
2. **Security surface** — agent with user's SSH permissions can do anything that user can do
3. **Error handling** — network drops, auth failures, stdout/stderr interleaving all need handling
4. **Interactive prompts** — `sudo` password, confirmations break naive `ssh host cmd`
5. **Latency is NOT the bottleneck** — LAN SSH 2-10ms vs LLM API 500-5000ms

---

## 2. Apple Ecosystem Remote Control

### SSH + osascript

**What works:**
```bash
ssh user@mac "osascript -e 'tell application \"Safari\" to activate'"
ssh user@mac "osascript -e 'tell application \"Music\" to play'"
```

**What breaks (TCC/permissions hell):**
- GUI scripting requires adding `/usr/bin/osascript` to Privacy > Accessibility (use Cmd+Shift+G to find `/usr/bin`)
- **Reboot required** after granting Accessibility permission
- Error -600 ("Application isn't running"): SSH user differs from GUI user, or `appleeventsd` stuck → `killall appleeventsd`
- Error 1002: Must add osascript to BOTH Accessibility AND Input Monitoring
- macOS Sequoia+: CGEvent keyboard synthesis silently blocked; monthly Screen Recording re-confirmation

**Latency:** SSH + simple osascript: ~150-200ms. GUI scripting (keystroke/click): 250-500ms (needs `delay` between steps).

### Apple Shortcuts CLI

```bash
ssh user@mac 'shortcuts run "Toggle Lights" -i /dev/stdin <<< "bedroom"'
shortcuts list  # enumerate available shortcuts
```

- Works perfectly over SSH
- ~500-1000ms startup overhead per invocation
- Shortcuts requiring GUI dialogs will **block** the CLI process
- Design shortcuts to accept all input as parameters, never prompt

### Hammerspoon (hs.httpserver) — RECOMMENDED

Built-in HTTP server for LAN control:

```lua
-- ~/.hammerspoon/init.lua
local server = hs.httpserver.new()
server:setPort(27182)
server:setCallback(function(method, path, headers, body)
    if path == "/launch" then
        hs.application.launchOrFocus(body)
        return "ok", 200, {}
    elseif path == "/type" then
        hs.eventtap.keyStrokes(body)
        return "ok", 200, {}
    elseif path == "/shortcut" then
        hs.execute("shortcuts run '" .. body .. "'")
        return "ok", 200, {}
    end
    return "not found", 404, {}
end)
server:start()
```

From RPi: `curl -X POST http://mac.local:27182/launch -d "Safari"`

- **Latency: 20-70ms** end-to-end from RPi (fastest option)
- Also has IPC CLI: `hs -c "hs.application.launchOrFocus('Safari')"`
- WebSocket support, Bonjour auto-discovery
- Requires Accessibility permission for Hammerspoon.app
- **No built-in auth** — implement shared-secret or firewall to RPi IP only

### BetterTouchTool (HTTP API)

- 30+ HTTP endpoints: trigger actions, paste text, clipboard read/write, run Shortcuts
- `curl http://mac:PORT/trigger_named/?trigger_name=MyAction`
- Shared secret auth + optional HTTPS
- **Latency: 10-30ms** on LAN
- Commercial: $10 standard / $22 lifetime

### Keyboard Maestro (Web Server)

- Built-in web server (port 4490/4491 HTTPS) with user/pass auth
- Public Web Trigger: expose specific macros without auth
- Remote Trigger via cloud relay (no port forwarding needed)
- **Latency: 10-20ms** local, 100-500ms via cloud relay
- Commercial: $36

### Remote Apple Events (EPPC)

- **NOT viable** — only works Mac-to-Mac (requires Apple EPPC client stack)
- CIS benchmarks recommend disabling it
- Protocol is decades old, poorly documented, known instability

### MCP-based Accessibility Servers (emerging)

| Project | Tools | Approach |
|---------|-------|----------|
| **ghost-os** (mcheemaa) | 33 MCP tools | Swift, reads AX tree directly, recipe system, 50ms per tree read |
| **mac-use-mcp** (antbotlab) | 18 MCP tools | Zero native deps (`npx`), Swift binary + AppleScript hybrid |
| **Fazm** (m13v) | Voice + MCP | Voice command → MCP tool calls → AX API |

These run **on the Mac itself**. For RPi: expose via Hammerspoon HTTP bridge or SSH CLI invocation.

### Comparison Matrix

| Approach | Latency (LAN) | Capability | Security | Cost | RPi-Ready |
|----------|---------------|-----------|----------|------|-----------|
| SSH + osascript | 150-500ms | Medium | SSH keys | Free | Yes |
| Shortcuts CLI (SSH) | 500-2000ms | High (pre-built) | SSH keys | Free | Yes |
| **Hammerspoon HTTP** | **20-70ms** | Very High | Shared secret | Free | **Yes** |
| BetterTouchTool HTTP | 10-30ms | Very High | Secret + HTTPS | $10-22 | Yes |
| Keyboard Maestro Web | 10-20ms | Very High | User/pass | $36 | Yes |
| Remote Apple Events | N/A | Medium | User-based | Free | **No** |
| MCP Accessibility | <50ms (local) | Highest | Local only | Free | Needs bridge |

---

## 3. Alternative Transports: MQTT / gRPC / HTTP / WebSocket

### Latency Comparison (LAN, small payloads)

| Transport | Connection Setup | p50 per msg | p99 per msg | Frame Overhead |
|-----------|-----------------|-------------|-------------|----------------|
| SSH (command) | 1-2s first, ~0 w/ ControlMaster | 10-25ms | 30-50ms | Variable |
| **MQTT QoS 0** | ~50ms (broker) | **1-10ms** | 10-30ms | **2 bytes** min |
| **gRPC (unary)** | ~10ms (HTTP/2+TLS) | **1-4ms** | 5-10ms | ~20 bytes |
| HTTP REST | 5-20ms (TCP) | 5-15ms | 20-40ms | 200-800 bytes |
| **WebSocket** | 10-20ms (upgrade) | **1-5ms** | 5-15ms | 2-14 bytes |

### MQTT (via Mosquitto)

**Already partially in Jarvis stack** (MQTT + sim devices).

Architecture:
```
RPi (Jarvis) ──MQTT──> Mosquitto Broker (on RPi)
                              │
Mac Agent <──MQTT─────────────┘
  (paho-mqtt, subscribes to jarvis/mac/cmd/#)
  Executes, publishes result to jarvis/mac/result/#
```

Command/response pattern:
```
RPi publishes: jarvis/mac/cmd/{request_id}    {"cmd": "osascript -e ...", "timeout": 10}
Mac publishes: jarvis/mac/result/{request_id}  {"stdout": "...", "rc": 0}
```

Strengths: trivial multi-device, built-in QoS/LWT, 2-byte overhead, already in stack.
Weaknesses: need broker process, no request/response built-in (build correlation IDs yourself).

Projects: **mqcontrol** (14 stars, Go) — subscribe to topic, execute command. **mqtt2ai** (23 stars) — MQTT to AI API bridge. **wactorz** (6 stars) — multi-agent orchestration on MQTT.

### gRPC

```protobuf
service DeviceControl {
  rpc Execute(CommandRequest) returns (CommandResponse);
  rpc StreamExecute(stream CommandRequest) returns (stream CommandResponse);
}
```

- Lowest latency (1-4ms p50, ~130μs noop on LAN)
- Strongly typed, bidirectional streaming, HTTP/2 multiplexing
- **Overkill for ~10-20 commands/day**; grpcio heavy on RPi 4GB

Projects: **DAAO** (Go+TS) — outbound-only mTLS gRPC tunnels for AI agent orchestration. **gbbirkisson/rpi** — gRPC server for remote GPIO/camera on RPi.

### HTTP REST (FastAPI on Mac)

```python
@app.post("/execute")
async def execute(cmd: str, timeout: int = 10):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
```

Projects: **mac-agent-gateway** (78 stars) — FastAPI exposing Apple apps (Reminders, Messages) as REST endpoints for AI agents.

Strengths: simplest to implement, Swagger UI, richest middleware ecosystem.
Weaknesses: no server push, polling required for Mac-initiated events.

### WebSocket

- True bidirectional after handshake (<1ms frame overhead)
- Natural for streaming command output line-by-line
- Built into FastAPI, can coexist with REST
- Must handle reconnection logic

### Recommendation for Jarvis

**MQTT is the natural choice** because:
1. Already in stack (MQTT + sim devices)
2. Adding more devices = zero friction
3. Built-in QoS/LWT handles network flakiness
4. Latency (1-10ms LAN) is more than sufficient
5. Mac agent is ~50 lines of `paho-mqtt`

**WebSocket as alternative** if you want streaming without a broker.

**gRPC is overkill** — setup cost doesn't pay off for personal assistant scale.

---

## 4. Screen Capture + VLM Pipelines

### The CUA Loop

```
Capture screenshot → Encode/compress → Send to VLM → Parse action → Execute input → Repeat
```

### Measured Latencies (Per Stage)

| Stage | Latency |
|-------|---------|
| Screenshot capture (macOS ScreenCaptureKit) | 50-100ms |
| JPEG/PNG encoding | 50-200ms |
| Network transfer to cloud VLM | 200-500ms |
| **VLM inference (cloud API)** | **1000-2000ms** |
| Action execution (mouse/keyboard) | 50-100ms |
| **Total per action (cloud VLM)** | **1.5-2.5s** |
| **Total per action (real-world)** | **3-6s** |
| Accessibility API alternative (click) | 10-30ms |
| Accessibility API alternative (read text) | 5-10ms |

Computer Agents blog: cut total from **14.1s → 1.7s** warm path. 77% of cold-start was Anthropic prompt cache creation.

### Major Projects

| Project | Stars | Approach | OSWorld Score |
|---------|-------|----------|---------------|
| **OpenCUA-72B** | — | Open-source, 200+ apps, 3 OSes | **45.0%** |
| **UI-TARS-1.5** | — | Multi-platform | 42.5% |
| **OpenAI CUA/Operator** | — | Browser-first, expanding to desktop | 36.4% |
| **Anthropic Computer Use** | — | Screenshot + mouse/keyboard in Docker/VNC | 28% |
| **CUA SDK** (trycua) | 13k | Multi-provider, Apple VM + VNC | — |
| **Microsoft UFO2** | — | **Hybrid**: Windows UI Automation + screenshots | — |
| **Fazm** | — | **Hybrid**: macOS AX API primary, screenshot fallback | — |
| **VNC MCP Server** | — | VNC bridge for Claude Desktop | — |
| Human | — | — | 72.4% |

### Screenshot Capture for Remote Targets

| Method | Latency | Headless? | Notes |
|--------|---------|-----------|-------|
| `screencapture` via SSH | ~100ms | **NO** — requires active GUI session | Fails silently on headless Macs |
| **VNC framebuffer** | 50-200ms | **YES** | Best approach. `vncdotool`, `vncsnapshot` |
| RDP | 100-300ms | YES | Windows-focused |
| Xvfb | ~50ms | YES | Linux only |
| Docker + VNC | 50-200ms | YES | Anthropic's official quickstart |

**Critical**: macOS `screencapture` over SSH is unreliable for headless Macs. Use VNC-based capture.

### Production Readiness

**Works today:** Web automation (75-90% with playbooks), sandboxed VM desktop control, Docker+VNC containers.

**Does NOT work reliably:** Fully autonomous unsupervised operation, real-time tasks (3-6s/action too slow), high-precision drag/scroll.

**The hybrid approach wins** (Fazm, UFO2): use accessibility APIs as primary (10-50ms, deterministic, free), fall back to screenshot+VLM only when AX tree insufficient (~10% of cases). 50-100x speedup for 90% of interactions.

### Cost per Workflow

| Provider | Per Screenshot | Per 10-step Workflow |
|----------|---------------|---------------------|
| Claude Sonnet | ~$0.0003-0.0006 | $0.02-0.10 |
| GPT-4V/5 | Similar | $0.02-0.15 |
| Local (UI-TARS 7B) | $0 (5.5GB VRAM) | $0 marginal |
| **Accessibility API** | **$0** | **$0** |

### For Jarvis (RPi5 → Remote Mac)

1. Run VNC server on Mac (built-in Screen Sharing)
2. Capture framebuffer via `vncdotool` from RPi (~100-200ms LAN)
3. Send to cloud VLM (Grok/Claude/GPT): 200-500ms upload + 1000-2000ms inference
4. Parse action, relay via VNC protocol (~50ms)
5. **Total: ~1.5-3s per action**

Better alternative: Run MCP accessibility server (ghost-os) on Mac, use RPi as orchestrator only. Skip screenshots for 90% of operations.

---

## 5. SSH Security for Always-On RPi → Mac

### Threat Model

- **Highest risk**: RPi compromise → SSH key exposed → full Mac access (iCloud Keychain, browser sessions, email, code repos)
- **Attack pattern**: Red Canary documents attackers harvesting `known_hosts` + private keys from compromised Linux hosts for lateral movement
- **SSH agent forwarding abuse**: `ssh -A` from RPi lets attacker hijack forwarded agent socket — **never use `-A`**
- **macOS resets `sshd_config` on OS updates** — hardening gets silently reverted

### Priority Actions

| Priority | Action | Effort | Impact |
|----------|--------|--------|--------|
| **Must** | Dedicated `jarvis` user on Mac (not personal account) | 10 min | High |
| **Must** | Ed25519 key pair, passphrase-protected on RPi | 5 min | High |
| **Must** | `authorized_keys` with `command=` + `no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty` | 20 min | **Critical** |
| **Must** | `sshd_config.d/99-jarvis-hardening.conf` (survives OS updates) | 15 min | High |
| **Must** | `sudoers.d/jarvis` NOPASSWD for specific binaries only | 5 min | High |
| Should | Firewall SSH to RPi's IP only (pf or macOS firewall) | 10 min | Medium |
| Should | Never use `ssh -A` (agent forwarding) | 0 min | Medium |
| Should | `ssh-agent` with timeout: `ssh-add -t 28800` | 5 min | Medium |
| Should | SSH monitoring via `log stream` + notification | 20 min | Low-Med |
| Nice | SSH Certificate Authority with short-lived certs | 1-2 hr | Medium |
| Nice | YubiKey for personal SSH (not for Jarvis automation) | 30 min | Low |
| Skip | Bastion/jump host, rbash, chroot, fail2ban, port knocking | — | — |

### The Critical Control: `command=` in authorized_keys

```bash
# On Mac: ~jarvis/.ssh/authorized_keys
command="/usr/local/bin/jarvis-ssh-gateway.sh",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAAC3... jarvis@rpi5
```

Gateway script whitelists specific commands:

```bash
#!/bin/bash
# /usr/local/bin/jarvis-ssh-gateway.sh
set -euo pipefail
LOG="/var/log/jarvis-ssh.log"
echo "$(date -Is) cmd=${SSH_ORIGINAL_COMMAND:-NONE} from=${SSH_CLIENT:-unknown}" >> "$LOG"

case "${SSH_ORIGINAL_COMMAND:-}" in
  "osascript "*)  exec /usr/bin/osascript -e "${SSH_ORIGINAL_COMMAND#osascript }" ;;
  "open "*)       exec ${SSH_ORIGINAL_COMMAND} ;;
  "pmset "*)      exec ${SSH_ORIGINAL_COMMAND} ;;
  "say "*)        exec ${SSH_ORIGINAL_COMMAND} ;;
  "shortcuts "*)  exec ${SSH_ORIGINAL_COMMAND} ;;
  "")             echo "Interactive shells not permitted." >&2; exit 1 ;;
  *)              echo "$(date -Is) DENIED: ${SSH_ORIGINAL_COMMAND}" >> "$LOG"
                  echo "Command not permitted." >&2; exit 1 ;;
esac
```

This transforms the threat model from "RPi compromised = Mac fully owned" to "RPi compromised = attacker can run `say`, `pmset`, and a few whitelisted commands." Massive blast radius reduction for 20 minutes of work.

### sshd_config Hardening (update-resilient)

```bash
# /etc/ssh/sshd_config.d/99-jarvis-hardening.conf
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
MaxAuthTries 3
MaxSessions 3
AllowUsers jarvis
AllowAgentForwarding no
AllowTcpForwarding no
X11Forwarding no
HostKeyAlgorithms ssh-ed25519
KexAlgorithms curve25519-sha256,curve25519-sha256@libssh.org
Ciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com
LogLevel VERBOSE
```

### Skip These (LAN-only)

- **rbash**: trivially escapable if any program can spawn a shell. `command=` is strictly superior
- **chroot**: heavyweight, pointless when `command=` prevents shell access
- **fail2ban**: no internet brute force on LAN
- **bastion/jump host**: adds complexity with no gain on same L2 network
- **port knocking**: security through obscurity, not worth the complexity

---

## Recommended Architecture for Jarvis

```
┌─────────────────────────────────────────────────────┐
│ RPi5 (Jarvis)                                       │
│                                                     │
│  Voice Pipeline → LLM → Skill Router                │
│       │                    │                        │
│       │         ┌──────────┼──────────┐             │
│       │         │          │          │             │
│       ▼         ▼          ▼          ▼             │
│   Local       MQTT      HTTP      SSH+osascript    │
│   Skills    (Mosquitto)  (curl)    (fallback)      │
│               │          │          │               │
└───────────────┼──────────┼──────────┼───────────────┘
                │          │          │
         LAN (1-10ms)  (20-70ms) (150-500ms)
                │          │          │
┌───────────────┼──────────┼──────────┼───────────────┐
│ Mac                      │          │               │
│               │          │          │               │
│         Mac Agent    Hammerspoon  sshd              │
│        (paho-mqtt)  (hs.httpserver) (command=)     │
│               │          │          │               │
│               └──────────┼──────────┘               │
│                          │                          │
│                    macOS APIs                       │
│            (osascript, Shortcuts,                   │
│             AX APIs, ghost-os MCP)                  │
└─────────────────────────────────────────────────────┘
```

**Phase 1** (now): SSH + osascript + `command=` gateway
**Phase 2**: Add Hammerspoon HTTP server for fast structured control
**Phase 3**: Add MQTT command channel (Mac agent daemon)
**Phase 4**: Integrate ghost-os MCP for AI-driven GUI control

---

## Sources (selected)

### SSH & AI Agents
- [mcp-ssh-bridge](https://github.com/shashikanth-gs/mcp-ssh-bridge) — FastMCP SSH bridge
- [pty-mcp](https://github.com/raychao-oao/pty-mcp) — Real PTY sessions over MCP
- [SSH-MCP](https://github.com/Harsh-2002/SSH-MCP) — Persistent SSH session manager
- [pi-mono ssh.ts](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/examples/extensions/ssh.ts) — Agent SSH extension
- [sshping](https://github.com/spook/sshping) — SSH latency measurement tool
- [Jason Vertrees: AI agent SSH on Raspberry Pi](https://www.linkedin.com/pulse/i-gave-ai-agent-ssh-access-my-raspberry-pi-jason-vertrees-v1avc)

### Apple Ecosystem
- [Hammerspoon hs.httpserver](https://www.hammerspoon.org/docs/hs.httpserver.html)
- [BetterTouchTool Webserver](https://docs.folivora.ai/docs/scripting/webserver/)
- [Keyboard Maestro Web Server](https://wiki.keyboardmaestro.com/trigger/Web_Server)
- [Apple: Run shortcuts from CLI](https://support.apple.com/guide/shortcuts-mac/run-shortcuts-from-the-command-line-apd455c82f02/mac)
- [ghost-os](https://github.com/mcheemaa/ghost-os) — 33 MCP tools for macOS AX
- [mac-use-mcp](https://github.com/antbotlab/mac-use-mcp) — Zero-dep macOS MCP

### Transport Alternatives
- [MQTT vs CoAP vs HTTP vs WebSocket](https://www.agilesoftlabs.com/blog/2026/04/mqtt-vs-coap-vs-http-vs-websocket-iot)
- [mqcontrol](https://github.com/albertnis/mqcontrol) — MQTT command execution
- [mac-agent-gateway](https://github.com/ericblue/mac-agent-gateway) — FastAPI Mac control
- [DAAO](https://github.com/daao-platform/daao) — Distributed AI Agent Orchestration via gRPC

### Screen Capture + VLM
- [CUA SDK](https://github.com/trycua/cua) — 13k stars, multi-provider CUA framework
- [VNC MCP Server](https://github.com/volkan-m/vnc-mcp-server) — VNC bridge for AI agents
- [Computer Agents: 14s → 1.7s latency optimization](https://computer-agents.com/blog/how-we-cut-agent-latency-from-14s-to-under-2s)
- [Fazm](https://fazm.ai/t/macos-accessibility-api-agent-speed) — Hybrid AX + screenshot agent

### SSH Security
- [Smallstep: SSH Certificates](https://smallstep.com/blog/use-ssh-certificates)
- [Red Canary: Lateral Movement with SSH](https://redcanary.com/blog/threat-detection/lateral-movement-with-secure-shell/)
- [SSH Agent Abuse](https://grahamhelton.com/blog/ssh-agent)
- [ZeonEdge: SSH Hardening 2026](https://zeonedge.com/en/blog/ssh-hardening-2026-complete-guide-linux-server)
- [macOS Sequoia STIG](https://www.stigviewer.com/stigs/apple_macos_15_sequoia/2025-05-05/MAC-3_Classified)
