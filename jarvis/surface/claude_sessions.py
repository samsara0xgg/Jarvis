"""Allen's own Claude Code sessions, read from Claude Code's own state (ADR 0046).

The roster and live status come from ``claude agents --json``: every
interactive and background session, its ``status`` (busy / idle / waiting)
and, for a background job, the agent-view ``state`` (working / blocked /
done). Two files Claude Code keeps per session fill in the rest: a background
job's ``~/.claude/jobs/<id>/state.json`` (its one-line ``detail``) and the
tail of the transcript ``~/.claude/projects/*/<sessionId>.jsonl`` (last
prompt, branch, current tool, last answer) and its head (the first moment it records is
``started_ms``; the registry's process start when there is none, else null). Nothing is
installed into Claude Code. ``GET /inherent/claude-sessions`` serves :meth:`ClaudeSessions.read`.

A row's title is the name he sees in Claude Desktop when it has one: Desktop keeps it in
``~/Library/Application Support/Claude/claude-code-sessions/**/local_*.json`` (``title``, keyed by
``cliSessionId``), and a session it spawned for a project thread carries the thread's title there.
Then ``claude agents``' own name, unless it is the generic "from a project" name or an
auto-made slug (``projects-3e``); then the session's opening message, which is what it is about
and does not change turn by turn (its ``@file`` references dropped, the files' names when that is
all it says, and for a project thread's session the words of the message that woke it rather than
the wake envelope around them, and for one a coordinator started, the thread its note names,
else his own words it quotes, else the note's first sentence); then his last prompt the same
way; then the folder.

On a brain (ADR 0170) these files are the owner's Mac's, so the board is asked of the connected
terminal, which runs :meth:`ClaudeSessions.raw`, :meth:`~ClaudeSessions.conversation` and
:meth:`~ClaudeSessions.reply` itself; what needs a model (whether a finished turn asks Allen
something) stays with the brain.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import html
import json
import logging
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.state import device_reads
from jarvis.state.daily_contract import DailyError

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.shared.device_link import DeviceLink

LOGGER = logging.getLogger(__name__)

RETENTION_MS = 24 * 60 * 60 * 1000
REFRESH_S = 3.0
_TAIL_BYTES = 512 * 1024
_TEXT_CHARS = 160
# ADR 0125: a finish is asked about only while it is news, and only its last 600 characters.
_ASK_WINDOW_MS = 15 * 60 * 1000
_TAIL_CHARS = 600
# The island's conversation page: this much of the transcript's end, at most this many messages.
_CONVERSATION_BYTES = 2 * 1024 * 1024
_MESSAGES = 60
_MESSAGE_CHARS = 20_000
_PASTED = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.DOTALL)
# A hidden attach shows its first screen in about 0.2 s; the input box takes a little longer.
_ATTACH_S = 3.0
SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DESKTOP_SESSIONS = Path("Library") / "Application Support" / "Claude" / "claude-code-sessions"
# What Claude Desktop and ``claude agents`` call a remote-control session before it has a title.
_UNNAMED = frozenset({"remote control session (from a project)", "session"})
# Claude Code's own made-up name for an unnamed session: words, then a short hex tail.
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{2,4}(?:-[0-9a-f]{2})?")
# A file he attached, as Claude Code writes it into the prompt: @"/path with spaces" or @/path.
_ATTACHED = re.compile(r'@"([^"]+)"|@(\S+)')
# A project thread's session is prompted with a wake envelope; his words are the body of a message
# a human sent, the one that triggered the turn first. A coordinator's relay quotes his words in
# ``<cited author="user">`` and otherwise carries its own ``<note>``.
_WAKE_MESSAGE = re.compile(r"<message\b([^>]*)>(.*?)</message>", re.DOTALL)
_CITED = re.compile(r'<cited\b[^>]*\bauthor="user"[^>]*>(.*?)</cited>', re.DOTALL)
_NOTE = re.compile(r"<note\b[^>]*>(.*?)</note>", re.DOTALL)
_ENVELOPE = re.compile(r"<(?:wake|relay)\b")
# The project thread a coordinator's note names, as it quotes it: thread "…" or thread 「…」.
_THREAD_NAMED = re.compile(r'\bthread\s+(?:"([^"\n]{1,80})"|\u300c([^\u300d\n]{1,80})\u300d)')
_SENTENCE = re.compile(r"(?<=[.!?])\s|(?<=[\u3002\uff01\uff1f])|\n")
# Process names (lowercase prefix) that host a terminal session, innermost first wins.
_TERMINALS = (
    ("zellij", "zellij"),
    ("tmux", "tmux"),
    ("ghostty", "Ghostty"),
    ("iterm", "iTerm"),
    ("terminal", "Terminal"),
    ("code", "VS Code"),
    ("cursor", "Cursor"),
)
_TOOL_ARGS = ("file_path", "command", "pattern", "description", "url", "query")

ClaudeSession = dict[str, Any]


def _short(value: object) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= _TEXT_CHARS else text[: _TEXT_CHARS - 1] + "…"


def _iso_ms(value: object) -> int:
    try:
        return int(dt.datetime.fromisoformat(str(value)).timestamp() * 1000)
    except ValueError:
        return 0


def _named(name: object) -> str:
    """``name`` as a title, or "" when it is a placeholder or a made-up slug."""
    text = _short(name)
    if text.lower() in _UNNAMED or _SLUG.fullmatch(text):
        return ""
    return text


def _prompt_title(prompt: str) -> str:
    """A prompt as a title: its words without attached-file paths, else the files' names."""
    if "<wake" in prompt:
        human = [
            (attrs, body) for attrs, body in _WAKE_MESSAGE.findall(prompt)
            if 'from="human"' in attrs
        ]
        triggered = [body for attrs, body in human if 'trigger="true"' in attrs]
        # Only a harness line (a spawn): no words of his, so no title from it.
        prompt = html.unescape((triggered or [body for _, body in human] or [""])[-1])
    elif "<relay" in prompt:
        cited = _CITED.findall(prompt)
        note = _NOTE.findall(prompt)
        said = html.unescape(note[0]).strip() if note else ""
        named = _THREAD_NAMED.search(said)
        if named:  # the thread's own name is how he knows the session
            prompt = named.group(1) or named.group(2)
        elif cited:
            prompt = html.unescape(cited[-1])
        else:
            prompt = _SENTENCE.split(said, maxsplit=1)[0]
    words = _short(_ATTACHED.sub(" ", prompt))
    if words:
        return words
    return _short(", ".join(Path(a or b).name for a, b in _ATTACHED.findall(prompt)))


