r"""Tool exam, tool layer (ADR 0317): one tool through the real dispatcher, no model, no network.

    .venv/bin/python -m scripts.tool_exam_tools run    [--items DIR] [--only ID,...]
    .venv/bin/python -m scripts.tool_exam_tools score  [--items DIR]
    .venv/bin/python -m scripts.tool_exam_tools record ITEM.json [--root ~/.jarvis]

``run`` prints every item's failures; ``score`` prints the counts and the verdict against the
baseline and exits 1 on a regression; ``record`` runs one item live and stores what each provider
answered. ``--items`` defaults to ``~/.jarvis/evals/tool-exam/tool-items/work``. The ``held``
sibling is read only by ``score`` (``--items .../held``), which prints counts and ids, never
an item.

An item (JSON, one per file) holds his words and the moment, place and data they were said in:

- ``id``, ``tool``, ``said``, ``args``; optional ``layer`` (``tool``, the default).
- ``at``: ISO time with offset; the wall clock is frozen there. ``zone``: IANA zone of his day.
- ``here``: optional ``{lat, lng, age_s?, place?, accuracy_m?}``, the phone's last report.
- ``state``: a backup directory name under ``~/.jarvis/backups/pi-brain``, a fixture directory
  (relative to the item's directory or the repo), or null for empty databases. A directory holds
  ``mac_events.db`` / ``memory.db`` (copied before the boot) and/or ``mac_events.sql`` /
  ``memory.sql`` (run after it). Rows made at or after ``at`` are deleted from the copied
  databases first (:func:`cut_at`); the run prints the tables that cut could not handle.
- ``job_mail``: optional, the job hunt he has at ``at`` (the recording's ``settings`` must turn
  ``job_mail`` on, and the sandbox has no Gmail): ``{mails: [...], applications: [...],
  read_at?, failing_since?}``. A mail is a ``job_mail`` row (``message_id``, ``received_at``,
  ``kind``, ``company``, ``role``, ``sender_name``, ``sender_domain``, ``subject``, and for an
  interview ``event_at``, ``event_later_at``, ``event_text``); an application is one he told her
  or added (``company``, ``role``, ``applied_at``, ``status``, ``source``, ``hidden``). They are
  written through the ledger's own functions. ``read_at`` is when job mail was last read and
  ``failing_since`` when reading began to fail (absent: not read since the sandbox started). A
  ``job_mail.json`` of this shape in the item's ``state`` directory is read first.
- ``ride``: optional, a followed bus ride he is on at ``at``:
  ``{route, trip_id, stops: [{seq, stop_id, name, lat, lng}]}``; the follower's timetable for the
  trip is those stops.
- ``setup``: earlier calls, ``[{tool, args}]``, made through the same dispatcher.
- ``recording``: path (relative to the item's directory) of what each provider answered, or null.
  A provider call the recording lacks fails the item.
- ``expect``:
  - ``summary_must[]`` / ``summary_must_not[]``: regexes on the result's ``summary`` (an
    error's text).
  - ``result_must_not_mention[]``: texts the result's JSON must not hold (any case).
  - ``result[]``: ``{path, op, value?}`` on the decoded result; ``path`` is dotted with list
    indexes (``items.0.title``); ``op`` is eq, ne, contains, not_contains, matches, in, gt, gte,
    lt, lte, len, exists or absent.
  - ``state_after[]``: ``{db: events|memory, sql, answer}``; ``answer`` is the rows (a list of
    lists) or one value for a single-cell query.
  - ``events_after``: ``{must[], must_not[]}``; an entry is an event type, or
    ``{type, payload_matches}`` (a regex on the payload), among events the call wrote.
  - ``receipt``: fields the call's receipt must hold (ADR 0319), e.g.
    ``{"status": "needs_choice"}``.
  - ``error_kind``: the kind the call must fail with (``needs_him``, ``impossible``,
    ``retry_other_args`` or ``dont_retry``); without it an error fails the item.
  - ``block_d_submitted``: true when the result's "At least N submitted" must be the N block D
    states in its own submitted line, rendered over the sandbox's data at ``at``.
  - ``checker``: name of a reference checker in ``scripts/tool_exam_checkers.py`` that derives the
    facts from the recording.

Every result also passes :func:`assert_tool_result`, the spec's rules for a result and an error.
The ``baseline.json`` beside the items lists the ids that fail; a run fails when an item outside it
fails, and names a listed item that now passes, so the list only shrinks.

Recordings are scrubbed of every credential before they are written, and the recorder checks in
memory that no key value is in the bytes it writes; it never prints one.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from unittest.mock import patch
from zoneinfo import ZoneInfo

import time_machine
import yaml
from mcp import types as mcp_types

from jarvis.deployment import load_env_file
from jarvis.execution.mcp_tools import McpServers
from jarvis.execution.tools import ActionLifecycle, release_turn_actions
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.shared.receipts import call_risk
from jarvis.state import job_ledger
from jarvis.state.event_log import emit_event
from jarvis.state.ledger import LedgerSources, job_hunt_text
from jarvis.state.ride_progress import StopTime
from jarvis.state.rides import Ride
from jarvis.state.rides import write_start as write_ride_start
from tests.support.runtime_config import boot_runtime

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from jarvis.runtime import JarvisRuntime

REPO = Path(__file__).resolve().parent.parent
ITEMS = Path("~/.jarvis/evals/tool-exam/tool-items").expanduser()
BACKUPS = Path("~/.jarvis/backups/pi-brain").expanduser()
BASELINE = "baseline.json"
ITEM_TIMEOUT_S = 60
RESULT_CHARS = 4000
SUMMARY_CHARS = 400

# --- the spec's rules for a result and an error (docs/spec.html#tools) -------------------------

ERROR_KINDS = ("needs_him", "impossible", "retry_other_args", "dont_retry")
"""The four kinds of error: he must act, impossible, retry with other arguments, do not retry."""
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_HASH = re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE)
_PATH = re.compile(r"(?:^|[\s\"'(=])(?:~|/)?/[\w.@+-]+(?:/[\w.@+-]+)+")
_HTML = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?>")


def _strings(value: object, path: str = "") -> Iterator[tuple[str, str]]:
    """Every string in a decoded result, with where it sits (keys are not walked)."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, one in value.items():
            yield from _strings(one, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list | tuple):
        for i, one in enumerate(value):
            yield from _strings(one, f"{path}.{i}" if path else str(i))


