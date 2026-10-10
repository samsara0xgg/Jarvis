"""Pushes to Allen's paired iPhone (ADR 0210): what is sent, when, and to which registered device.

``jarvis.surface.apns`` talks to Apple and ``jarvis.state.push_tokens`` keeps the tokens; this
module decides. Four things push, each by its own rule:

- a reminder that fired (:meth:`Push.reminder`, called by the reminder clock after it wrote
  ``reminder.fired``): at every quiet level, with a sound, time-sensitive;
- something that waits for him (:meth:`Push.waiting`): a confirmation or an ask card the watcher
  finds in the event log, or a Claude Code permission request the hooks hold. Once per item, not
  to a phone whose conversation socket is open, and as the quiet level allows: sound at ``off``,
  a silent banner at ``quiet``, nothing from ``no-pop`` up;
- whatever another feature wants him to see (:meth:`Push.send`);
- the running Live Activity's content (:meth:`Push.poll`), when her state or the next reminder
  changed.

A push that fails is logged without its text and never tried again. A host with no key
configured pushes nothing and says so once at boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared import lang
from jarvis.state import push_tokens
from jarvis.state.event_log import iter_events_after, open_runtime_event_log
from jarvis.surface.apns import (
    ApnsClient,
    Outcome,
    ProviderToken,
    PushMessage,
    alert_payload,
    load_signing_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

LOGGER = logging.getLogger(__name__)

# The env var (filled from the Keychain on macOS, or the ``env`` file, by ``load_env_file``) that
# may hold the ``.p8`` instead of a file; the file's newlines may be written as ``\n``.
KEY_ENV: Final = "APNS_AUTH_KEY"
POLL_S: Final = 5.0
# One Live Activity line is a glance, not a paragraph.
_ACTIVITY_TEXT: Final = 80
# Items remembered as pushed or skipped, so one is never pushed twice while it is on the card.
_REMEMBERED: Final = 500
_QUIET_SILENT: Final = "quiet"
_QUIET_HELD: Final = frozenset({"no-pop", "dnd"})
_WAITING_TITLES: Final = {
    "confirmation": "push.confirmation",
    "question": "push.question",
    "claude": "push.claude",
}
_CLOSERS: Final = {
    "confirmation.accepted": "confirmation_id",
    "confirmation.rejected": "confirmation_id",
    "confirmation.expired": "confirmation_id",
    "clarification.withdrawn": "clarification_id",
    "surface.clarified": "clarification_id",
    "surface.dismissed": "clarification_id",
}


def _pem(block: Mapping[str, Any], root: Path) -> tuple[bytes | None, str]:
    """The ``.p8`` bytes and, when there are none, why (no key configured at all is ``""``)."""
    named = str(block.get("key_file") or "")
    if not named:
        value = os.environ.get(KEY_ENV, "")
        return (value.replace("\\n", "\n").encode() or None), ""
    path = (root / named).resolve()
    try:
        info = path.stat()
        inside = path.is_relative_to(root.resolve())
    except OSError:
        return None, "push.key_file cannot be read"
    if not inside or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        return None, "push.key_file must be a file under the runtime root, mode 0600"
    try:
        return path.read_bytes(), ""
    except OSError:
        return None, "push.key_file cannot be read"


class Push:
    """The sender; built once at boot, safe to call from any thread."""

    def __init__(self, root: Path, event_log: Path, client: ApnsClient | None = None) -> None:
        """``client`` is ``None`` while no key is configured: every push then does nothing."""
        self._root = root
        self._event_log = event_log
        self._client = client
        # Wired by the daemon.
        self.quiet: Callable[[], str] = lambda: "off"
        # Whether this device has a ``/phone/ws`` conversation socket open now (ADR 0209).
        self.phone_socket_open: Callable[[str], bool] = lambda _device: False
        self.her_state: Callable[[], str] = lambda: "idle"
        self.clock: Callable[[], float] = time.time
        # ``(text, due_at_ms)`` of the reminder that rings next, or ``None``.
        self.next_reminder: Callable[[], tuple[str, int] | None] = lambda: None
        self._lock = threading.Lock()
        self._remembered: dict[str, None] = {}
        self._watch_from: int | None = None
        self._activity: dict[str, tuple[str, str]] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="push")

    @classmethod
    def from_config(cls, config: Mapping[str, Any], root: Path, event_log: Path) -> Push:
        """The sender for the ``push:`` block; with no usable key, a sender that pushes nothing.

        Logs exactly one line saying which. Never raises: a broken key switches pushes off, it
        does not stop the boot.
        """
        block = config.get("push")
        block = block if isinstance(block, dict) else {}
        key_id, team_id, bundle_id = (
            str(block.get(name) or "") for name in ("key_id", "team_id", "bundle_id")
        )
        pem, why = _pem(block, root)
        if pem is None:
            LOGGER.log(
                logging.WARNING if why else logging.INFO,
                "push: off (%s)", why or "no APNs key configured",
            )
            return cls(root, event_log)
        if not (key_id and team_id and bundle_id):
            LOGGER.warning("push: off (push.key_id, push.team_id and push.bundle_id are needed)")
            return cls(root, event_log)
        try:
            token = ProviderToken(load_signing_key(pem), key_id, team_id)
        except ValueError as exc:
            LOGGER.warning("push: off (the APNs key is unusable: %s)", exc)
            return cls(root, event_log)
        LOGGER.info("push: on for %s", bundle_id)
        return cls(root, event_log, ApnsClient(token, bundle_id))

    @property
    def enabled(self) -> bool:
        """Whether a key is configured."""
        return self._client is not None

    # --- what callers use -------------------------------------------------------------

    def send(
        self,
        title: str,
        body: str,
        url: str | None = None,
        category: str | None = None,
        thread: str | None = None,
    ) -> int:
        """Push a notification to every registered phone; returns how many took it.

        ``url`` is what tapping it opens (an Uber deep link, say). Blocks for the network, so a
        coroutine calls it through ``asyncio.to_thread``. Any quiet level is the caller's to
        honour. Pushes nothing, and returns 0, until a key is configured.

        Raises:
            ValueError: ``url`` is not a link with a scheme, or ``category`` or ``thread`` is
                empty or over 64 characters.
        """
        return self._deliver(PushMessage(title, body, url, category, thread))

    def reminder(self, line: str) -> int:
        """Push a fired reminder's line, whatever the quiet level (ADR 0179)."""
        return self._deliver(
            PushMessage("Jarvis", line, category="reminder", thread="reminders",
                        time_sensitive=True),
        )

    def waiting(self, kind: str, item_id: str, text: str) -> bool:
        """Push that ``kind`` (``confirmation``, ``question`` or ``claude``) waits; once per item.

        Nothing goes to a device whose conversation socket is open, or to any from ``no-pop`` up;
        the item counts as seen all the same. Returns whether a phone took it.
        """
        key = f"{kind}:{item_id}"
        with self._lock:
            if key in self._remembered:
                return False
            self._remembered[key] = None
            while len(self._remembered) > _REMEMBERED:
                del self._remembered[next(iter(self._remembered))]
        level = self.quiet()
        if level in _QUIET_HELD:
            return False
        message = PushMessage(
            lang.t(_WAITING_TITLES[kind]), text, category=kind, thread="waiting",
            sound=level != _QUIET_SILENT,
        )
        return self._deliver(message, skip=self.phone_socket_open) > 0

    def claude_request(self, tool: str, cwd: str, request_id: str) -> None:
        """A permission prompt is held for him (called on the loop); the push goes off-thread."""
        text = f"{tool} · {Path(cwd).name}" if cwd else tool
        self._pool.submit(self._safely, self.waiting, "claude", request_id, text)

    # --- the watcher ------------------------------------------------------------------

    async def run(self) -> None:
        """Poll every :data:`POLL_S`; a failed poll is logged and the loop goes on."""
        LOGGER.info("push watcher started (every %.0f s)", POLL_S)
        try:
            while True:
                await asyncio.to_thread(self._safely, self.poll)
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            self._pool.shutdown(wait=False)
            raise

    def poll(self) -> None:
        """Push the cards asked since the last poll, then update the Live Activity."""
        if self._client is None:
            return
        for kind, item_id, text in self._new_waiting():
            self.waiting(kind, item_id, text)
        self._update_activity()

    def _new_waiting(self) -> list[tuple[str, str, str]]:
        """The cards asked since the last poll and not closed or expired by now.

        The first poll after boot only marks the log's end: what was waiting before it is not news.
        """
        with contextlib.closing(open_runtime_event_log(self._event_log)) as conn:
            top = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
            start, self._watch_from = self._watch_from, top
            if start is None:
                return []
            asked: list[tuple[str, str, str, int | None]] = []
            closed: set[str] = set()
            for _, event in iter_events_after(conn, start, top):
                payload = event.payload
                if event.type == "confirmation.requested":
                    asked.append((
                        "confirmation", str(payload["confirmation_id"]),
                        str(payload["template_line"]), int(payload["expires_at_ms"]),
                    ))
                elif event.type == "clarification.requested":
                    asked.append((
                        "question", str(payload["clarification_id"]),
                        str(payload["question"]), None,
                    ))
                elif (field := _CLOSERS.get(event.type)) and field in payload:
                    closed.add(str(payload[field]))
        now_ms = int(self.clock() * 1000)
        return [
            (kind, item_id, text) for kind, item_id, text, expires in asked
            if item_id not in closed and (expires is None or expires > now_ms)
        ]

    def _update_activity(self) -> None:
        """Send the Live Activity content to each running activity whose last send differs."""
        running = {
            name: one.live_activity_token
            for name, one in push_tokens.registrations(self._root).items()
            if one.live_activity_token
        }
        for gone in self._activity.keys() - running.keys():
            del self._activity[gone]
        nxt = self.next_reminder()
        state = {
            "state": self.her_state(),
            "next_text": None if nxt is None else nxt[0][:_ACTIVITY_TEXT],
            "next_at_ms": None if nxt is None else nxt[1],
        }
        wire = json.dumps(state, sort_keys=True)
        for name, token in running.items():
            if self._activity.get(name) == (str(token), wire):
                continue
            # Marked before it goes: a failed update waits for the next change, it is not retried.
            self._activity[name] = (str(token), wire)
            self._send_one(name, "live_activity", state)

    # --- delivery ---------------------------------------------------------------------

    def _deliver(
        self, message: PushMessage, skip: Callable[[str], bool] = lambda _device: False,
    ) -> int:
        alert_payload(message)  # a bad url or label is the caller's to hear about, once
        if self._client is None:
            return 0
        return sum(
            self._send_one(name, "device", message)
            for name in push_tokens.registrations(self._root)
            if not skip(name)
        )

    def _send_one(
        self, name: str, kind: push_tokens.Kind, what: PushMessage | dict[str, Any],
    ) -> int:
        """Push to one device's token of ``kind``; a 410 forgets that token. 1 if it took it."""
        one = push_tokens.registrations(self._root).get(name)
        if self._client is None or one is None:
            return 0
        try:
            if isinstance(what, PushMessage):
                outcome = self._client.send_alert(
                    one.device_token, one.environment, what, label=name,
                )
            elif one.live_activity_token is None:
                return 0
            else:
                outcome = self._client.send_activity(
                    one.live_activity_token, one.environment, what, label=name,
                )
        except Exception as exc:  # noqa: BLE001 — a push must never take its caller down.
            LOGGER.warning("push: to %s failed (%s)", name, type(exc).__name__)
            return 0
        if outcome is Outcome.UNREGISTERED:
            push_tokens.drop_token(self._root, name, kind)
        return int(outcome is Outcome.SENT)

    @staticmethod
    def _safely(call: Callable[..., object], *args: object) -> None:
        try:
            call(*args)
        except Exception as exc:  # noqa: BLE001 — the watcher outlives any one failure.
            LOGGER.warning(
                "push: %s failed (%s)", getattr(call, "__name__", "call"), type(exc).__name__,
            )
