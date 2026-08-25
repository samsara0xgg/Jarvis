"""ADR-0009 Step 0 spike: ctypes-IOKit power callbacks in a plain process.

Proves the D3 primary candidate: ``IORegisterForSystemPower`` via ctypes
against IOKit + CoreFoundation, with the CFRunLoop on a dedicated
background thread (the exact architecture ``sleep_wake.py`` will use in
Step 3 — no NSApplication, no main-thread runloop, no PyObjC).

Usage::

    python scripts/spike_power_observer.py --log /tmp/spike.log --duration 240 &
    pmset sleepnow          # then wake the machine (or a scheduled wake)
    cat /tmp/spike.log      # expect will_sleep + has_powered_on lines

The log is append-only, one line per callback:
``<monotonic> <wall-iso> <message-name> arg=<notification-id>``.

Before-sleep protocol matched to D3: on ``kIOMessageSystemWillSleep`` and
``kIOMessageCanSystemSleep`` the callback calls ``IOAllowPowerChange``
unconditionally (never veto). The Python callback wrapper is held in a
module global for the process lifetime — GC while registered crashes the
runloop thread (the hazard D3 pins with a unit test in Step 3).
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import threading
import time
from pathlib import Path

# --- IOKit / CoreFoundation surface (the ~7 calls D3 enumerates) -----------

_IOKIT = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
_CF = ctypes.CDLL(
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)

# void (*IOServiceInterestCallback)(void *refcon, io_service_t service,
#                                   natural_t messageType, void *messageArgument)
_CALLBACK_TYPE = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p
)

# io_connect_t IORegisterForSystemPower(void *refcon,
#     IONotificationPortRef *thePortRef, IOServiceInterestCallback callback,
#     io_object_t *notifier)
_IOKIT.IORegisterForSystemPower.restype = ctypes.c_uint32
_IOKIT.IORegisterForSystemPower.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p),
    _CALLBACK_TYPE,
    ctypes.POINTER(ctypes.c_uint32),
]
_IOKIT.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
_IOKIT.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
_IOKIT.IOAllowPowerChange.restype = ctypes.c_int
_IOKIT.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_ssize_t]
_IOKIT.IODeregisterForSystemPower.restype = ctypes.c_int
_IOKIT.IODeregisterForSystemPower.argtypes = [ctypes.POINTER(ctypes.c_uint32)]
_IOKIT.IONotificationPortDestroy.restype = None
_IOKIT.IONotificationPortDestroy.argtypes = [ctypes.c_void_p]
_IOKIT.IOServiceClose.restype = ctypes.c_int
_IOKIT.IOServiceClose.argtypes = [ctypes.c_uint32]

_CF.CFRunLoopGetCurrent.restype = ctypes.c_void_p
_CF.CFRunLoopGetCurrent.argtypes = []
_CF.CFRunLoopAddSource.restype = None
_CF.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_CF.CFRunLoopRun.restype = None
_CF.CFRunLoopRun.argtypes = []
_CF.CFRunLoopStop.restype = None
_CF.CFRunLoopStop.argtypes = [ctypes.c_void_p]

_KCF_RUNLOOP_DEFAULT_MODE = ctypes.c_void_p.in_dll(_CF, "kCFRunLoopDefaultMode")

# IOMessage.h power codes (iokit_common_msg base 0xE0000000).
_MESSAGE_NAMES = {
    0xE0000270: "can_system_sleep",
    0xE0000280: "will_sleep",
    0xE0000290: "will_not_sleep",
    0xE0000300: "has_powered_on",
    0xE0000320: "will_power_on",
}
_ACK_MESSAGES = {0xE0000270, 0xE0000280}


class _Spike:
    """Holds registration state + the callback ref for the process lifetime."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.root_port = ctypes.c_uint32(0)
        self.notify_port = ctypes.c_void_p(None)
        self.notifier = ctypes.c_uint32(0)
        self.runloop_ref = ctypes.c_void_p(None)
        self.runloop_ready = threading.Event()
        # Kept referenced forever — see module docstring.
        self.callback = _CALLBACK_TYPE(self._on_power_message)

    def log(self, line: str) -> None:
        """Append one timestamped line (monotonic + wall clock)."""
        wall = datetime.datetime.now(tz=datetime.UTC)
        stamp = f"{time.monotonic():.3f} {wall.isoformat()}"
        with self.log_path.open("a") as fh:
            fh.write(f"{stamp} {line}\n")
            fh.flush()

    def _on_power_message(
        self,
        _refcon: int | None,
        _service: int,
        message_type: int,
        message_argument: int | None,
    ) -> None:
        name = _MESSAGE_NAMES.get(message_type, f"unknown_0x{message_type:x}")
        self.log(f"{name} arg={message_argument}")
        if message_type in _ACK_MESSAGES:
            rc = _IOKIT.IOAllowPowerChange(
                self.root_port.value, message_argument or 0
            )
            self.log(f"allow_power_change rc={rc}")

    def runloop_thread(self) -> None:
        self.runloop_ref = ctypes.c_void_p(_CF.CFRunLoopGetCurrent())
        source = _IOKIT.IONotificationPortGetRunLoopSource(self.notify_port)
        _CF.CFRunLoopAddSource(self.runloop_ref, source, _KCF_RUNLOOP_DEFAULT_MODE)
        self.runloop_ready.set()
        self.log("runloop_entering")
        _CF.CFRunLoopRun()
        self.log("runloop_exited")

    def register(self) -> bool:
        self.root_port = ctypes.c_uint32(
            _IOKIT.IORegisterForSystemPower(
                None,
                ctypes.byref(self.notify_port),
                self.callback,
                ctypes.byref(self.notifier),
            )
        )
        return self.root_port.value != 0

    def shutdown(self) -> None:
        _IOKIT.IODeregisterForSystemPower(ctypes.byref(self.notifier))
        _IOKIT.IOServiceClose(self.root_port.value)
        if self.runloop_ref.value:
            _CF.CFRunLoopStop(self.runloop_ref)
        _IOKIT.IONotificationPortDestroy(self.notify_port)
        self.log("shutdown_complete")


def main() -> int:
    """Register, spin the runloop thread for --duration seconds, tear down."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=240.0)
    args = parser.parse_args()

    spike = _Spike(args.log)
    if not spike.register():
        spike.log("register_failed root_port=0")
        return 1
    spike.log(f"registered root_port={spike.root_port.value}")

    thread = threading.Thread(target=spike.runloop_thread, daemon=True)
    thread.start()
    if not spike.runloop_ready.wait(timeout=5):
        spike.log("runloop_thread_never_ready")
        return 1

    deadline = time.monotonic() + args.duration
    while time.monotonic() < deadline:
        time.sleep(1)

    spike.shutdown()
    thread.join(timeout=5)
    spike.log(f"exit thread_alive={thread.is_alive()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