def result_problems(  # noqa: C901 - one pass over every rule of the spec
    out: str | Mapping[str, Any],
) -> list[str]:
    """What breaks the spec's rules for this tool result (the dispatcher's JSON or its decoding)."""
    raw = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    problems: list[str] = []
    if len(raw) > RESULT_CHARS:
        problems.append(f"result is {len(raw)} characters, over about {RESULT_CHARS}")
    try:
        decoded = json.loads(raw) if isinstance(out, str) else dict(out)
    except ValueError:
        return [*problems, "result is not JSON"]
    if not isinstance(decoded, dict) or not decoded:
        return [*problems, "result is not a JSON object"]
    if "error" in decoded:
        problems += _error_problems(decoded)
    else:
        first = next(iter(decoded))
        summary = decoded.get("summary")
        if first != "summary":
            problems.append(f"the first field is {first!r}, not a speakable summary")
        elif not isinstance(summary, str) or not summary.strip():
            problems.append("summary is empty")
        elif "\n" in summary or "{" in summary or len(summary) > SUMMARY_CHARS:
            problems.append("summary is not one speakable line (a newline, a brace, or too long)")
    for where, text in _strings(decoded):
        for rule, pattern in (("uuid", _UUID), ("hash", _HASH), ("path", _PATH), ("html", _HTML)):
            if pattern.search(text):
                problems.append(f"{where} holds a {rule}")
        if text.lstrip()[:1] in "{[":
            with contextlib.suppress(ValueError):
                json.loads(text)
                problems.append(f"{where} holds JSON inside JSON")
    return problems


def _error_problems(decoded: Mapping[str, Any]) -> list[str]:
    """An error says what happened and what to do next, and is one of the four kinds."""
    text, kind = decoded.get("error"), decoded.get("kind")
    problems = []
    if not isinstance(text, str) or not text.strip():
        problems.append("error text is empty")
    if kind not in ERROR_KINDS:
        problems.append(f"error kind {kind!r} is not one of the four kinds {ERROR_KINDS}")
    return problems


def assert_tool_result(out: str | Mapping[str, Any]) -> None:
    """Raise AssertionError listing every rule this tool result breaks."""
    problems = result_problems(out)
    if problems:
        raise AssertionError("; ".join(problems))


# --- recordings: what each provider answered ----------------------------------------------------

_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.IGNORECASE)
_SECRET_PARAM = re.compile(
    r"^(key|api[-_]?key|apikey|token|access[-_]?token|sig|signature|auth)$", re.IGNORECASE
)
_KEPT_HEADERS = frozenset({"content-type"})
_SCRUBBED = "<scrubbed>"
_MIN_SECRET = 6


class RecordingMiss(OSError):  # noqa: N818 - reads as the fact it states
    """A provider call the item's recording does not hold (the item fails)."""


def scrub_url(url: str) -> str:
    """``url`` without the query parameters that carry a credential."""
    parts = urllib.parse.urlsplit(url)
    pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not _SECRET_PARAM.match(k)
    ]
    query = "&".join(
        f"{urllib.parse.quote(k, safe=',:/')}={urllib.parse.quote(v, safe=',:/')}" for k, v in pairs
    )
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def secret_values() -> dict[str, str]:
    """Environment variable name -> value for every value the program may read as a key."""
    return {n: v for n, v in os.environ.items() if _SECRET_NAME.search(n) and len(v) >= _MIN_SECRET}


def _hash(data: bytes | None) -> str:
    return hashlib.sha256(data or b"").hexdigest()[:16]


def _request_of(url: object, data: object) -> tuple[str, str, bytes]:
    """``(method, url, body)`` of what ``urlopen`` was given: a string or a Request."""
    if isinstance(url, urllib.request.Request):
        body = (
            url.data
            if isinstance(url.data, bytes)
            else (str(url.data).encode() if url.data is not None else b"")
        )
        return url.get_method(), url.full_url, body
    body = data if isinstance(data, bytes) else b""
    return ("POST" if body else "GET"), str(url), body


