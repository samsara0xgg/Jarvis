"""ADR 0266: a verification code that arrives in Allen's mail is copied to the Mac clipboard.

Every ``poll_s`` the brain lists the mail of the last ``window_min`` minutes through the shared
``gmail`` MCP client (``gmail_search`` and ``gmail_get`` only: nothing is ever changed), finds a
code with :func:`find_code` (local rules, no model) and asks the terminal's hidden ``copy_code``
operation to put it on the clipboard and show a system notification. The code is never stored
and never logged; each mail is handled once.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, Final

from jarvis.runtime.home import MAIL_SERVER, _letter, mail_body
from jarvis.runtime.job_mail import gmail_read
from jarvis.shared import lang
from jarvis.shared.device_link import COPY_CODE, DeviceCallError

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.shared.device_link import DeviceLink

LOGGER = logging.getLogger(__name__)

_QUERY: Final = "after:{epoch} -in:sent -in:drafts"
_SEARCH_LIMIT: Final = 20
_SEEN_KEEP: Final = 500
_PBCOPY: Final = "/usr/bin/pbcopy"
_OSASCRIPT: Final = "/usr/bin/osascript"
_NOTIFY: Final = (
    "on run argv\n"
    'display notification (item 1 of argv) with title "Jarvis" subtitle (item 2 of argv)\n'
    "end run"
)
_TIMEOUT_S: Final = 5.0

# --- finding the code ---------------------------------------------------------------

_STRONG: Final = (
    r"verification|verify|one[- ]time|passcode|\botp\b|security code|(?:log|sign)[- ]?in code"
    r"|验证码|校验码|动态码|确认码|認証コード"
)
_KEYWORD: Final = re.compile(rf"(?P<strong>{_STRONG})|(?P<weak>\bcode\b|\bpin\b)", re.IGNORECASE)
# A "code" that is not a login code: promo, postal and similar words just before the keyword.
_NOT_LOGIN: Final = re.compile(
    r"(?:promo\w*|coupon|discount|gift|referral|zip|postal|area|country|source|tracking|invit\w*"
    r"|优惠|折扣)\W*$",
    re.IGNORECASE,
)
_URL: Final = re.compile(r"https?://\S+")
_TOKEN: Final = re.compile(r"(?<![A-Za-z0-9])(\d{3} \d{3}|[A-Za-z0-9]{4,8})(?![A-Za-z0-9])")
# A 4-digit number that could be a year is a code only right after the keyword.
_YEAR_REACH: Final = 12
_YEAR_LEN: Final = 4
_MIXED_MIN: Final = 6
_AFTER_CHARS: Final = 60
_BEFORE_CHARS: Final = 40


def _plausible(text: str, found: re.Match[str], *, strong: bool, year_ok: bool) -> str | None:
    """The code the token ``found`` stands for, or ``None`` when it is a date, number or word."""
    token = found.group(1)
    before = text[max(0, found.start(1) - 12) : found.start(1)]
    after = text[found.end(1) : found.end(1) + 2]
    if re.search(r"\d[-.,/]$|[$¥€£#+]$", before) or re.match(r"[-.,/:]\d|%", after):
        return None
    digits = token.replace(" ", "")
    if digits.isdigit():
        if len(digits) == _YEAR_LEN and digits[:2] in ("19", "20") and not year_ok:
            return None
        return digits
    # ponytail: letters and digits mixed only under a strong keyword and in capitals, so a promo
    # code or a word with a digit in it is not taken; add 6-8 lowercase if a sender needs it.
    if strong and len(token) >= _MIXED_MIN and token.upper() == token and any(
        c.isdigit() for c in token
    ) and any(c.isalpha() for c in token):
        return token
    return None


def _first(text: str, lo: int, hi: int, *, strong: bool, last: bool = False) -> str | None:
    """The first plausible code in ``text[lo:hi]`` (the last one when ``last``)."""
    found = [
        code
        for m in _TOKEN.finditer(text, max(0, lo), hi)
        if (
            code := _plausible(
                text, m, strong=strong, year_ok=not last and m.start(1) - lo <= _YEAR_REACH,
            )
        )
    ]
    if not found:
        return None
    return found[-1] if last else found[0]


def find_code(subject: str, body: str) -> str | None:
    """The verification code in a mail, or ``None``; the subject is read before the body.

    A keyword (code, verification, OTP, 验证码 ...) must stand within a few words of a 4-8 digit
    number (or a 6-8 character capital mix under a strong keyword). Of the numbers after a
    keyword the nearest wins; one just before it counts when none follows ("G-123456 is your
    Google verification code").
    """
    for part in (subject, body):
        text = _URL.sub(" ", part)
        hits = [
            m for m in _KEYWORD.finditer(text) if not _NOT_LOGIN.search(text[: m.start()][-24:])
        ]
        for m in hits:
            if code := _first(text, m.end(), m.end() + _AFTER_CHARS, strong=bool(m["strong"])):
                return code
        for m in hits:
            if code := _first(
                text, m.start() - _BEFORE_CHARS, m.start(), strong=bool(m["strong"]), last=True
            ):
                return code
    return None


# --- putting it on the clipboard ----------------------------------------------------


def copy_code(code: str, sender: str) -> dict[str, Any]:
    """Copy ``code`` to the macOS clipboard and show a notification; the terminal's ``copy_code``.

    The notification text goes in as script arguments, never into the script, so nothing in a
    sender's name can run. A notification that fails is reported, not fatal: the code is copied.
    """
    if not re.fullmatch(r"[A-Za-z0-9]{4,8}", code):
        return {"ok": False, "code": "bad_code", "message": "that is not a verification code"}
    try:
        subprocess.run(  # noqa: S603 - fixed path, the code is stdin
            [_PBCOPY], input=code.encode(), check=True, timeout=_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "code": "clipboard_failed", "message": f"pbcopy failed: {exc}"}
    notified = True
    try:
        subprocess.run(  # noqa: S603 - fixed path, text passed as arguments
            [_OSASCRIPT, "-e", _NOTIFY, lang.t("mail.code_copied", code=code), sender[:80]],
            check=True, capture_output=True, timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        notified = False
    return {"ok": True, "output": {"copied": True, "notified": notified}}


# --- the poller ---------------------------------------------------------------------


@dataclass(frozen=True)
class MailCodesSettings:
    """The ``mail_codes:`` block of ``config/jarvis.yaml`` once validated."""

    poll_s: float
    window_min: int


class MailCodes:
    """The poller; built once at boot, started by the daemon."""

    def __init__(
        self, settings: MailCodesSettings, connections: PluginConnections,
        device: DeviceLink | None = None,
    ) -> None:
        """``device`` is the link to the terminal; ``None`` copies on this machine."""
        self._settings = settings
        self._connections = connections
        self._device = device
        self._seen: dict[str, None] = {}
        self._last_error = ""
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def run(self) -> None:
        """Poll every ``poll_s``, first at once; a failed cycle is logged, never ends the loop."""
        LOGGER.info("mail codes started (every %.0f s)", self._settings.poll_s)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.poll_once)
                    self._last_error = ""
                except Exception as exc:  # noqa: BLE001 - logged once per distinct failure
                    if str(exc) != self._last_error:
                        self._last_error = str(exc)
                        LOGGER.warning("mail codes: cycle failed: %s", exc)
                await asyncio.sleep(self._settings.poll_s)
        except asyncio.CancelledError:
            LOGGER.info("mail codes cancelled")
            raise

    def poll_once(self) -> int:
        """One cycle; how many codes were handed over."""
        window = timedelta(minutes=self._settings.window_min)
        now = self.now()
        servers = self._connections.client_for(MAIL_SERVER)
        reply = gmail_read(servers, "gmail_search", {
            "query": _QUERY.format(epoch=int((now - window).timestamp())),
            "maxResults": _SEARCH_LIMIT,
        })
        sent = 0
        for head in reply.get("messages") or []:
            message_id = str(head["id"])
            if message_id in self._seen:
                continue
            try:
                message = gmail_read(
                    servers, "gmail_get", {"messageId": message_id, "format": "full"},
                )
            except Exception:  # noqa: BLE001 - tried again next cycle while it is in the window
                LOGGER.warning("mail codes: cannot read %s", message_id, exc_info=True)
                continue
            sent += self._handle(message_id, message, now, window)
        return sent

    def _handle(
        self, message_id: str, message: dict[str, Any], now: datetime, window: timedelta,
    ) -> int:
        letter = _letter(message)
        if letter["received"] and datetime.fromisoformat(letter["received"]) < now - window:
            self._remember(message_id)
            return 0
        code = find_code(letter["subject"], mail_body(str(message.get("body") or "")))
        if code is None:
            self._remember(message_id)
            return 0
        domain = parseaddr(str(message.get("from") or ""))[1].rpartition("@")[2]
        try:
            self._deliver(code, letter["from"])
        except DeviceCallError as exc:
            LOGGER.warning("mail codes: %s from %s not delivered: %s", message_id, domain, exc.code)
            if exc.code != "device_not_connected":  # a terminal that is away may come back in time
                self._remember(message_id)
            return 0
        except Exception:
            LOGGER.exception("mail codes: %s from %s not delivered", message_id, domain)
            self._remember(message_id)
            return 0
        self._remember(message_id)
        LOGGER.info("mail codes: handed over the code in %s from %s", message_id, domain)
        return 1

    def _deliver(self, code: str, sender: str) -> None:
        if self._device is None:
            reply = copy_code(code, sender)
            if not reply["ok"]:
                raise DeviceCallError(reply["message"], code=reply["code"])
            return
        self._device(COPY_CODE, {"code": code, "sender": sender}, None)

    def _remember(self, message_id: str) -> None:
        self._seen[message_id] = None
        while len(self._seen) > _SEEN_KEEP:
            del self._seen[next(iter(self._seen))]