def _project(cwd: str) -> str:
    """``~/Projects/jarvis/.claude/worktrees/x`` belongs to ``jarvis``."""
    return Path(re.split(r"/\.(?:claude/)?worktrees/", cwd)[0]).name


def _tool(block: dict[str, Any]) -> str:
    args = block.get("input")
    if not isinstance(args, dict):
        args = {}
    arg = next((str(args[k]) for k in _TOOL_ARGS if args.get(k)), "")
    if arg.startswith("/"):
        arg = Path(arg).name
    return _short(f"{block.get('name', '?')} {arg.splitlines()[0] if arg else ''}".strip())


def _newest_blocks(entry: dict[str, Any]) -> tuple[str, str]:
    """(tool called after the newest text, newest text) from one assistant entry."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    tool = ""
    for block in reversed(content if isinstance(content, list) else []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and str(block.get("text", "")).strip():
            return tool, str(block["text"])
        if block.get("type") == "tool_use" and not tool:
            tool = _tool(block)
    return tool, ""


def _said_at(entry: dict[str, Any]) -> float | None:
    """When Allen typed this entry (epoch seconds); None unless it is his own words.

    A user entry whose ``origin`` names something else (a task notification, a peer or
    subagent hand-back) is not him; an entry without ``origin`` cannot be told apart.
    """
    origin = entry.get("origin")
    if (isinstance(origin, dict) and origin.get("kind") != "human") or not _said(entry):
        return None
    ms = _iso_ms(entry.get("timestamp"))
    return ms / 1000 if ms else None


def _entry_title(entry: dict[str, Any]) -> str:
    """The title one transcript entry gives when it is his own opening message; else ""."""
    if entry.get("type") != "user":
        return ""
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return ""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = " ".join(
            str(block.get("text") or "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if not isinstance(content, str):
        return ""
    # Remote Control injects the wake and relay envelopes as meta entries; other meta
    # entries (command output, caveats) are not his words.
    if entry.get("isMeta") and not _ENVELOPE.match(content.lstrip()):
        return ""
    return _prompt_title(content)


def _opening(path: Path) -> tuple[str, int]:
    """(title, start in epoch ms) the transcript's head gives; "" and 0 for what it does not say.

    The title is what the session's first message gives. Claude Code keeps only 200 characters of
    a ``last-prompt``, which a wake envelope fills before his words, so the whole user entry is
    read instead. The start is the first moment the transcript records: when the session began,
    which a resumed session's newer process (the registry's ``startedAt``) is not.
    """
    with path.open("rb") as fh:
        lines = fh.read(_TAIL_BYTES).decode("utf-8", "replace").splitlines()
    title, started = "", 0
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        started = started or _iso_ms(entry.get("timestamp"))
        title = title or _entry_title(entry)
        if title and started:
            break
    return title, started


def read_transcript(path: Path) -> dict[str, Any]:
    """Last prompt, branch, current tool and last answer from the transcript tail.

    ``tool`` is set only when the newest assistant block is a tool call, i.e.
    what the session is doing (or asking to do) right now. ``said`` is when Allen
    sent each message in the tail, newest first (ADR 0128).
    """
    out: dict[str, Any] = {
        "prompt": "", "branch": "", "tool": "", "last_message": "", "tail": "", "said": [],
        "title": "",
    }
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - _TAIL_BYTES))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    if size > _TAIL_BYTES:
        lines = lines[1:]  # cut mid-line
    answered = False
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        if not out["branch"] and isinstance(entry.get("gitBranch"), str):
            out["branch"] = entry["gitBranch"]
        if entry.get("type") == "last-prompt" and not out["prompt"]:
            out["prompt"] = _short(entry.get("lastPrompt"))
            raw = entry.get("lastPrompt")
            out["title"] = _prompt_title(raw if isinstance(raw, str) else "")
        if entry.get("type") == "assistant" and not answered:
            tool, text = _newest_blocks(entry)
            out["tool"] = out["tool"] or tool
            if text:
                answered, out["last_message"], out["tail"] = True, _short(text), text[-_TAIL_CHARS:]
        if entry.get("type") == "user" and (at := _said_at(entry)) is not None:
            out["said"].append(at)
    return out


def _said(entry: dict[str, Any]) -> str:
    """Allen's own words in a user entry; empty for tool results, commands and notifications."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if entry.get("isMeta") or entry.get("isCompactSummary") or not isinstance(content, str):
        return ""
    text = _PASTED.sub("[Pasted text]", content).strip()
    return "" if text.startswith("<") else text