class _Reply(io.BytesIO):
    """What ``urlopen`` returns: a readable body with a status and a content type."""

    def __init__(self, body: bytes, status: int, content_type: str, url: str) -> None:
        super().__init__(body)
        self.status = self.code = status
        self.url = url
        self.headers = Message()
        if content_type:
            self.headers["Content-Type"] = content_type

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.url

    def info(self) -> Message:
        return self.headers

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class Recording:
    """What each provider answered for one item: HTTP replies and MCP tool results."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        """Hold ``data`` (a loaded recording), or an empty one."""
        self.data: dict[str, Any] = {"version": 1, "settings": {}, "env": [], "http": [], "mcp": []}
        self.data.update(data or {})
        for entry in self.data["http"]:  # a hand-written entry gives its request body, not a hash
            entry.setdefault("body_sha", _hash(entry.get("request", "").encode()))

    @classmethod
    def load(cls, path: Path) -> Recording:
        """Read a recording from its file."""
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @property
    def settings(self) -> dict[str, Any]:
        """The settings the recorded URLs depend on."""
        return dict(self.data["settings"])

    @property
    def env(self) -> list[str]:
        """Names of the environment variables the program read as keys."""
        return list(self.data["env"])

    def http(self, method: str, url: str, body: bytes) -> dict[str, Any] | None:
        """The recorded HTTP exchange for this request, or None."""
        want = (method.upper(), scrub_url(url), _hash(body))
        return next(
            (e for e in self.data["http"] if (e["method"], e["url"], e["body_sha"]) == want), None
        )

    def mcp(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any] | None:
        """The recorded MCP tool call, or None."""
        want = json.dumps(args, sort_keys=True)
        return next(
            (
                e
                for e in self.data["mcp"]
                if (e["server"], e["tool"], json.dumps(e["args"], sort_keys=True))
                == (server, tool, want)
            ),
            None,
        )

    def save(self, path: Path, secrets: Mapping[str, str]) -> None:
        """Write the recording after checking in memory that no credential is in the bytes."""
        blob = json.dumps(self.data, indent=1, ensure_ascii=False)
        raw = blob.encode()
        for name, value in secrets.items():
            forms = {value, urllib.parse.quote(value), urllib.parse.quote(value, safe="")}
            forms.add(base64.b64encode(value.encode()).decode())
            if any(f.encode() in raw for f in forms if len(f) >= _MIN_SECRET):
                msg = f"refusing to write {path.name}: it would hold the value of {name}"
                raise SystemExit(msg)
        path.write_text(blob + "\n", encoding="utf-8")


def _scrub(text: str, secrets: Mapping[str, str]) -> str:
    for value in secrets.values():
        text = text.replace(value, _SCRUBBED)
    return text


def load_listing(path: Path) -> dict[str, list[mcp_types.Tool]]:
    """Each MCP server's tools as the server lists them, from an ``mcp-tools.json``.

    The file maps ``mcp__<server>__<tool>`` to ``{description, input_schema, annotations?}``; a
    tool without ``annotations`` is listed with none, as a server that gives none lists it.
    """
    listed: dict[str, list[mcp_types.Tool]] = {}
    for key, one in json.loads(path.read_text(encoding="utf-8")).items():
        _, server, tool = key.split("__", 2)
        listed.setdefault(server, []).append(
            mcp_types.Tool.model_validate({
                "name": tool,
                "description": one.get("description"),
                "inputSchema": one["input_schema"],
                "annotations": one.get("annotations"),
            })
        )
    return listed


@dataclass(frozen=True)
class _Listed:
    """The client of a server whose listing is recorded: no process or connection behind it."""

    server: str


class _Transport:
    """Patches the provider boundaries: ``urlopen`` and the MCP client's listing and tool call.

    ``replay`` answers from the recording and fails any call it lacks; ``record`` makes the call
    and stores the answer. Everything else that opens a socket is refused while replaying, except
    the exam's own model request inside :meth:`model_call`. While replaying, an MCP server in
    ``listed`` connects as if logged in, lists the recorded tools and never starts; any other
    server is refused.
    """

    def __init__(
        self,
        recording: Recording,
        *,
        live: bool,
        listed: Mapping[str, Sequence[mcp_types.Tool]] | None = None,
    ) -> None:
        self.recording = recording
        self.live = live
        self.listed = dict(listed or {})
        self.misses: list[str] = []
        self.secrets: dict[str, str] = secret_values()
        self._real_urlopen = urllib.request.urlopen
        self._stack = contextlib.ExitStack()
        self._open = threading.local()

    def __enter__(self) -> Self:
        real_call, real_direct = McpServers._call, McpServers.call  # noqa: SLF001

        def call(
            srv: McpServers, server: str, client: object, tool: str, args: Mapping[str, Any]
        ) -> dict[str, Any]:
            return self._mcp(server, tool, args, lambda: real_call(srv, server, client, tool, args))  # type: ignore[arg-type]

        def direct(
            srv: McpServers, server: str, tool: str, args: Mapping[str, Any]
        ) -> dict[str, Any]:
            return self._mcp(server, tool, args, lambda: real_direct(srv, server, tool, args))

        def opened(_srv: McpServers, server: str, _spec: Mapping[str, Any]) -> _Listed:
            if server not in self.listed:
                _refuse()
            return _Listed(server)

        async def listing(_srv: McpServers, client: _Listed) -> list[mcp_types.Tool]:
            return list(self.listed[client.server])

        def logged_in(_srv: McpServers, server: str) -> bool:
            return server in self.listed

        self._stack.enter_context(patch.object(urllib.request, "urlopen", self._urlopen))
        self._stack.enter_context(patch.object(McpServers, "_call", call))
        if not self.live:  # a call outside a model turn skips the "connected" check when replayed
            self._stack.enter_context(patch.object(McpServers, "call", direct))
            self._stack.enter_context(patch.object(McpServers, "_open", opened))
            self._stack.enter_context(patch.object(McpServers, "_list", listing))
            self._stack.enter_context(patch.object(McpServers, "has_login", logged_in))
            for owner, name in ((socket.socket, "connect"), (socket, "getaddrinfo")):
                self._stack.enter_context(
                    patch.object(owner, name, self._gate(getattr(owner, name)))
                )
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stack.close()

    def _gate(self, real: Callable[..., Any]) -> Callable[..., Any]:
        """``real`` inside :meth:`model_call` on this thread; refused everywhere else."""

        def gated(*args: Any, **kw: Any) -> Any:  # noqa: ANN401 - the socket call as it is
            if getattr(self._open, "model", False):
                return real(*args, **kw)
            return _refuse()

        return gated

    @contextmanager
    def model_call(self) -> Iterator[None]:
        """Open the network to this thread for the exam's own model request; tools stay recorded.

        The request is synchronous, so its sockets open on the calling thread while every other
        thread, and this one before and after, stays refused.
        """
        self._open.model = True
        try:
            yield
        finally:
            self._open.model = False

    def _urlopen(self, url: object, data: object = None, *args: object, **kw: object) -> _Reply:
        method, full, body = _request_of(url, data)
        if self.live:
            return self._record(url, data, args, kw, method, full, body)
        entry = self.recording.http(method, full, body)
        if entry is None:
            shown = body.decode("utf-8", "replace")[:400]
            self.misses.append(f"{method} {scrub_url(full)} body#{_hash(body)} {shown}")
            msg = f"the recording has no answer for {method} {scrub_url(full)}"
            raise RecordingMiss(msg)
        payload = (
            base64.b64decode(entry["body_b64"]) if "body_b64" in entry else entry["body"].encode()
        )
        if entry["status"] >= 400:  # noqa: PLR2004 - HTTP
            raise urllib.error.HTTPError(
                full,
                entry["status"],
                "recorded",
                Message(),
                io.BytesIO(payload),
            )
        return _Reply(payload, entry["status"], entry.get("content_type", ""), full)

    def _record(  # noqa: PLR0913 - the call as made and what it was reduced to
        self,
        url: object,
        data: object,
        args: tuple[object, ...],
        kw: dict[str, object],
        method: str,
        full: str,
        body: bytes,
    ) -> _Reply:
        headers = dict(url.header_items()) if isinstance(url, urllib.request.Request) else {}
        self.secrets.update(
            {
                f"header:{k}": v.removeprefix("Bearer ").strip()
                for k, v in headers.items()
                if _SECRET_NAME.search(k) and len(v.removeprefix("Bearer ").strip()) >= _MIN_SECRET
            }
        )
        status, payload, ctype = 200, b"", ""
        try:
            with self._real_urlopen(url, data, *args, **kw) as reply:  # type: ignore[call-overload]
                payload, status = reply.read(), reply.status
                ctype = reply.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            payload, status, ctype = exc.read(), exc.code, exc.headers.get("Content-Type", "")
            self._store(method, full, body, headers, status, payload, ctype)
            raise urllib.error.HTTPError(
                full, status, exc.msg, exc.headers, io.BytesIO(payload)
            ) from None
        self._store(method, full, body, headers, status, payload, ctype)
        return _Reply(payload, status, ctype, full)

    def _store(  # noqa: PLR0913 - one HTTP exchange
        self,
        method: str,
        full: str,
        body: bytes,
        headers: Mapping[str, str],
        status: int,
        payload: bytes,
        ctype: str,
    ) -> None:
        raw_request = full + body.decode("utf-8", "replace") + "".join(headers.values())
        for name, value in self.secrets.items():
            if value in raw_request and not name.startswith("header:"):
                names = self.recording.data["env"]
                if name not in names:
                    names.append(name)
        entry: dict[str, Any] = {
            "method": method,
            "url": _scrub(scrub_url(full), self.secrets),
            "body_sha": _hash(body),
            "status": status,
            "content_type": ctype,
        }
        if body:
            entry["request"] = _scrub(body.decode("utf-8", "replace"), self.secrets)
        try:
            entry["body"] = _scrub(payload.decode("utf-8"), self.secrets)
        except UnicodeDecodeError:
            entry["body_b64"] = base64.b64encode(payload).decode()
        self.recording.data["http"].append(entry)

    def _mcp(
        self, server: str, tool: str, args: Mapping[str, Any], live: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        if self.live:
            made = live()
            self.recording.data["mcp"].append(
                {
                    "server": server,
                    "tool": tool,
                    "args": dict(args),
                    "result": json.loads(_scrub(json.dumps(made), self.secrets)),
                }
            )
            return made
        entry = self.recording.mcp(server, tool, args)
        if entry is None:
            shown = json.dumps(args, sort_keys=True, ensure_ascii=False)[:400]
            self.misses.append(f"mcp {server}.{tool} {shown}")
            msg = f"the recording has no answer for mcp {server}.{tool}"
            raise RecordingMiss(msg)
        result: dict[str, Any] = entry["result"]
        return result


def _refuse(*_a: object, **_k: object) -> None:
    msg = "network blocked: the tool layer runs from recordings"
    raise OSError(msg)


# --- the sandbox ---------------------------------------------------------------------------------


@dataclass
class Item:
    """One exam item and where it was read from."""

    raw: dict[str, Any]
    path: Path

    @property
    def id(self) -> str:
        """The item's id."""
        return str(self.raw["id"])


