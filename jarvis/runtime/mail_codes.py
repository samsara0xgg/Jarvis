"""ADR 0266: a verification code that arrives in Allen's mail is copied to the Mac clipboard.

Every ``poll_s`` the brain lists the mail of the last ``window_min`` minutes through the shared
``gmail`` MCP client (``gmail_search`` and ``gmail_get`` only: nothing is ever changed), finds a
code with :func:`find_code` (local rules, no model) and asks the terminal's hidden ``copy_code``
operation to put it on the clipboard and show a system notification. The code is never stored
and never logged. A mail a code was copied from is recorded in the event log (its id and its
sender's domain), so it is copied at most once across restarts (ADR 0369); a mail with no code is
remembered in memory only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess
import time
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, Final

from jarvis.runtime.home import MAIL_SERVER, _letter, mail_body
from jarvis.runtime.job_mail import gmail_read
from jarvis.shared import lang
from jarvis.shared.device_link import COPY_CODE, DeviceCallError
from jarvis.state import mail_codes as copies
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

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
_LOG_WAIT_S: Final = 5.0

# --- finding the code ---------------------------------------------------------------

_STRONG: Final = (
    r"verification|verify|one[- ]time|passcode|\botp\b|security code|(?:log|sign)[- ]?in code"
    r"|验证码|校验码|动态码|确认码|認証コード"
)
_KEYWORD: Final = re.compile(rf"(?P<strong>{_STRONG})|(?P<weak>\bcode\b|\bpin\b)", re.IGNORECASE)
_STRONG_WORD: Final = re.compile(_STRONG, re.IGNORECASE)
# A "code" that is not a login code: promo, postal and similar words just before the keyword.
_NOT_LOGIN: Final = re.compile(
    r"(?:promo\w*|coupon|discount|gift|referral|zip|postal|area|country|source|tracking|invit\w*"
    r"|优惠|折扣)\W*$",
    re.IGNORECASE,
)
# What a bare "code" or "PIN" is for (ADR 0369): it is a login code only in a sentence that also
# says signing in, signing up, verifying, confirming or authenticating.
_LOGIN_WORD: Final = re.compile(
    r"\b(?:sign(?:ing)?[- ]?(?:in(?:to)?|up)|log(?:ging)?[- ]?in(?:to)?)\b"
    r"|\b(?:verif(?:y|ie[sd]|ying|ication)|confirm|authenticat|two[- ]?step|2[- ]?step)"
    r"|登录|登陆|注册|验证|确认",
    re.IGNORECASE,
)
# A sentence ends at a full stop, "!" or "?" before a space or the end ("no." is not one), at the
# full-width stop, "!" or "?", and at a line break.
_SENTENCE_END: Final = re.compile(
    r"(?<!\bno)[.!?]+(?=\s|$)|[\u3002\uff01\uff1f]|[\r\n]", re.IGNORECASE
)
# What makes a number another kind of number (ADR 0369), read just before it: an account, order,
# invoice, reference, member, customer or tracking number (the word alone, or with number, no., #
# or ID after it), a confirmation or booking number (a "confirmation code" is still a code), a
# promo, coupon or gift code.
_SP: Final = r"[ \t\xa0]"  # horizontal space, the no-break space of an HTML mail included
_KIND_EN: Final = (
    r"(?:account|acct|order|invoice|ref(?:erence)?|member(?:ship)?|customer|tracking)"
    rf"(?:{_SP}*(?:number|no\b\.?|#|id\b))?"
    rf"|(?:confirmation|booking){_SP}*(?:number|no\b\.?|#|id\b)"
    rf"|(?:promo\w*|coupon|gift)(?:{_SP}*(?:code|card))?"
)
_KIND_ZH: Final = (
    r"(?:账号|账户|订单|发票|会员|客户|运单|快递|物流|优惠|折扣|礼品)(?:单号|号码?|编号|码)?"
    r"|(?:确认|预订|预定)(?:号码?|编号)"
)
# ... then "is", "are", "ending in", a colon or a dash, up to the number itself.
_NAMED_ELSE: Final = re.compile(
    rf"(?:\b(?:{_KIND_EN})|{_KIND_ZH})"
    rf"(?:{_SP}*(?:\bis\b|\bare\b|\bending(?:{_SP}+in)?\b|是|为|[:=#\uff1a-]))*{_SP}*$",
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
_NAME_REACH: Final = 40


def _plausible(text: str, found: re.Match[str], *, strong: bool, year_ok: bool) -> str | None:
    """The code the token ``found`` stands for, or ``None`` when it is a date, number or word."""
    token = found.group(1)
    before = text[max(0, found.start(1) - 12) : found.start(1)]
    after = text[found.end(1) : found.end(1) + 2]
    if re.search(r"\d[-.,/]$|[$¥€£#+]$", before) or re.match(r"[-.,/:]\d|%", after):
        return None
    if _NAMED_ELSE.search(text, max(0, found.start(1) - _NAME_REACH), found.start(1)):
        return None  # an account, order or promo number, not a login code
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


def _keywords(text: str, *, covered: bool) -> list[re.Match[str]]:
    """The keywords of ``text`` that name a login code.

    A "code" after promo, postal and similar words is not one, and a bare "code" or "PIN" is one
    only in a sentence that says what it is for (ADR 0369) or when ``covered`` says the subject did.
    """
    ends = [(m.start(), m.end()) for m in _SENTENCE_END.finditer(text)]
    starts = [start for start, _ in ends]
    said: dict[int, bool] = {}  # sentence number -> whether it has a login word

    def says_login(at: int) -> bool:
        """Whether the sentence that holds the position ``at`` has a login word."""
        k = bisect_left(starts, at)
        if k not in said:
            first = ends[k - 1][1] if k else 0
            last = starts[k] if k < len(starts) else len(text)
            said[k] = _LOGIN_WORD.search(text, first, last) is not None
        return said[k]

    return [
        m
        for m in _KEYWORD.finditer(text)
        if not _NOT_LOGIN.search(text[: m.start()][-24:])
        and (m["strong"] or covered or says_login(m.start()))
    ]


def find_code(subject: str, body: str) -> str | None:
    """The verification code in a mail, or ``None``; the subject is read before the body.

    A keyword (code, verification, OTP, 验证码 ...) must stand within a few words of a 4-8 digit
    number (or a 6-8 character capital mix under a strong keyword). A bare "code" or "PIN" counts
    only in a sentence that also says sign in, verify, confirm ... (a strong word in the subject
    covers the body), and a number is not taken when the words just before it make it another kind
    of number ("account 20481", "order 12345", "gift code 482913"). Of the numbers left after a
    keyword the nearest wins; one just before it counts when none follows ("G-123456 is your
    Google verification code").
    """
    subject_says = _STRONG_WORD.search(_URL.sub(" ", subject)) is not None
    for part, covered in ((subject, False), (body, subject_says)):
        text = _URL.sub(" ", part)
        hits = _keywords(text, covered=covered)
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
        self, settings: MailCodesSettings, connections: PluginConnections, event_log: Path,
        device: DeviceLink | None = None,
    ) -> None:
        """``event_log`` holds the record of the mails a code was copied from (ADR 0369).

        ``device`` is the link to the terminal; ``None`` copies on this machine.
        """
        self._settings = settings
        self._connections = connections
        self._event_log = event_log
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
        listed = [str(head["id"]) for head in reply.get("messages") or []]
        if not listed:
            return 0
        sent = 0
        with contextlib.closing(self._log()) as conn:
            # Before any mail is read, whatever run of the brain it was: a mail a code was copied
            # from is not read again (ADR 0369). A log that cannot be read ends the cycle here.
            copied = copies.copied_among(conn, listed)
            for message_id in listed:
                if message_id in self._seen or message_id in copied:
                    continue
                try:
                    message = gmail_read(
                        servers, "gmail_get", {"messageId": message_id, "format": "full"},
                    )
                except Exception:  # noqa: BLE001 - tried again next cycle while it is in the window
                    LOGGER.warning("mail codes: cannot read %s", message_id, exc_info=True)
                    continue
                sent += self._handle(conn, message_id, message, now - window)
        return sent

    def _handle(
        self, conn: sqlite3.Connection, message_id: str, message: dict[str, Any],
        cutoff: datetime,
    ) -> int:
        letter = _letter(message)
        if letter["received"] and datetime.fromisoformat(letter["received"]) < cutoff:
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
        try:
            copies.record_copy(conn, message_id, domain)
        except Exception:  # the code is copied: a restart inside the window could copy it again
            LOGGER.exception(
                "mail codes: the copy of %s from %s is not recorded", message_id, domain,
            )
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

    def _log(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._event_log, deadline=time.monotonic() + _LOG_WAIT_S)

