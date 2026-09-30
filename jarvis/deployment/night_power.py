"""ADR 0093: the Mac switches a night run uses — keep awake, display, brightness, sound.

Every call answers what it managed to do and never raises for an OS that says
no: on another OS, or with a framework missing, reads answer ``None`` and
writes answer ``False``. The keep-awake hold is one named
``PreventUserIdleSystemSleep`` assertion whose timeout powerd enforces, so a
hung or killed daemon never keeps the Mac awake past the deadline, and the
assertion dies with this process anyway.

Brightness is the built-in panel's, through the private DisplayServices
framework (Apple Silicon has no public call). Sound is muted device by device
through CoreAudio: a Multi-Output Device has no level or mute of its own
(AppleScript reads it as ``missing value``), so each of its active sub-devices
is muted instead. Presence is the session's lock state and the seconds since
the last keyboard or mouse input.

Layer placement: L6 deployment. stdlib and L1's product name; nothing here
imports ``jarvis.state`` (H13), the runtime records what these calls did.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Final

from jarvis.constitution import JARVIS_IDENTITY

LOGGER = logging.getLogger(__name__)

ASSERTION_NAME: Final = f"{JARVIS_IDENTITY} night run"  # ADR 0091: the product name
_DETAILS: Final = "Keeps agents running through the night; released at the deadline."
_IOKIT: Final = "/System/Library/Frameworks/IOKit.framework/IOKit"
_CORE_FOUNDATION: Final = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_CORE_GRAPHICS: Final = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CORE_AUDIO: Final = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
_DISPLAY_SERVICES: Final = (
    "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"
)
_UTF8: Final = 0x08000100  # kCFStringEncodingUTF8
_HID_SYSTEM_STATE: Final = 1  # kCGEventSourceStateHIDSystemState
_ANY_INPUT_EVENT: Final = 0xFFFFFFFF  # kCGAnyInputEventType
_MAX_DISPLAYS: Final = 16
_TIMEOUT_S: Final = 5
_PERCENT_RE: Final = re.compile(r"(\d{1,3})%")

# CoreAudio: the system object, the property codes and elements used here.
_AUDIO_SYSTEM: Final = 1  # kAudioObjectSystemObject
_GLOBAL: Final = int.from_bytes(b"glob", "big")
_OUTPUT: Final = int.from_bytes(b"outp", "big")
_DEVICES: Final = int.from_bytes(b"dev#", "big")
_DEFAULT_OUTPUT: Final = int.from_bytes(b"dOut", "big")
_DEVICE_UID: Final = int.from_bytes(b"uid ", "big")
_SUB_DEVICES: Final = int.from_bytes(b"agrp", "big")  # an aggregate's active sub-devices
_STREAMS: Final = int.from_bytes(b"stm#", "big")
_MUTE: Final = int.from_bytes(b"mute", "big")
_MAIN: Final = 0  # kAudioObjectPropertyElementMain
_CHANNELS: Final = range(1, 9)  # a device without a main mute may have one per channel
_MAX_AUDIO_IDS: Final = 256


@dataclass(frozen=True)
class Presence:
    """Whether the login session is locked, and seconds since the last input."""

    locked: bool
    idle_s: float


@dataclass(frozen=True)
class Battery:
    """Whether the Mac runs on its battery now, and the charge left."""

    on_battery: bool
    percent: int


def parse_battery(text: str) -> Battery | None:
    """``pmset -g batt`` output -> Battery; None on a Mac without one."""
    if "InternalBattery" not in text:
        return None
    percent = _PERCENT_RE.search(text)
    if percent is None:
        return None
    return Battery(
        on_battery="'Battery Power'" in text,
        percent=max(0, min(100, int(percent.group(1)))),
    )


def _run(argv: list[str]) -> str | None:
    """Run a fixed macOS command; its stdout, or None when it failed."""
    try:
        done = subprocess.run(  # noqa: S603 — fixed argv of macOS's own tools.
            argv, check=True, capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        LOGGER.warning("night run: %s failed", argv[0], exc_info=True)
        return None
    return done.stdout


class _Address(ctypes.Structure):
    """CoreAudio's AudioObjectPropertyAddress."""

    _fields_ = (
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    )