def load_items(directory: Path) -> list[Item]:
    """Every ``*.json`` item in ``directory`` (not the baseline or a recording), in name order."""
    return [
        Item(json.loads(p.read_text(encoding="utf-8")), p)
        for p in sorted(directory.glob("*.json"))
        if p.name != BASELINE and not p.name.endswith(".recording.json")
    ]


def _state_dir(item: Item) -> Path | None:
    name = item.raw.get("state")
    if not name:
        return None
    for base in (BACKUPS, item.path.parent, REPO):
        if (base / name).is_dir():
            return Path(base / name)
    msg = f"no state directory {name!r} (looked in the backups, beside the item, in the repo)"
    raise LookupError(msg)


def _ephemeral_weather_key(root: Path) -> dict[str, Any]:
    """A throwaway signing key, so WeatherKit is on in the sandbox without the real one."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key = root / "weatherkit-exam.p8"
    key.write_bytes(pem)
    key.chmod(0o600)
    return {
        "weatherkit": {"key_id": "EXAMKEY000", "service_id": "exam.service", "key_file": str(key)}
    }


def _settings(
    item: Item, root: Path, recording: Recording | None, real_root: Path | None, *, live: bool
) -> None:
    """The sandbox's ``settings.yaml``: a brain on his zone, WeatherKit on, the recorded places.

    Only a live run signs WeatherKit with the real key; a replayed answer needs none, and the real
    key file is out of reach under the sandbox's home, which would leave the weather off.
    """
    zone = str(item.raw.get("zone") or "UTC")
    settings: dict[str, Any] = {}
    signed = False
    if real_root is not None and (real_root / "settings.yaml").is_file():
        settings = yaml.safe_load((real_root / "settings.yaml").read_text(encoding="utf-8")) or {}
        signed = live
    else:
        settings.update(recording.settings if recording else {})
    if not signed:
        settings["weather"] = {**(settings.get("weather") or {}), **_ephemeral_weather_key(root)}
    settings["runtime"] = {"role": "brain"}
    settings.setdefault("work_state", {})["timezone"] = zone
    settings.setdefault("observer", {})["claude_sessions"] = {"enabled": False}
    (root / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")


@dataclass
class Box:
    """A booted sandbox: the runtime, its root and what the item named."""

    runtime: JarvisRuntime
    root: Path
    item: Item
    clock: Any
    """The frozen clock; ``clock.shift(seconds=n)`` lets time pass between two calls."""
    uncut: list[str] = field(default_factory=list)
    """The tables of the copied databases the cut at ``at`` could not handle."""


@contextmanager
def _environment(
    item: Item, tmp: Path, recording: Recording | None, *, live: bool
) -> Iterator[None]:
    """Process environment for the sandbox: his zone, a home of its own, placeholder keys."""
    zone = str(item.raw.get("zone") or "UTC")
    set_env = {"TZ": zone, "JARVIS_RUNTIME_ROOT": str(tmp / "root")}
    if not live:
        set_env["HOME"] = str(tmp / "home")
        (tmp / "home").mkdir()
        for name in recording.env if recording else []:
            set_env[name] = "exam-placeholder-key"
    saved = {k: os.environ.get(k) for k in set_env}
    os.environ.update(set_env)
    time.tzset()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        time.tzset()


UNCUT: set[str] = set()
"""Every ``db:table`` the cut left as it was in this process, for the run's header."""
_DAY_COLUMNS = frozenset({"day", "local_date", "upto_day"})
_KEPT_COLUMNS = frozenset({"updated_at", "event_at"})
"""A row edited later, or about a time ahead (an interview), is not made after the cut."""
_MADE_COLUMNS = (
    "ts_epoch_ms", "created_at_ms", "created_at", "ts", "written_at", "computed_at",
    "recorded_at_ms", "received_at", "shown_at",
)
"""Stamps of when a row was made, in order of preference. A table with one is cut by it alone:
the day, stretch or later time a row is about does not make the row later than it was written."""


def _cut_tests(db: sqlite3.Connection, table: str, moment: datetime, zone: str) -> list[str]:
    """The SQL conditions that mark a row of ``table`` as made at or after ``moment``."""
    day = moment.astimezone(ZoneInfo(zone)).date().isoformat()
    utc = moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    tests = []
    columns = [col for _, col, *_rest in db.execute(f'PRAGMA table_info("{table}")').fetchall()]
    made = next((col for col in _MADE_COLUMNS if col in columns), None)
    for col in [made] if made else columns:
        if col in _KEPT_COLUMNS:
            continue
        if col.endswith("_ms"):
            tests.append(f'"{col}" >= {int(moment.timestamp() * 1000)}')
        elif col in _DAY_COLUMNS:
            tests.append(f"\"{col}\" >= '{day}'")
        elif col == "ts" or col.endswith("_at"):
            tests.append(f"datetime(\"{col}\") >= '{utc}'")
    return tests