def _answered(entry: dict[str, Any]) -> str:
    """The newest text in one assistant entry."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    texts = [
        str(b["text"]).strip()
        for b in (content if isinstance(content, list) else [])
        if isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip()
    ]
    return texts[-1][:_MESSAGE_CHARS] if texts else ""


def read_conversation(path: Path) -> list[dict[str, str]]:
    """What Allen said and what the session answered at the end of each turn, oldest first.

    The steps in between (tool calls, the texts before them) are left out;
    a turn still running shows its newest text. Only the transcript's tail
    is read, so a long session starts partway through.
    """
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - _CONVERSATION_BYTES))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    if size > _CONVERSATION_BYTES:
        lines = lines[1:]  # cut mid-line
    out: list[dict[str, str]] = []
    answer = ""
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        if entry.get("type") == "user" and (said := _said(entry)):
            if answer:
                out.append({"who": "it", "text": answer})
            out.append({"who": "you", "text": said[:_MESSAGE_CHARS]})
            answer = ""
        elif entry.get("type") == "assistant":
            answer = _answered(entry) or answer
    if answer:
        out.append({"who": "it", "text": answer})
    return out[-_MESSAGES:]


def said_since(path: Path, offset: int) -> list[str]:
    """Allen's lines written to a transcript after byte ``offset``, whitespace folded."""
    with path.open("rb") as fh:
        fh.seek(offset)
        lines = fh.read().decode("utf-8", "replace").splitlines()
    out: list[str] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("type") == "user" and not entry.get("isSidechain"):
            out += [" ".join(said.split())] if (said := _said(entry)) else []
    return out


