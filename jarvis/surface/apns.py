"""ADR 0210: Apple Push Notification service, token-based: provider token, payloads, client.

Three small pieces, none of which knows what Jarvis is pushing about:

- :class:`ProviderToken` signs the ES256 JWT APNs wants (header ``alg`` and ``kid``, claims ``iss``
  and ``iat``) with the owner's ``.p8`` key and reuses it until it is :data:`TOKEN_TTL_S` old,
  because APNs refuses a token older than an hour and refuses one refreshed too often;
- :func:`alert_payload` and :func:`activity_payload` build the two bodies the app handles;
- :class:`ApnsClient` posts one of them to one device over HTTP/2, to the sandbox or the
  production endpoint as the device registered, and answers :class:`Outcome`.

Nothing here logs a key, a JWT, a device token or the text of a notification: a failure is logged
as the device's name, what was sent and APNs's own ``reason`` word. A request is never retried.
"""

from __future__ import annotations

import base64
import enum
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

LOGGER = logging.getLogger(__name__)

REGISTER_PATH: Final = "/inherent/device/push"
MAX_REGISTER_BYTES: Final = 4096
HOSTS: Final[Mapping[str, str]] = {
    "sandbox": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}
# APNs rejects a provider token older than 60 minutes and one that is replaced more than once
# every 20, so a token is reused for 50.
TOKEN_TTL_S: Final = 50 * 60
# A notification APNs could not hand over at once is kept this long for a phone that is off.
ALERT_TTL_S: Final = 3600
TIMEOUT_S: Final = 10.0
# APNs refuses a body over 4096 bytes; these caps keep any alert far under it in UTF-8.
MAX_TITLE: Final = 100
MAX_BODY: Final = 600
MAX_URL: Final = 1024
MAX_LABEL: Final = 64
_SCHEME_END: Final = ":"


class _HideDeviceTokens(logging.Filter):
    """Drops httpx's INFO line for a push: its URL is ``/3/device/<token>``."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/3/device/" not in record.getMessage()


_HIDE_TOKENS: Final = _HideDeviceTokens()


def _keep_secrets_out_of_library_logs() -> None:
    """The client libraries log what a push is made of; nothing of that may reach the log.

    httpx writes each request's URL at INFO (it holds the device token), and the HTTP/2 header
    codec writes every header at DEBUG (one is the provider token).
    """
    logging.getLogger("httpx").addFilter(_HIDE_TOKENS)
    for name in ("hpack", "h2"):
        logging.getLogger(name).setLevel(logging.WARNING)


class Outcome(enum.Enum):
    """What APNs did with one push."""

    SENT = "sent"
    UNREGISTERED = "unregistered"  # 410: the token is no longer one APNs delivers to
    FAILED = "failed"


@dataclass(frozen=True)
class PushMessage:
    """One notification as the app shows it.

    ``url`` is what a tap opens; ``category`` names the app's action set; ``thread`` groups
    notifications in the notification center. ``sound`` off makes it a silent banner, and
    ``time_sensitive`` lets it through Focus.
    """

    title: str
    body: str
    url: str | None = None
    category: str | None = None
    thread: str | None = None
    sound: bool = True
    time_sensitive: bool = False


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def alert_payload(message: PushMessage) -> bytes:
    """The JSON body of an ``alert`` push: ``aps`` with the alert, sound, thread and category.

    ``url`` rides beside ``aps`` as the app's own key.

    Raises:
        ValueError: ``url`` has no scheme or is over :data:`MAX_URL`, or ``category`` or
            ``thread`` is empty or over :data:`MAX_LABEL`.
    """
    aps: dict[str, Any] = {"alert": {"title": _clip(message.title, MAX_TITLE),
                                     "body": _clip(message.body, MAX_BODY)}}
    if message.sound:
        aps["sound"] = "default"
    if message.time_sensitive:
        aps["interruption-level"] = "time-sensitive"
    for key, value in (("thread-id", message.thread), ("category", message.category)):
        if value is not None:
            if not value or len(value) > MAX_LABEL:
                msg = f"{key} is 1 to {MAX_LABEL} characters"
                raise ValueError(msg)
            aps[key] = value
    payload: dict[str, Any] = {"aps": aps}
    if message.url is not None:
        scheme, found, _ = message.url.partition(_SCHEME_END)
        if not (found and scheme.isascii() and scheme.isalpha()) or len(message.url) > MAX_URL:
            msg = f"url is a link with a scheme, up to {MAX_URL} characters"
            raise ValueError(msg)
        payload["url"] = message.url
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def activity_payload(content_state: Mapping[str, Any], now_s: int) -> bytes:
    """The JSON body of a Live Activity update: ``event`` ``update`` and the new content state."""
    return json.dumps(
        {"aps": {"timestamp": now_s, "event": "update", "content-state": dict(content_state)}},
        ensure_ascii=False, separators=(",", ":"),
    ).encode()


def load_signing_key(pem: bytes) -> ec.EllipticCurvePrivateKey:
    """The P-256 key in a ``.p8`` (PKCS#8 PEM) file.

    Raises:
        ValueError: not an unencrypted PEM private key, or not on P-256 (the message says which
            and never carries any of the bytes).
    """
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        msg = "not an unencrypted PEM private key"
        raise ValueError(msg) from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        msg = "not an EC P-256 key"
        raise ValueError(msg)
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class ProviderToken:
    """The ES256 JWT for one key, signed lazily and reused until it is :data:`TOKEN_TTL_S` old."""

    def __init__(
        self,
        key: ec.EllipticCurvePrivateKey,
        key_id: str,
        team_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Hold the key; ``key_id`` is the key's own id, ``team_id`` the developer team's."""
        self._key = key
        self._header = _b64(
            json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode(),
        )
        self._team_id = team_id
        self._clock = clock
        self._lock = threading.Lock()
        self._held: tuple[str, float] | None = None

    def current(self) -> str:
        """The token to send now: the held one while it is young enough, else a new one."""
        with self._lock:
            now = self._clock()
            if self._held is not None and now - self._held[1] < TOKEN_TTL_S:
                return self._held[0]
            claims = _b64(json.dumps({"iss": self._team_id, "iat": int(now)},
                                     separators=(",", ":")).encode())
            signing_input = f"{self._header}.{claims}"
            r, s = decode_dss_signature(
                self._key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())),
            )
            signature = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
            self._held = (f"{signing_input}.{signature}", now)
            return self._held[0]

    def discard(self) -> None:
        """Forget the held token (APNs said it is expired or invalid); the next one is new."""
        with self._lock:
            self._held = None


