"""Invented mail for the tool exam (ADR 0317, 0362): a recording's ``mailbox`` as a Gmail server.

A recording (``*.recording.json``) may hold a ``mailbox`` instead of one MCP answer per call: the
messages the invented account holds, which the fake Workspace server of ``tests.support`` serves
whatever the tool asks of it (a search, a header read, a raw message, a label change). The
exam's recordings answer only the exact call they hold, and a mail tool's calls are many and
change with it; the mailbox is answered by one server that knows Gmail's query language.

``mailbox`` is ``{"messages": [...]}``; a message is an object with:

- ``id``, ``subject``; ``sender`` and ``to`` (``Name <name@example.com>``, every address on
  example.com), ``cc``, ``thread`` (default: its own id);
- ``ago``: minutes before the item's moment it arrived, or ``at``: an ISO time;
- ``labels``: Gmail's (default: unread in the inbox, Primary);
- ``plain``, ``html``: the parts; ``plain`` may be a list of paragraphs, each a text or
  ``{"repeat": n, "text": ...}``; ``headers``: more headers (``List-Unsubscribe``…);
- ``attachments``: each ``{"name", "type", "text"}`` or ``{"name", "type": "application/pdf",
  "pdf": [[line, ...], ...]}`` (a page of no lines is a scan);
- ``size``: the size Gmail says it is, when the test needs it big;
- ``repeat``: that many copies, ``{i}`` in a text standing for the copy's number from 1, each
  ``every`` minutes older than the one before.

Every message is invented; none is ever read from a real mailbox.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from jarvis.runtime.plugin_connections import _Active
from jarvis.runtime.plugins import Plugins
from tests.support.gmail_fake import PRIMARY, Gmail
from tests.support.mail_raw import SAM, pdf

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from jarvis.execution.mcp_tools import McpServers
    from jarvis.runtime import JarvisRuntime

ME = "Allen <allen@example.com>"


class _Server:
    """The connected ``gmail`` server as the home reaches it: the fake, answering as JSON text."""

    connected_servers: frozenset[str] = frozenset({"gmail"})

    def __init__(self, gmail: Gmail) -> None:
        self._gmail = gmail

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """One call of the server."""
        return self._gmail.call(server, tool, args)

    def stop(self) -> None:
        """Nothing to stop."""


def _fill(value: Any, i: int) -> Any:  # noqa: ANN401 - JSON
    """``value`` with ``{i}`` in every text of it replaced by ``i``."""
    if isinstance(value, str):
        return value.replace("{i}", str(i))
    if isinstance(value, list):
        return [_fill(one, i) for one in value]
    if isinstance(value, dict):
        return {key: _fill(one, i) for key, one in value.items()}
    return value


def _copies(entry: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """The messages one entry stands for: itself, or ``repeat`` numbered copies of it."""
    for i in range(1, int(entry.get("repeat", 1)) + 1):
        made = {k: _fill(v, i) for k, v in entry.items() if k not in ("repeat", "every")}
        if "ago" in made:
            made["ago"] = float(made["ago"]) + (i - 1) * float(entry.get("every", 0))
        yield made


def _text(paragraphs: str | Sequence[str | Mapping[str, Any]] | None) -> str | None:
    """A message's plain part: a text, or its paragraphs (a repeated one numbered) apart."""
    if paragraphs is None or isinstance(paragraphs, str):
        return paragraphs
    out: list[str] = []
    for one in paragraphs:
        if isinstance(one, str):
            out.append(one)
        else:
            out += [_fill(one["text"], i) for i in range(1, int(one["repeat"]) + 1)]
    return "\n\n".join(out)


def _file(spec: Mapping[str, Any]) -> tuple[str, str, bytes]:
    """One attachment as ``(name, type, bytes)``."""
    if "pdf" in spec:
        return str(spec["name"]), str(spec["type"]), pdf(*spec["pdf"])
    return str(spec["name"]), str(spec["type"]), str(spec["text"]).encode()


def expanded(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every message ``spec`` describes, each ``repeat`` written out as its numbered copies."""
    return [one for entry in spec["messages"] for one in _copies(entry)]


def arrived(one: Mapping[str, Any], now: datetime) -> datetime:
    """When a message of :func:`expanded` arrived, as of ``now``."""
    if "at" in one:
        return datetime.fromisoformat(one["at"])
    return now - timedelta(minutes=float(one.get("ago", 10)))


def build(spec: Mapping[str, Any], now: datetime) -> Gmail:
    """The fake server holding the messages ``spec`` describes, as of ``now``."""
    gmail = Gmail(now=now)
    for one in expanded(spec):
        gmail.add(
            one["id"], one["subject"], sender=one.get("sender", SAM), to=one.get("to", ME),
            cc=one.get("cc", ""), at=arrived(one, now), thread=one.get("thread"),
            labels=one.get("labels", PRIMARY), plain=_text(one.get("plain")),
            html=one.get("html"), headers=one.get("headers"),
            attachments=[_file(f) for f in one.get("attachments", ())],
            size=one.get("size"),
        )
    return gmail


def connect(runtime: JarvisRuntime, gmail: Gmail) -> None:
    """Make ``gmail`` the runtime's connected ``gmail`` server, as a logged-in plugin is."""
    connections = runtime.plugin_connections
    if connections is None:
        msg = "the booted runtime has no plugin connections to hold the mailbox"
        raise LookupError(msg)
    client = cast("McpServers", _Server(gmail))
    connections._active["exam-mailbox"] = _Active(client, (), Plugins(sources={}))  # noqa: SLF001