type _AudioData = ctypes.c_uint32 | ctypes.c_void_p | ctypes.Array[ctypes.c_uint32]


class _Frameworks:
    """The frameworks behind the ctypes calls, each function's types pinned once."""

    def __init__(self) -> None:
        self.iokit = ctypes.CDLL(_IOKIT)
        self.cf = ctypes.CDLL(_CORE_FOUNDATION)
        self.cg = ctypes.CDLL(_CORE_GRAPHICS)
        self.ca = ctypes.CDLL(_CORE_AUDIO)
        try:
            self.ds: ctypes.CDLL | None = ctypes.CDLL(_DISPLAY_SERVICES)
        except OSError:
            self.ds = None
        cf, cg, iokit = self.cf, self.cg, self.iokit
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetTypeID.argtypes = []
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
        ]
        iokit.IOPMAssertionCreateWithDescription.restype = ctypes.c_int
        iokit.IOPMAssertionCreateWithDescription.argtypes = [
            ctypes.c_void_p,  # AssertionType
            ctypes.c_void_p,  # Name
            ctypes.c_void_p,  # Details
            ctypes.c_void_p,  # HumanReadableReason
            ctypes.c_void_p,  # LocalizationBundlePath
            ctypes.c_double,  # Timeout (CFTimeInterval)
            ctypes.c_void_p,  # TimeoutAction
            ctypes.POINTER(ctypes.c_uint32),  # AssertionID out
        ]
        iokit.IOPMAssertionRelease.restype = ctypes.c_int
        iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
        cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
        cg.CGSessionCopyCurrentDictionary.argtypes = []
        cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32, ctypes.c_uint32]
        cg.CGGetOnlineDisplayList.restype = ctypes.c_int32
        cg.CGGetOnlineDisplayList.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
        ]
        cg.CGDisplayIsBuiltin.restype = ctypes.c_uint32
        cg.CGDisplayIsBuiltin.argtypes = [ctypes.c_uint32]
        address = ctypes.POINTER(_Address)
        self.ca.AudioObjectHasProperty.restype = ctypes.c_bool
        self.ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, address]
        self.ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self.ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        self.ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
        self.ca.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p,
        ]
        if self.ds is not None:
            self.ds.DisplayServicesGetBrightness.restype = ctypes.c_int
            self.ds.DisplayServicesGetBrightness.argtypes = [
                ctypes.c_uint32, ctypes.POINTER(ctypes.c_float),
            ]
            self.ds.DisplayServicesSetBrightness.restype = ctypes.c_int
            self.ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]

    def cfstring(self, text: str) -> int:
        """A +1 CFString the caller releases."""
        ref: int | None = self.cf.CFStringCreateWithCString(None, text.encode(), _UTF8)
        if not ref:
            msg = f"CFStringCreateWithCString failed for {text!r}"
            raise OSError(msg)
        return ref

    def flag(self, dictionary: int, key: str) -> bool | None:
        """A CFBoolean entry of ``dictionary``; None when the key is absent."""
        name = self.cfstring(key)
        try:
            value: int | None = self.cf.CFDictionaryGetValue(dictionary, name)
        finally:
            self.cf.CFRelease(name)
        if not value:
            return None
        if self.cf.CFGetTypeID(value) != self.cf.CFBooleanGetTypeID():
            return True
        return bool(self.cf.CFBooleanGetValue(value))

    def builtin_display(self) -> int | None:
        """The built-in panel's display id, when it is online."""
        ids = (ctypes.c_uint32 * _MAX_DISPLAYS)()
        count = ctypes.c_uint32(0)
        if self.cg.CGGetOnlineDisplayList(_MAX_DISPLAYS, ids, ctypes.byref(count)) != 0:
            return None
        for index in range(min(count.value, _MAX_DISPLAYS)):
            if self.cg.CGDisplayIsBuiltin(ids[index]):
                return int(ids[index])
        return None

    def audio_get(self, audio_object: int, address: _Address, out: _AudioData) -> int | None:
        """Read a CoreAudio property into ``out``; the bytes written, or None."""
        size = ctypes.c_uint32(ctypes.sizeof(out))
        code = self.ca.AudioObjectGetPropertyData(
            audio_object, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(out),
        )
        return int(size.value) if code == 0 else None

    def audio_ids(self, audio_object: int, selector: int) -> list[int]:
        """A list-of-ids property (``dev#``, ``agrp``); empty when there is none."""
        ids = (ctypes.c_uint32 * _MAX_AUDIO_IDS)()
        written = self.audio_get(audio_object, _Address(selector, _GLOBAL, _MAIN), ids)
        count = 0 if written is None else written // ctypes.sizeof(ctypes.c_uint32)
        return [int(ids[index]) for index in range(count)]

    def audio_uid(self, device: int) -> str | None:
        """The device's UID, which, unlike its id, outlives a replug or a restart."""
        ref = ctypes.c_void_p(0)
        if self.audio_get(device, _Address(_DEVICE_UID, _GLOBAL, _MAIN), ref) is None:
            return None
        if not ref.value:
            return None
        text = ctypes.create_string_buffer(512)
        try:
            if not self.cf.CFStringGetCString(ref, text, len(text), _UTF8):
                return None
        finally:
            self.cf.CFRelease(ref)
        return text.value.decode("utf-8", errors="replace")

    def device(self, uid: str) -> int | None:
        """The id the device with ``uid`` has now, when it is there."""
        for device in self.audio_ids(_AUDIO_SYSTEM, _DEVICES):
            if self.audio_uid(device) == uid:
                return device
        return None

    def plays(self, device: int) -> bool:
        """Whether the device has output streams: an aggregate may hold a microphone too."""
        size = ctypes.c_uint32(0)
        code = self.ca.AudioObjectGetPropertyDataSize(
            device, ctypes.byref(_Address(_STREAMS, _OUTPUT, _MAIN)), 0, None, ctypes.byref(size),
        )
        return code == 0 and size.value > 0

    def mutes(self, device: int) -> list[_Address]:
        """Where the device's output takes a mute: its main element, else each channel's."""
        main = _Address(_MUTE, _OUTPUT, _MAIN)
        if self.ca.AudioObjectHasProperty(device, ctypes.byref(main)):
            return [main]
        channels = [_Address(_MUTE, _OUTPUT, channel) for channel in _CHANNELS]
        return [
            channel for channel in channels
            if self.ca.AudioObjectHasProperty(device, ctypes.byref(channel))
        ]

    def muted(self, device: int) -> bool | None:
        """Whether every mute the device has is on; None when it has none."""
        states: list[bool] = []
        for address in self.mutes(device):
            value = ctypes.c_uint32(0)
            if self.audio_get(device, address, value) is None:
                return None
            states.append(value.value != 0)
        return all(states) if states else None

    def set_muted(self, device: int, *, muted: bool) -> bool:
        """Turn every mute the device has on or off; False when one would not."""
        value = ctypes.c_uint32(1 if muted else 0)
        codes = [
            self.ca.AudioObjectSetPropertyData(
                device, ctypes.byref(address), 0, None, ctypes.sizeof(value), ctypes.byref(value),
            )
            for address in self.mutes(device)
        ]
        return bool(codes) and all(code == 0 for code in codes)


