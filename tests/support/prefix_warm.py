"""A runtime with prefix warming, and checks that a warm request is a prefix of the turn's."""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from jarvis.decision import PREFIX_WARM_MAX_OUTPUT_TOKENS
from jarvis.state.memory_db import MemorySettings, append_record
from tests.support.routine import logged_payloads
from tests.support.spoken import SpokenPeer, spoken_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime

_WARM_THREAD = "jarvis-prefix-warm"  # the name ``warm_next_prefix`` gives the request's thread


@contextlib.contextmanager
def warm_runtime(
    tmp_path: Path, url: str, *, warm: bool,
) -> Iterator[tuple[JarvisRuntime, MemorySettings]]:
    """A runtime that warms (or not) against the peer at ``url``; leave it before the peer closes.

    A warm is a request in a thread of its own, started as a turn ends, that nothing joins. A
    test that ends with one still on its way would see it reach the provider in whichever test
    runs next on the process, and a stream stub that test installs would catch it. So leaving
    the block waits every warm out, and the peer must hold an answer for each warm the last turn
    starts.
    """
    runtime = spoken_runtime(tmp_path, url)
    memory = MemorySettings.from_config({}, runtime_root=tmp_path)
    for index in range(3):
        append_record(memory.db_path, record_id=f"a{index}", source="allen", text=f"问题{index}")
        append_record(memory.db_path, record_id=f"j{index}", source="jarvis", text=f"回答{index}")
    runtime = replace(
        runtime,
        memory=memory,
        system_prompt="You are Jarvis. " * 40,
        response_flags=replace(runtime.response_flags, prefix_warm=warm),
    )
    try:
        yield runtime, memory
    finally:
        for thread in threading.enumerate():
            if thread.name == _WARM_THREAD:
                thread.join(10)
                assert not thread.is_alive(), "a prefix warm is still running"


def warm_costs(runtime: JarvisRuntime) -> int:
    """How many prefix-warm requests have had their cost recorded: one row each, once it is done."""
    kinds = [payload["kind"] for payload in logged_payloads(runtime.conn, "cost.recorded")]
    return kinds.count("prefix_warm")


def wait_for_requests(peer: SpokenPeer, count: int) -> None:
    deadline = time.monotonic() + 10
    while len(peer.requests) < count:
        assert time.monotonic() < deadline, f"{len(peer.requests)} requests, wanted {count}"
        time.sleep(0.01)


def assert_prefix_of(warm: dict[str, Any], turn: dict[str, Any]) -> None:
    assert warm["max_output_tokens"] == PREFIX_WARM_MAX_OUTPUT_TOKENS
    for key in ("model", "instructions", "tools", "store"):
        assert warm[key] == turn[key], key
    assert turn["input"][: len(warm["input"])] == warm["input"]
    assert len(turn["input"]) > len(warm["input"])