def cut_at(path: Path, moment: datetime, zone: str) -> list[str]:
    """Delete from the database copy at ``path`` every row made at or after ``moment``.

    A table with a stamp of when its rows were made (:data:`_MADE_COLUMNS`) is cut by that stamp
    alone. Any other table is cut by every column that counts, a row going when one of them is at
    or after the moment: a millisecond epoch (``*_ms``), a stamp (``ts``, ``*_at``) or a day
    (``day``, ``local_date``, ``upto_day``; the days on or after ``moment``'s day in ``zone`` go).
    The append-only triggers are dropped for the cut and put back. Returns the non-empty tables
    with no such column, which the cut left as they were.
    """
    db = sqlite3.connect(path)
    try:
        triggers = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        for name, _ in triggers:
            db.execute(f'DROP TRIGGER "{name}"')
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        uncut = []
        for (table,) in tables:  # names come from the copy's own schema
            if tests := _cut_tests(db, table, moment, zone):
                db.execute(f'DELETE FROM "{table}" WHERE ' + " OR ".join(tests))  # noqa: S608
            elif db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():  # noqa: S608
                uncut.append(table)
        for _, sql in triggers:
            db.execute(sql)
        db.commit()
    finally:
        db.close()
    return uncut


def _copy_state(item: Item, root: Path) -> tuple[list[Path], list[str]]:
    """Copy the item's databases in, cut at its moment, before the boot.

    Returns the ``.sql`` files to run after the boot and the tables the cut could not handle.
    """
    state = _state_dir(item)
    sql: list[Path] = []
    uncut: list[str] = []
    if state is None:
        return sql, uncut
    moment = datetime.fromisoformat(item.raw["at"])
    for name in ("mac_events.db", "memory.db"):
        if (state / name).is_file():
            src = sqlite3.connect(f"file:{state / name}?mode=ro", uri=True)
            dst = sqlite3.connect(root / name)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
            uncut += [
                f"{name}:{t}"
                for t in cut_at(root / name, moment, str(item.raw.get("zone") or "UTC"))
            ]
    UNCUT.update(uncut)
    sql = [state / n for n in ("mac_events.sql", "memory.sql") if (state / n).is_file()]
    return sql, uncut


@contextmanager
def sandbox(
    item: Item,
    recording: Recording | None,
    *,
    live: bool = False,
    real_root: Path | None = None,
) -> Iterator[Box]:
    """Boot the runtime on a copy of his data at the item's moment, with the providers recorded."""
    from jarvis.runtime.bootstrap import app as boot_module  # noqa: PLC0415

    tmp = Path(tempfile.mkdtemp(prefix="tool-exam-"))
    root = tmp / "root"
    root.mkdir()
    runtime: JarvisRuntime | None = None
    moment = datetime.fromisoformat(item.raw["at"])
    try:
        sql, uncut = _copy_state(item, root)
        _settings(item, root, recording, real_root, live=live)
        if live and real_root is not None and (real_root / "env").is_file():
            shutil.copyfile(real_root / "env", root / "env")
        with (
            _environment(item, tmp, recording, live=live),
            time_machine.travel(moment, tick=False) as clock,
            patch.object(boot_module, "_start_locating_places", lambda *_: None),
        ):
            runtime = boot_runtime(root)
            if runtime.runtime_paths.root != root.resolve() and runtime.runtime_paths.root != root:
                msg = f"the runtime booted on {runtime.runtime_paths.root}, not on the sandbox"
                raise SystemExit(msg)
            if runtime.rides is not None:
                runtime.rides.defer = lambda _fn: None
                _seat_ride(runtime, item.raw.get("ride"), moment)
            for script in sql:
                runtime.conn.executescript(script.read_text(encoding="utf-8"))
            _place_him(runtime, item.raw.get("here"), moment)
            _seat_job_mail(runtime, _job_mail_spec(item), moment)
            yield Box(runtime, root, item, clock, uncut)
    finally:
        if runtime is not None:
            runtime.conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _seat_ride(runtime: JarvisRuntime, ride: Mapping[str, Any] | None, moment: datetime) -> None:
    """Put him on a followed bus ride as ``ride`` says, its timetable the stops given."""
    if not ride or runtime.rides is None:
        return
    now_ms = int(moment.timestamp() * 1000)
    stops = [
        StopTime(
            int(s["seq"]), str(s["stop_id"]), str(s["name"]), float(s["lat"]), float(s["lng"]),
            now_ms + int(s["seq"]) * 60_000,
        )
        for s in ride["stops"]
    ]
    runtime.rides._timetable = lambda *_args: stops  # type: ignore[method-assign]  # noqa: SLF001
    write_ride_start(
        runtime.conn,
        Ride(
            ride_id="ride-exam", route=str(ride["route"]), source="said", started_at_ms=now_ms,
            trip_id=str(ride["trip_id"]), scheduled_end_ms=stops[-1].sched_ms,
        ),
    )


def _place_him(runtime: JarvisRuntime, here: Mapping[str, Any] | None, moment: datetime) -> None:
    """The phone's last report, as ``here`` says, written to the sandbox log."""
    if not here:
        return
    payload = {"lat": here["lat"], "lng": here["lng"], "accuracy_m": here.get("accuracy_m", 20.0)}
    if here.get("place"):
        payload["place"] = here["place"]
    emit_event(
        runtime.conn,
        type="phone.location_observed",
        ingestion_node="iphone",
        payload=payload,
        ts_epoch_ms=int(moment.timestamp() * 1000) - int(here.get("age_s", 0)) * 1000,
    )


JOB_FIXTURE = "job_mail.json"
"""The file of a state directory that holds a job hunt, in the shape of an item's ``job_mail``."""


def _job_mail_spec(item: Item) -> dict[str, Any]:
    """The job hunt an item seats: its state directory's ``job_mail.json``, then its own."""
    spec: dict[str, Any] = {"mails": [], "applications": []}
    state = _state_dir(item)
    parts = [item.raw.get("job_mail") or {}]
    if state is not None and (state / JOB_FIXTURE).is_file():
        parts.insert(0, json.loads((state / JOB_FIXTURE).read_text(encoding="utf-8")))
    for part in parts:
        spec["mails"] += part.get("mails", [])
        spec["applications"] += part.get("applications", [])
        spec.update({k: part[k] for k in ("read_at", "failing_since") if k in part})
    return spec