class ApnsClient:
    """Posts pushes to APNs over HTTP/2; one connection per endpoint, kept alive."""

    def __init__(
        self,
        token: ProviderToken,
        bundle_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        hosts: Mapping[str, str] = HOSTS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``bundle_id`` is the app's ``apns-topic``; ``transport`` and ``hosts`` serve tests."""
        self._token = token
        self._bundle_id = bundle_id
        self._hosts = hosts
        self._clock = clock
        _keep_secrets_out_of_library_logs()
        self._http = httpx.Client(http2=True, timeout=TIMEOUT_S, transport=transport)

    def close(self) -> None:
        """Close the connections."""
        self._http.close()

    def send_alert(
        self, device_token: str, environment: str, message: PushMessage, *, label: str,
    ) -> Outcome:
        """Push ``message`` to a device; ``label`` is the device's name, for the log only."""
        return self._post(
            device_token, environment, label, "alert", self._bundle_id, alert_payload(message),
            priority=10, expiration=int(self._clock()) + ALERT_TTL_S,
        )

    def send_activity(
        self, activity_token: str, environment: str, content_state: Mapping[str, Any], *,
        label: str,
    ) -> Outcome:
        """Update the running Live Activity whose push token is ``activity_token``."""
        return self._post(
            activity_token, environment, label, "liveactivity",
            f"{self._bundle_id}.push-type.liveactivity",
            activity_payload(content_state, int(self._clock())), priority=5, expiration=0,
        )

    def _post(  # noqa: PLR0913 — one APNs header each.
        self, token: str, environment: str, label: str, push_type: str, topic: str, body: bytes,
        *, priority: int, expiration: int,
    ) -> Outcome:
        headers = {
            "authorization": f"bearer {self._token.current()}",
            "apns-push-type": push_type,
            "apns-topic": topic,
            "apns-priority": str(priority),
            "apns-expiration": str(expiration),
            "content-type": "application/json",
        }
        try:
            reply = self._http.post(
                f"{self._hosts[environment]}/3/device/{token}", content=body, headers=headers,
            )
        except (httpx.HTTPError, KeyError) as exc:
            # The class only: an httpx message can carry the URL, which has the device token.
            LOGGER.warning(
                "push: %s to %s not delivered (%s)", push_type, label, type(exc).__name__,
            )
            return Outcome.FAILED
        if reply.status_code == httpx.codes.OK:
            return Outcome.SENT
        reason = _reason(reply)
        if reply.status_code == httpx.codes.GONE:
            LOGGER.info("push: %s token of %s is unregistered; dropped", push_type, label)
            return Outcome.UNREGISTERED
        if reason in {"ExpiredProviderToken", "InvalidProviderToken"}:
            self._token.discard()
        LOGGER.warning(
            "push: %s to %s refused: %d %s", push_type, label, reply.status_code, reason,
        )
        return Outcome.FAILED


def _reason(reply: httpx.Response) -> str:
    """APNs's own ``reason`` word (``BadDeviceToken``…); nothing else of the body is kept."""
    try:
        reason = reply.json().get("reason")
    except (ValueError, AttributeError):
        return "?"
    return reason if isinstance(reason, str) and reason.isalnum() else "?"