class MacPower:
    """The real Mac, loaded on first use; answers None / False on another OS."""

    def __init__(self) -> None:
        """Nothing is loaded until a call needs it."""
        self._frameworks: _Frameworks | None = None
        self._loaded = False

    def _mac(self) -> _Frameworks | None:
        if not self._loaded:
            self._loaded = True
            if platform.system() == "Darwin":
                try:
                    self._frameworks = _Frameworks()
                except (OSError, AttributeError):
                    LOGGER.warning("night run: macOS frameworks did not load", exc_info=True)
        return self._frameworks

    def hold_awake(self, seconds: float) -> int | None:
        """Take the named keep-awake assertion for ``seconds``; its id, or None."""
        mac = self._mac()
        if mac is None:
            return None
        texts = ("PreventUserIdleSystemSleep", ASSERTION_NAME, _DETAILS, "TimeoutActionRelease")
        refs: list[int] = []
        try:
            refs.extend(mac.cfstring(text) for text in texts)
            assertion = ctypes.c_uint32(0)
            code = mac.iokit.IOPMAssertionCreateWithDescription(
                refs[0], refs[1], refs[2], None, None,
                ctypes.c_double(max(1.0, seconds)), refs[3], ctypes.byref(assertion),
            )
        except (OSError, ctypes.ArgumentError):
            LOGGER.warning("night run: could not take the assertion", exc_info=True)
            return None
        finally:
            for ref in refs:
                mac.cf.CFRelease(ref)
        if code != 0:
            LOGGER.warning("night run: IOPMAssertionCreateWithDescription returned %#x", code)
            return None
        return int(assertion.value)

    def release(self, assertion: int) -> bool:
        """Let go of the assertion; False when powerd already had (its timeout)."""
        mac = self._mac()
        if mac is None:
            return False
        code = int(mac.iokit.IOPMAssertionRelease(assertion))
        if code != 0:
            LOGGER.info("night run: IOPMAssertionRelease(%d) returned %#x", assertion, code)
        return code == 0

    def sleep_display(self) -> bool:
        """Put every display to sleep now; the Mac itself stays up."""
        if self._mac() is None:
            return False
        return _run(["/usr/bin/pmset", "displaysleepnow"]) is not None

    def brightness(self) -> float | None:
        """The built-in panel's brightness, 0-1; None without one."""
        mac = self._mac()
        if mac is None or mac.ds is None:
            return None
        display = mac.builtin_display()
        if display is None:
            return None
        level = ctypes.c_float(0.0)
        if mac.ds.DisplayServicesGetBrightness(display, ctypes.byref(level)) != 0:
            return None
        return max(0.0, min(1.0, float(level.value)))

    def set_brightness(self, level: float) -> bool:
        """Set the built-in panel's brightness, 0-1."""
        mac = self._mac()
        if mac is None or mac.ds is None:
            return False
        display = mac.builtin_display()
        if display is None:
            return False
        value = ctypes.c_float(max(0.0, min(1.0, level)))
        return int(mac.ds.DisplayServicesSetBrightness(display, value)) == 0

    def outputs(self) -> dict[str, bool | None] | None:
        """The devices the default output plays on, by UID: muted or not, None for no mute.

        An aggregate (a Multi-Output Device) has no mute of its own, so its
        active sub-devices stand in for it. None when CoreAudio does not answer.
        """
        mac = self._mac()
        if mac is None:
            return None
        default = ctypes.c_uint32(0)
        where = _Address(_DEFAULT_OUTPUT, _GLOBAL, _MAIN)
        if mac.audio_get(_AUDIO_SYSTEM, where, default) is None:
            return None
        found: dict[str, bool | None] = {}
        for device in mac.audio_ids(default.value, _SUB_DEVICES) or [default.value]:
            uid = mac.audio_uid(device)
            if uid is not None and mac.plays(device):
                found[uid] = mac.muted(device)
        return found

    def is_muted(self, uid: str) -> bool | None:
        """Whether the device with ``uid`` is muted now; None when it is gone."""
        mac = self._mac()
        if mac is None:
            return None
        device = mac.device(uid)
        return None if device is None else mac.muted(device)

    def set_muted(self, uid: str, *, muted: bool) -> bool:
        """Mute or unmute the device with ``uid``, the default output or not."""
        mac = self._mac()
        if mac is None:
            return False
        device = mac.device(uid)
        return device is not None and mac.set_muted(device, muted=muted)

    def presence(self) -> Presence | None:
        """Lock state and input idle time of this login session; None outside one."""
        mac = self._mac()
        if mac is None:
            return None
        session: int | None = mac.cg.CGSessionCopyCurrentDictionary()
        if not session:
            return None
        try:
            locked = mac.flag(session, "CGSSessionScreenIsLocked")
            on_console = mac.flag(session, "kCGSSessionOnConsoleKey")
        finally:
            mac.cf.CFRelease(session)
        idle = mac.cg.CGEventSourceSecondsSinceLastEventType(_HID_SYSTEM_STATE, _ANY_INPUT_EVENT)
        return Presence(locked=bool(locked) or on_console is False, idle_s=max(0.0, float(idle)))

    def battery(self) -> Battery | None:
        """Charge and power source; None on a Mac without a battery."""
        if self._mac() is None:
            return None
        out = _run(["/usr/bin/pmset", "-g", "batt"])
        return None if out is None else parse_battery(out)


__all__ = [
    "ASSERTION_NAME",
    "Battery",
    "MacPower",
    "Presence",
    "parse_battery",
]