def type_into(claude: str, job_id: str, text: str) -> None:
    """Type one line into a background session through an attach no screen shows.

    ``claude attach`` runs on a pseudo-terminal this process owns; the words
    go in, then Enter, then the attach client is closed. The session keeps
    running; a terminal showing it sees the line too.
    """
    # No API key (it would bill the API) and none of a parent session's CLAUDE_* markers;
    # the config directory stays, so attach finds the same sessions `claude agents` lists.
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "ANTHROPIC_API_KEY" and (not k.startswith("CLAUDE") or k == "CLAUDE_CONFIG_DIR")
    }
    main, sub = pty.openpty()
    try:
        fcntl.ioctl(sub, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        child = subprocess.Popen(  # noqa: S603 — fixed argv; the binary is Claude Code's own.
            [claude, "attach", job_id],
            stdin=sub,
            stdout=sub,
            stderr=sub,
            env=env,
            start_new_session=True,
        )
    except OSError:
        os.close(main)
        raise
    finally:
        os.close(sub)

    def drain(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if select.select([main], [], [], 0.1)[0]:
                try:
                    os.read(main, 65536)
                except OSError:
                    return

    try:
        drain(_ATTACH_S)
        os.write(main, " ".join(text.split()).encode())
        time.sleep(0.3)
        os.write(main, b"\r")
        drain(1.0)
    finally:
        child.send_signal(signal.SIGHUP)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
        os.close(main)


def _phase(agent: dict[str, Any], job: dict[str, Any]) -> str:
    """A background job still ``working`` but at tempo ``idle`` only sleeps on a stale wakeup."""
    status, state = agent.get("status"), agent.get("state")
    if status == "waiting" or state == "blocked":
        return "needs_input"
    if status == "busy" or (state == "working" and job.get("tempo") == "active"):
        return "working"
    return "done"


def _terminals(pids: list[int]) -> dict[int, str]:
    """Which terminal app (or multiplexer) each interactive session runs in.

    Raises:
        OSError: there is no ``ps`` to run.
        subprocess.SubprocessError: it did not answer within its time.
    """
    ps = subprocess.run(
        ["/bin/ps", "-Ao", "pid=,ppid=,comm="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    ).stdout
    parent: dict[int, int] = {}
    name: dict[int, str] = {}
    for line in ps.splitlines():
        child, ppid, comm = [*line.split(None, 2), "", "", ""][:3]
        if child.isdigit() and ppid.isdigit():
            parent[int(child)], name[int(child)] = int(ppid), Path(comm).name.lower()
    found: dict[int, str] = {}
    for pid in pids:
        at, where = parent.get(pid, 1), "terminal"
        for _ in range(16):
            label = next((v for k, v in _TERMINALS if name.get(at, "").startswith(k)), None)
            if label or at <= 1:
                where = label or where
                break
            at = parent.get(at, 1)
        found[pid] = where
    return found


class ClaudeSessions:
    """Newest-first Claude Code session rows, rebuilt at most every ``REFRESH_S``."""

    def __init__(
        self,
        asks: Callable[[str, str, bool], bool | None] | None = None,
        answered: Callable[[str, list[float]], None] | None = None,
        device: DeviceLink | None = None,
    ) -> None:
        """Find ``claude`` once; launchd's PATH lacks ``~/.local/bin``.

        ``device`` (ADR 0170: this is a brain) is the link to the terminal whose Claude Code it
        is; every read and the reply go there, and this machine's own files are never touched.

        ``asks(session, tail, send)`` (ADR 0125) tells, without waiting, whether a finished
        session's last message asks Allen something, asking Jev only when ``send``; its row
        says so as ``asks``, or None while that is not known. ``answered(session, times)`` (ADR
        0128) is told when Allen sent each message in a session's transcript.
        """
        self._asks = asks
        self._answered = answered
        self._device = device
        self._home = Path.home()
        self._claude = shutil.which("claude") or str(self._home / ".local" / "bin" / "claude")
        self._lock = threading.Lock()
        self._at = -REFRESH_S
        self._board: dict[str, Any] = {"sessions": [], "error": None}
        self._transcripts: dict[Path, tuple[int, dict[str, Any]]] = {}
        # The (title, start) each transcript's opening gives, once its title is found.
        self._openings: dict[Path, tuple[str, int]] = {}
        # Claude Desktop's session files: path -> (mtime, CLI session id, title).
        self._desktop: dict[Path, tuple[int, str, str]] = {}
        self._reply_lock = threading.Lock()
        self._ps_down = False

    def read(self) -> dict[str, Any]:
        """Blocking (a subprocess and file reads, or the terminal): call it from a worker thread."""
        with self._lock:
            if time.monotonic() - self._at >= REFRESH_S:
                raw = self._remote_board() if self._device is not None else self._collect()
                self._board = self._annotate(raw)
                self._at = time.monotonic()
            return self._board

    def raw(self) -> dict[str, Any]:
        """The board as this machine sees it, before anything is asked of a model.

        A terminal's answer to the brain's ``board`` read: each row's finished tail and the times
        Allen spoke ride beside the rows in ``extra``, for the brain to judge and note.
        """
        with self._lock:
            return self._collect()

    def _remote_board(self) -> dict[str, Any]:
        """The terminal's raw board, or an empty one that says why it could not be had."""
        assert self._device is not None  # noqa: S101 — the caller checked.
        try:
            board: dict[str, Any] = device_reads.ask(
                self._device, device_reads.CLAUDE_READ, "board",
            )
        except (device_reads.DeviceUnavailable, DailyError) as exc:
            return {"sessions": [], "error": str(exc)[:200]}
        return board

    def _annotate(self, board: dict[str, Any]) -> dict[str, Any]:
        """Tell Jev about the finishes and Allen's lines (ADR 0125, 0128); drop ``extra``."""
        extra = board.pop("extra", {})
        now_ms = int(time.time() * 1000)
        for row in board["sessions"]:
            material = extra.get(row["session_id"], {})
            said, tail = material.get("said"), material.get("tail")
            if self._answered and said:
                self._answered(row["session_id"], said)
            # An answer already in stays; Jev is asked only while the finish is news.
            if self._asks and row["phase"] == "done" and tail:
                row["asks"] = self._asks(
                    row["session_id"], tail, now_ms - row["updated_ms"] < _ASK_WINDOW_MS,
                )
        return board

    def _path(self, session_id: str) -> Path | None:
        if not SESSION_ID.fullmatch(session_id):
            return None
        return next(iter((self._home / ".claude" / "projects").glob(f"*/{session_id}.jsonl")), None)

    def _remote(self, fn: str, /, **args: Any) -> Any:  # noqa: ANN401 — the terminal's JSON.
        """One read or the reply on the terminal, its refusals as the local call raises them.

        Raises:
            LookupError: the terminal has no such session (or none that can take a reply).
            ConnectionError: no terminal, or it reads no Claude Code files; the plain sentence.
            RuntimeError: the terminal typed the line and it never reached the transcript.
        """
        assert self._device is not None  # noqa: S101 — the callers checked.
        try:
            return device_reads.ask(self._device, device_reads.CLAUDE_READ, fn, **args)
        except device_reads.DeviceUnavailable as exc:
            raise ConnectionError(str(exc)) from exc
        except DailyError as exc:
            if exc.code == "not_found":
                raise LookupError(str(exc)) from exc
            if exc.code == "disabled":
                raise ConnectionError(str(exc)) from exc
            raise RuntimeError(str(exc)) from exc

    def conversation(self, session_id: str) -> dict[str, Any]:
        """Blocking. ``{"messages": [{who, text}]}``; LookupError for an unknown session."""
        if self._device is not None:
            reply: dict[str, Any] = self._remote("conversation", session_id=session_id)
            return reply
        path = self._path(session_id)
        if path is None:
            raise LookupError(session_id)
        return {"messages": read_conversation(path)}

    def reply(self, session_id: str, text: str) -> None:
        """Blocking, several seconds. Type ``text`` into an idle background session.

        LookupError: no such session on the board, or it cannot take a reply
        now (interactive, working, or a dialog open). RuntimeError: the line
        was typed but never reached the transcript.
        """
        if self._device is not None:
            self._remote("reply", session_id=session_id, text=text)
            self._at = -REFRESH_S  # the next board read shows it working
            return
        line = " ".join(text.split())
        with self._reply_lock:
            # On a fresh board, under the lock: a reply queued behind another finds it working.
            self._at = -REFRESH_S
            row = next((r for r in self.read()["sessions"] if r["session_id"] == session_id), None)
            path = self._path(session_id)
            if row is None or not row["replyable"] or path is None:
                raise LookupError(session_id)
            start = path.stat().st_size
            type_into(self._claude, row["job_id"], line)
            end = time.monotonic() + 4
            while time.monotonic() < end:
                # A new entry, not an old line that happens to read the same ("continue").
                if line in said_since(path, start):
                    self._at = -REFRESH_S  # the next board read shows it working
                    return
                time.sleep(0.3)
        msg = "the reply did not reach the session"
        raise RuntimeError(msg)

    def _transcript(self, path: Path | None, mtime_ns: int) -> dict[str, Any]:
        if path is None:
            return {
                "prompt": "", "branch": "", "tool": "", "last_message": "", "tail": "", "said": [],
                "title": "", "started_ms": 0,
            }
        cached = self._transcripts.get(path)
        if cached is None or cached[0] != mtime_ns:
            tx = read_transcript(path)
            # A session's opening never changes: read once from the head of what can be a
            # transcript of many megabytes, then kept; the last prompt stands in until then.
            opening = self._openings.get(path)
            if opening is None:
                opening = _opening(path)
                if opening[0]:
                    self._openings[path] = opening
            tx["title"] = opening[0] or tx["title"]
            tx["started_ms"] = opening[1]
            cached = self._transcripts[path] = (mtime_ns, tx)
        return cached[1]

    def _desktop_titles(self) -> dict[str, str]:
        """Claude Desktop's title for each CLI session id it knows; a file is re-read on change."""
        seen: dict[Path, tuple[int, str, str]] = {}
        for path in (self._home / _DESKTOP_SESSIONS).rglob("local_*.json"):
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                continue
            cached = self._desktop.get(path)
            if cached is None or cached[0] != mtime_ns:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = None
                data = data if isinstance(data, dict) else {}
                cached = (mtime_ns, str(data.get("cliSessionId") or ""), _named(data.get("title")))
            seen[path] = cached
        self._desktop = seen
        return {sid: title for _, sid, title in seen.values() if sid and title}

    def _job(self, job_id: object) -> dict[str, Any]:
        try:
            state = json.loads(
                (self._home / ".claude" / "jobs" / str(job_id) / "state.json").read_text()
            )
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def _where(self, pids: list[int]) -> dict[int, str]:
        """The terminal of each interactive session; none while ``ps`` does not answer.

        The label only tells where to find a session, so a ``ps`` that stalls leaves the rows
        unlabelled for that poll instead of failing the board. The warning is one per outage: a
        new one is told only after ``ps`` has answered again.
        """
        if not pids:
            return {}
        try:
            found = _terminals(pids)
        except (OSError, subprocess.SubprocessError) as exc:
            if not self._ps_down:
                LOGGER.warning(
                    "claude sessions: ps did not answer (%s); no terminal is named until it does",
                    type(exc).__name__,
                )
            self._ps_down = True
            return {}
        self._ps_down = False
        return found

    def _collect(self) -> dict[str, Any]:
        try:
            done = subprocess.run(  # noqa: S603 — fixed argv; the binary is Claude Code's own.
                [self._claude, "agents", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            agents = [
                a
                for a in json.loads(done.stdout)
                if isinstance(a, dict) and isinstance(a.get("sessionId"), str)
            ]
        except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
            return {"sessions": [], "error": f"claude agents --json: {exc}"[:200], "extra": {}}
        now_ms = int(time.time() * 1000)
        where = self._where(
            [
                a["pid"]
                for a in agents
                if a.get("kind") == "interactive" and isinstance(a.get("pid"), int)
            ]
        )
        projects = self._home / ".claude" / "projects"
        titles = self._desktop_titles()
        rows: list[ClaudeSession] = []
        extra: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        # A session open in two processes (a terminal still on one Startrail took back) is one
        # row, the live one: two rows under one id flip the companion's notices between them.
        agents.sort(key=lambda a: a.get("status") not in {"busy", "waiting"})
        for agent in agents:
            if agent["sessionId"] in seen:
                continue
            job = self._job(agent.get("id")) if agent.get("kind") == "background" else {}
            path = next(iter(projects.glob(f"*/{agent['sessionId']}.jsonl")), None)
            mtime_ns = path.stat().st_mtime_ns if path else 0
            updated_ms = max(
                mtime_ns // 1_000_000,
                _iso_ms(job.get("updatedAt")),
                int(agent.get("startedAt") or 0),
            )
            live = agent.get("status") in {"busy", "waiting"}
            if not live and now_ms - updated_ms >= RETENTION_MS:
                continue
            seen.add(agent["sessionId"])
            tx = self._transcript(path, mtime_ns)
            cwd = str(agent.get("cwd") or "")
            phase = _phase(agent, job)
            extra[agent["sessionId"]] = {"tail": tx["tail"], "said": tx["said"]}
            rows.append(
                {
                    "agent": "claude",
                    "session_id": agent["sessionId"],
                    "kind": agent.get("kind", ""),
                    # What `claude attach` takes; not always the session id's first 8 characters.
                    "job_id": str(agent.get("id") or "")
                    if agent.get("kind") == "background"
                    else "",
                    "phase": phase,
                    "title": titles.get(agent["sessionId"])
                    or _named(agent.get("name"))
                    or tx["title"]
                    or _project(cwd),
                    "project": _project(cwd),
                    "branch": tx["branch"],
                    "cwd": cwd,
                    # ADR 0153: a project thread's session (run in ~/Projects) asks in that
                    # thread, so the notch does not queue it.
                    "from_project": cwd == str(Path.home() / "Projects"),
                    "where": "background"
                    if agent.get("kind") == "background"
                    else where.get(agent.get("pid", 0), "terminal"),
                    "prompt": tx["prompt"] or _short(job.get("intent")),
                    "activity": _short(job.get("detail"))
                    or tx["tool"]
                    or _short(agent.get("waitingFor")),
                    "last_message": tx["last_message"],
                    "asks": None,
                    # Idle at its input box: a line typed through a hidden attach lands there.
                    "replyable": agent.get("kind") == "background"
                    and agent.get("status") == "idle"
                    and bool(agent.get("id")),
                    # When it began: the transcript's first moment, else its process's start.
                    "started_ms": tx["started_ms"] or int(agent.get("startedAt") or 0) or None,
                    "updated_ms": updated_ms,
                }
            )
        self._transcripts = {p: v for p, v in self._transcripts.items() if p.stem in seen}
        self._openings = {p: v for p, v in self._openings.items() if p.stem in seen}
        rows.sort(key=lambda r: r["updated_ms"], reverse=True)
        return {"sessions": rows, "error": None, "extra": extra}