def _seat_job_mail(runtime: JarvisRuntime, spec: Mapping[str, Any], moment: datetime) -> None:
    """Write the job hunt through the ledger's own functions, and set when mail was last read."""
    asked = bool(spec["mails"] or spec["applications"] or "read_at" in spec)
    job = runtime.job_mail
    if job is None:
        if asked:
            msg = "the item seats job mail, and the recording's settings leave job_mail off"
            raise LookupError(msg)
        return
    path = runtime.memory.db_path  # type: ignore[union-attr]
    for mail in spec["mails"]:
        job_ledger.upsert_mail(
            path,
            {"sender_name": "Recruiting", "confidence": 0.9, "extracted_by": "exam", **mail},
            moment,
        )
    for application in spec["applications"]:
        fields = {k: v for k, v in application.items() if k != "hidden"}
        fields.setdefault("source", "said")
        app_id = job_ledger.add_application(path, moment, **fields)
        if application.get("hidden"):
            job_ledger.edit_application(path, moment, app_id, {"hidden": True})
    # What a poll leaves behind, set directly: the sandbox has no Gmail to poll.
    for name in ("read_at", "failing_since"):
        if spec.get(name):
            setattr(job, f"_{name}", datetime.fromisoformat(spec[name]))


def dispatch(box: Box, tool: str, args: Mapping[str, Any], said: str) -> str:
    """One call through the real dispatcher as the model would make it; the result it reads."""
    runtime = box.runtime
    definitions = {t.name: t for t in runtime.tool_registry.get_definitions()}
    if tool not in definitions:
        msg = f"the booted runtime has no tool {tool!r}"
        raise LookupError(msg)
    turn_id = f"T{uuid.uuid4().hex[:12]}"
    emit_event(
        runtime.conn,
        type="surface.user_intent",
        payload={"transcript": said, "turn_id": turn_id, "channel": "cli_stdin"},
        correlation={"turn_id": turn_id},
    )
    action_id = uuid.uuid4().hex
    request = ActionRequest(
        action_id=action_id,
        tool_name=tool,
        target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level=call_risk(definitions[tool], args),
        arguments=args,
        authorization_lease=None,
        run_id=None,
        turn_id=turn_id,
    )
    lifecycle = ActionLifecycle()
    lifecycle.register(action_id)
    lifecycle.transition(action_id, "authorized")
    try:
        slot = runtime.tool_registry.dispatch(
            request,
            runtime.conn,
            runtime.runtime_paths,
            lifecycle,
        )
    finally:
        release_turn_actions(turn_id)
    return slot.tool_output or ""


# --- checking one item ---------------------------------------------------------------------------


_SEGMENT = re.compile(r"([^.\[\]]*)((?:\[[^\]]*\])*)")
_BRACKET = re.compile(r"\[([^\]]*)\]")


def _key(c: Any, name: str) -> list[Any]:  # noqa: ANN401 - decoded JSON
    """What a key (or, on a list, an index) reaches in one value."""
    if isinstance(c, dict) and name in c:
        return [c[name]]
    if isinstance(c, list) and name.lstrip("-").isdigit() and -len(c) <= int(name) < len(c):
        return [c[int(name)]]
    return []


def _bracket(rows: list[Any], inner: str) -> list[Any]:
    """One ``[...]`` selector over a list: an index, ``*``, or a ``?field~text`` filter."""
    if inner == "*":
        return list(rows)
    if inner.lstrip("-").isdigit():
        return [rows[int(inner)]] if -len(rows) <= int(inner) < len(rows) else []
    if inner.startswith("?") and ("~" in inner or "=" in inner):
        field, sign, text = re.split(r"([~=])", inner[1:], maxsplit=1)
        got = [str(r.get(field, "")) if isinstance(r, dict) else "" for r in rows]
        same = (lambda g: text.casefold() in g.casefold()) if sign == "~" else (lambda g: g == text)
        return [r for r, g in zip(rows, got, strict=True) if same(g)]
    return []


def _step(cands: list[Any], name: str, brackets: str) -> list[Any]:
    """One path segment over every candidate: a key or index, then each ``[...]``."""
    if name:
        cands = [x for c in cands for x in _key(c, name)]
    for inner in _BRACKET.findall(brackets):
        cands = [x for c in cands if isinstance(c, list) for x in _bracket(c, inner)]
    return cands


def select(value: Any, path: str) -> list[Any]:  # noqa: ANN401 - decoded JSON
    """Every value ``path`` reaches in a decoded result (none when it reaches nothing).

    ``$`` is the whole result; segments are dotted keys; after a key come list selectors:
    ``[2]`` an index, ``[*]`` every element, ``[?what~text]`` the elements whose ``what``
    contains ``text`` (any case), ``[?what=text]`` those whose ``what`` is exactly ``text``.
    """
    cands = [value]
    rest = path[1:].lstrip(".") if path.startswith("$") else path
    for segment in rest.split(".") if rest else ():
        match = _SEGMENT.fullmatch(segment)
        if match is None:
            return []
        cands = _step(cands, match[1], match[2])
    return cands


def _holds(got: Any, op: str, want: Any) -> bool:  # noqa: ANN401, PLR0911, C901 - one op, one value
    text = json.dumps(got, ensure_ascii=False)
    match op:
        case "eq":
            return bool(got == want)
        case "ne":
            return bool(got != want)
        case "contains":
            return want in got if isinstance(got, str | list | dict) else False
        case "mentions":  # the value's JSON text holds each of these words, in any case
            return all(
                w.casefold() in text.casefold() for w in ([want] if isinstance(want, str) else want)
            )
        case "matches":
            return isinstance(got, str) and re.search(want, got) is not None
        case "in":
            return bool(got in want)
        case "gt":
            return bool(got > want)
        case "gte":
            return bool(got >= want)
        case "lt":
            return bool(got < want)
        case "lte":
            return bool(got <= want)
        case "len":
            return bool(len(got) == want)
    msg = f"unknown result op {op!r}"
    raise ValueError(msg)


_NEGATIVE = {
    "ne": "eq",
    "not_contains": "contains",
    "not_mentions": "mentions",
    "not_matches": "matches",
}


