"""Test-time record of every model request and what came back (ADR 0118).

``diagnostics.log_llm_io`` turns it on; off, :func:`start` returns ``None`` and
nothing else here runs. One JSON line per request goes to ``llm-io.jsonl``,
written by a thread of its own: the request path only collects text and tool
calls as they arrive and hands the finished record over, so a stream's first
word never waits on disk or on JSON. The file holds what Allen said and what
she answered; it is cut by nothing and is for the owner to delete.

Layer rules: stdlib only.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import queue
import re
import threading
import time
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

_PENDING_MAX: Final = 256
_LABELS: contextvars.ContextVar[Mapping[str, str | None]] = contextvars.ContextVar(
    "jarvis_llm_io_labels", default=MappingProxyType({}),
)
# The request carries no key (the SDK adds it), but a key pasted into a
# message or an extra_body would; the line is scrubbed whatever its source.
_SECRETS = re.compile(r"sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)

_path: Path | None = None
_pending: queue.Queue[tuple[Path, dict[str, Any]]] = queue.Queue(maxsize=_PENDING_MAX)
_writer_lock = threading.Lock()
_writer: threading.Thread | None = None
_warned = False


def configure(path: Path | None) -> None:
    """Log to ``path`` from now on, or stop with ``None``; the folder is made if absent."""
    global _path  # noqa: PLW0603 - one process-wide sink, like the realtime trace
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    _path = path


@contextlib.contextmanager
def labels(**values: str | None) -> Iterator[None]:
    """Name the requests made inside (``kind``, ``turn_id``, ``response_id``, ``run_id``)."""
    token = _LABELS.set(MappingProxyType({**_LABELS.get(), **values}))
    try:
        yield
    finally:
        _LABELS.reset(token)


class Record:
    """One request in flight: text and tool calls gather here, :meth:`end` queues the line."""

    def __init__(self, path: Path, head: dict[str, Any]) -> None:
        """Remember where the line goes and what was asked."""
        self._path = path
        self._head = head
        self._started = time.monotonic()
        self._parts: list[list[Any]] = []
        self._tools: list[dict[str, str]] = []
        self._done = False

    def tap(self, event: Any) -> None:  # noqa: ANN401 - a typed stream event, duck-typed so L1 stays free of L3
        """Take one stream event: a text delta or an assembled tool call."""
        if event.kind == "text_delta":
            phase = event.phase
            if self._parts and self._parts[-1][0] == phase:
                self._parts[-1][1] += event.text
            else:
                self._parts.append([phase, event.text])
        elif event.kind == "tool_completed":
            self._tools.append(
                {"call_id": event.call_id, "name": event.name, "arguments": event.arguments_json},
            )

    def end(
        self, *, text: str | None = None, tool_calls: list[dict[str, str]] | None = None,
        **tail: Any,  # noqa: ANN401 - finish_reason, usage, outcome, error: whatever the path knows
    ) -> None:
        """Queue the finished line, once; never raises and never waits."""
        if self._done:
            return
        self._done = True
        output = {
            "text": text if text is not None else "".join(part[1] for part in self._parts),
            "tool_calls": tool_calls if tool_calls is not None else self._tools,
            **tail,
        }
        if any(part[0] is not None for part in self._parts):
            output["parts"] = self._parts
        line = {
            **self._head, "ms": round((time.monotonic() - self._started) * 1000),
            "output": output,
        }
        try:
            _pending.put_nowait((self._path, line))
        except queue.Full:
            _warn_once("queue full; a line was dropped")
            return
        _start_writer()


def enabled() -> bool:
    """Whether :func:`start` would open a record; lets a caller skip building the request."""
    return _path is not None


def start(request: Mapping[str, Any], **head: Any) -> Record | None:  # noqa: ANN401 - head is free-form
    """Open a record for one request; ``None`` when the log is off."""
    path = _path
    if path is None:
        return None
    return Record(path, {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        **{key: value for key, value in _LABELS.get().items() if value is not None},
        **head, "request": request,
    })


def flush() -> None:
    """Wait for every queued line to reach the file."""
    _pending.join()


def _warn_once(why: str) -> None:
    global _warned  # noqa: PLW0603 - one warning, not one per request
    if not _warned:
        _warned = True
        LOGGER.warning("llm-io log: %s", why)


def _start_writer() -> None:
    global _writer  # noqa: PLW0603 - started on the first line
    with _writer_lock:
        if _writer is None:
            _writer = threading.Thread(target=_drain, name="jarvis-llm-io-log", daemon=True)
            _writer.start()


def _drain() -> None:
    while True:
        path, line = _pending.get()
        try:
            text = json.dumps(line, ensure_ascii=False, default=str)
            with path.open("a", encoding="utf-8") as sink:
                sink.write(_SECRETS.sub("[redacted]", text) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            _warn_once(f"cannot write {path}: {exc}")
        finally:
            _pending.task_done()


__all__ = ["Record", "configure", "enabled", "flush", "labels", "start"]