def check_result(out: Any, check: Mapping[str, Any]) -> str | None:  # noqa: ANN401 - decoded JSON
    """Why a result fails one ``expect.result`` check, or None.

    Ops: eq, contains, mentions, matches, in, gt, gte, lt, lte, len hold when any value the path
    reaches holds; ne, not_contains, not_mentions, not_matches hold when none does; exists and
    absent say whether the path reaches anything.
    """
    path, op, want = check["path"], check["op"], check.get("value")
    got = select(out, path)
    if op == "exists":
        ok = bool(got)
    elif op == "absent":
        ok = not got
    elif op in _NEGATIVE:
        ok = not any(_holds(g, _NEGATIVE[op], want) for g in got)
    else:
        try:
            ok = any(_holds(g, op, want) for g in got)
        except ValueError as exc:
            return str(exc)
    if ok:
        return None
    shown = json.dumps(got[:2], ensure_ascii=False)[:160] if got else "nothing"
    return f"result {path} {op} {want!r}: reached {shown}"


def _rows(box: Box, db: str, sql: str) -> list[list[Any]]:
    path = box.root / "mac_events.db" if db == "events" else box.runtime.memory.db_path  # type: ignore[union-attr]
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [list(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def state_failures(box: Box, checks: Sequence[Mapping[str, Any]]) -> list[str]:
    """What differs between the sandbox databases and the ``state_after`` queries' answers."""
    failures = []
    for check in checks:
        got_rows = _rows(box, check.get("db", "events"), check["sql"])
        want = check["answer"]
        cell = len(got_rows) == 1 and len(got_rows[0]) == 1 and not isinstance(want, list)
        got: Any = got_rows[0][0] if cell else got_rows
        if got != want:
            failures.append(f"state {check['sql'][:60]!r}: expected {want!r}, got {got!r}")
    return failures


def _events_since(box: Box, after: int) -> list[tuple[str, str]]:
    rows = box.runtime.conn.execute(
        "SELECT type, payload_json FROM events WHERE id > ? ORDER BY id",
        (after,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _block_d_failures(box: Box, expect: Mapping[str, Any], out: Mapping[str, Any]) -> list[str]:
    """Whether the result's submitted count is the one block D states over the same data."""
    if not expect.get("block_d_submitted"):
        return []
    zone = ZoneInfo(str(box.item.raw.get("zone") or "UTC"))
    sources = LedgerSources(
        box.runtime.memory.db_path,  # type: ignore[union-attr]
        box.runtime.runtime_paths.event_log, None, zone,
    )
    block = job_hunt_text(sources, datetime.fromisoformat(box.item.raw["at"]).astimezone(zone))
    stated = re.search(r"Applications submitted: at least (\d+)", block)
    said = re.search(r"At least (\d+) submitted", str(out.get("summary", "")))
    if stated is None or said is None:
        return [f"block D states {stated and stated[0]!r}, the summary {said and said[0]!r}"]
    if stated[1] != said[1]:
        return [f"block D says at least {stated[1]} submitted, the result says {said[1]}"]
    return []


def _receipt_failures(
    want: Mapping[str, Any] | None, events: Sequence[tuple[str, str]],
) -> list[str]:
    """Where the call's receipt (ADR 0319) differs from the fields ``expect.receipt`` holds."""
    if want is None:
        return []
    rows = [json.loads(p) for t, p in events if t == "action.result_observed"]
    if len(rows) != 1 or not isinstance(rows[0].get("receipt"), dict):
        return [f"expected one result row with a receipt, got {len(rows)} row(s)"]
    got = rows[0]["receipt"]
    return [
        f"receipt {key}: expected {value!r}, got {got.get(key)!r}"
        for key, value in want.items()
        if got.get(key) != value
    ]


def _check_events(spec: Mapping[str, Any], events: Sequence[tuple[str, str]]) -> list[str]:
    def seen(entry: str | Mapping[str, Any]) -> bool:
        kind, pattern = (
            (entry, None)
            if isinstance(entry, str)
            else (entry["type"], entry.get("payload_matches"))
        )
        return any(t == kind and (pattern is None or re.search(pattern, p)) for t, p in events)

    return [f"event expected but not written: {e}" for e in spec.get("must", ()) if not seen(e)] + [
        f"event written that must not be: {e}" for e in spec.get("must_not", ()) if seen(e)
    ]


def run_item(item: Item, *, recording: Recording | None = None) -> list[str]:
    """Run one tool-layer item in a sandbox; the list of what failed (empty = passed)."""
    if item.raw.get("layer", "tool") != "tool":
        msg = f"{item.id}: not a tool-layer item"
        raise ValueError(msg)
    rec_path = item.raw.get("recording")
    if recording is None and rec_path:
        recording = Recording.load(item.path.parent / rec_path)
    transport = _Transport(recording or Recording(), live=False)
    expect = item.raw.get("expect") or {}
    failures: list[str] = []

    def alarm(*_: object) -> None:
        msg = f"timed out after {ITEM_TIMEOUT_S}s (a wait on the frozen clock?)"
        raise TimeoutError(msg)

    previous = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(ITEM_TIMEOUT_S)
    try:
        with transport, sandbox(item, recording) as box:
            for step in item.raw.get("setup") or ():
                dispatch(box, step["tool"], step.get("args") or {}, item.raw.get("said", ""))
            after = int(
                box.runtime.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
            )
            raw = dispatch(
                box, item.raw["tool"], item.raw.get("args") or {}, item.raw.get("said", "")
            )
            failures += _judge(box, item, expect, raw, after, recording)
    except (LookupError, TimeoutError, RecordingMiss) as exc:
        failures.append(str(exc))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    failures += [f"provider call missing from the recording: {m}" for m in transport.misses]
    return failures


def _judge(  # noqa: PLR0913 - one result against everything the item expects
    box: Box,
    item: Item,
    expect: Mapping[str, Any],
    raw: str,
    after: int,
    recording: Recording | None,
) -> list[str]:
    failures = [f"rule: {p}" for p in result_problems(raw)]
    try:
        out = json.loads(raw)
    except ValueError:
        return [*failures, "the result is not JSON"]
    if not isinstance(out, dict):
        return [*failures, "the result is not an object"]
    errored = "error" in out
    kind = expect.get("error_kind")
    if errored and not kind:
        failures.append(f"the call failed: {out.get('code')}: {str(out['error'])[:120]}")
    elif kind and not errored:
        failures.append(f"expected an error of kind {kind}, got a result")
    elif kind and out.get("kind") != kind:
        failures.append(f"expected error kind {kind}, got {out.get('kind')!r}")
    said = str(out.get("error") if errored else out.get("summary", ""))
    failures += [
        f"summary lacks /{p}/: {said[:120]!r}"
        for p in expect.get("summary_must", ())
        if not re.search(p, said)
    ]
    failures += [
        f"summary has /{p}/: {said[:120]!r}"
        for p in expect.get("summary_must_not", ())
        if re.search(p, said)
    ]
    failures += [why for check in expect.get("result", ()) if (why := check_result(out, check))]
    shown = json.dumps(out, ensure_ascii=False).lower()
    failures += [
        f"the result mentions {text!r}"
        for text in expect.get("result_must_not_mention", ())
        if text.lower() in shown
    ]
    failures += _receipt_failures(expect.get("receipt"), _events_since(box, after))
    failures += state_failures(box, expect.get("state_after", ()))
    failures += _check_events(expect.get("events_after") or {}, _events_since(box, after))
    failures += _block_d_failures(box, expect, out)
    if name := expect.get("checker"):
        from scripts.tool_exam_checkers import CHECKERS  # noqa: PLC0415

        failures += [f"checker {name}: {m}" for m in CHECKERS[name](item.raw, recording, out)]
    return failures


# --- the runner ----------------------------------------------------------------------------------


def run_one(item: Item) -> list[str]:
    """Run an item of either layer."""
    if item.raw.get("layer", "tool") == "plumbing":
        from scripts.tool_exam_plumbing import run_item as run_plumbing  # noqa: PLC0415

        return run_plumbing(item)
    return run_item(item)


def _safely(run: Callable[[Item], list[str]], item: Item) -> list[str]:
    """``run(item)``, or the item's failure when the run itself broke (a bad field, a boot)."""
    try:
        return run(item)
    except Exception as exc:  # noqa: BLE001 - one broken item must not stop the exam
        return [f"the run crashed: {type(exc).__name__}: {exc}"[:300]]


def score(
    directory: Path,
    only: Sequence[str] = (),
    *,
    run: Callable[[Item], list[str]] = run_one,
) -> dict[str, Any]:
    """Run the items in ``directory`` against its baseline.

    ``failed`` maps each failing id to why; ``regressed`` are the failures not in the baseline;
    ``fixed`` are baselined ids that now pass (remove them from the baseline).
    """
    items = [i for i in load_items(directory) if not only or i.id in only]
    base_file = directory / BASELINE
    baseline: list[str] = (
        json.loads(base_file.read_text(encoding="utf-8")) if base_file.is_file() else []
    )
    failed = {i.id: _safely(run, i) for i in items}
    failed = {k: v for k, v in failed.items() if v}
    ids = {i.id for i in items}
    return {
        "items": len(items),
        "failed": failed,
        "regressed": sorted(set(failed) - set(baseline)),
        "fixed": sorted(i for i in baseline if i in ids and i not in failed),
        "stale": sorted(i for i in baseline if not only and i not in ids),
    }


def verdict(result: Mapping[str, Any], *, detail: bool = True) -> list[str]:
    """What fails the gate: a failure outside the baseline, a baselined item that passes.

    Without ``detail`` (the held set) a failure is its id alone: why it failed would show the item.
    """
    return (
        [
            f"FAIL {i}" + (f": {'; '.join(result['failed'][i])[:300]}" if detail else "")
            for i in result["regressed"]
        ]
        + [f"{i}: passes now; remove it from the baseline" for i in result["fixed"]]
        + [f"{i}: no such item; remove it from the baseline" for i in result["stale"]]
    )


def cmd_record(args: argparse.Namespace) -> int:
    """``record``: run one item live and store what each provider answered beside it.

    The providers' keys are the ones saved for ``--root`` (this machine's saved keys for it, then
    its ``env``), loaded before the recorder starts so that it scrubs and checks every one.
    """
    item = Item(json.loads(args.item.read_text(encoding="utf-8")), args.item.resolve())
    load_env_file(args.root.expanduser())
    recording = Recording({"settings": _snapshot_settings(args.root)})
    transport = _Transport(recording, live=True)
    with transport, sandbox(item, recording, live=True, real_root=args.root.expanduser()) as box:
        for step in item.raw.get("setup") or ():
            dispatch(box, step["tool"], step.get("args") or {}, item.raw.get("said", ""))
        raw = dispatch(box, item.raw["tool"], item.raw.get("args") or {}, item.raw.get("said", ""))
    target = args.item.with_name(f"{args.item.stem}.recording.json")
    recording.save(target, transport.secrets)
    sys.stdout.write(
        f"recorded {len(recording.data['http'])} http, {len(recording.data['mcp'])} mcp"
        f" -> {target.name}"
        f'; set "recording": "{target.name}" in the item. Result {len(raw)} characters.\n',
    )
    return 0


_SNAPSHOT_KEYS = (("home", "weather"), ("transit",))


def _snapshot_settings(root: Path) -> dict[str, Any]:
    """The settings a provider URL depends on (his home, his stops), kept so a replay asks alike."""
    path = root.expanduser() / "settings.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, Any] = {}
    for keys in _SNAPSHOT_KEYS:
        cursor: Any = data
        for k in keys:
            cursor = cursor.get(k) if isinstance(cursor, dict) else None
        if cursor is not None:
            target = out
            for k in keys[:-1]:
                target = target.setdefault(k, {})
            target[keys[-1]] = cursor
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``run``, ``score`` and ``record``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "score"):
        one = sub.add_parser(name)
        one.add_argument("--items", type=Path, default=ITEMS / "work")
        one.add_argument("--only", default="")
    rec = sub.add_parser("record")
    rec.add_argument("item", type=Path)
    rec.add_argument("--root", type=Path, default=Path("~/.jarvis"))
    args = parser.parse_args(argv)
    if args.cmd == "record":
        return cmd_record(args)
    only = [s for s in args.only.split(",") if s]
    result = score(args.items.expanduser(), only)
    held = args.items.name == "held"  # read only by its counts and ids
    if args.cmd == "run" and not held:
        for ident, why in result["failed"].items():
            sys.stdout.write(f"FAIL {ident}\n" + "".join(f"  - {m}\n" for m in why))
    sys.stdout.write(
        f"items: {result['items']}   failing: {len(result['failed'])}"
        f"   regressed: {len(result['regressed'])}   fixed: {len(result['fixed'])}\n",
    )
    if UNCUT and not held:
        sys.stdout.write(f"cut left these tables as they were: {', '.join(sorted(UNCUT))}\n")
    problems = verdict(result, detail=not held)
    for line in problems:
        sys.stdout.write(line + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
