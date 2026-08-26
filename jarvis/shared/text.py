"""Codepoint-safe byte-truncation helper (ADR-0011 §12 D5/D6).

`read_file`, `read_clipboard`, `search_notes` (L4, `jarvis.execution.tools`)
and the Tier 0 template renderer's spoken-preview cap (L3,
`jarvis.decision.__init__`) all need the SAME "cut a blob to at most N
bytes and report exactly how much was left out" logic. A naive
``blob[:n].decode("utf-8", errors="replace")`` has two bugs: it can cut
mid-codepoint on any multibyte boundary (guaranteed on some inputs —
e.g. ``8192 % 3 == 2`` for pure-CJK content), which prints a stray
U+FFFD at the tail, and reporting ``total_bytes - n`` as the truncated
count silently ignores the bytes the boundary trim ate on top of the
cap. One helper, used everywhere a byte cap needs the shared
``…[truncated N bytes]`` marker.

`jarvis.shared` sits at the bottom of the layer DAG (`.importlinter`)
alongside `jarvis.constitution` and may be imported by any layer above,
which is what lets L3's pure Tier 0 module (`jarvis.decision.tier0`,
which otherwise imports no `jarvis.*` module) apply the same cap
without crossing the decision/execution sibling boundary — the cap
itself lives in the caller (`jarvis.decision.__init__`), not in
`tier0.py`.

Layer rules: stdlib only.
"""

from __future__ import annotations


def truncate_utf8(data: bytes, total_bytes: int) -> tuple[str, int, bool]:
    """Decode ``data`` as UTF-8, codepoint-safe, reporting the real shortfall.

    ``data`` is a byte-prefix of a larger (or equal) blob whose true
    length is ``total_bytes`` — i.e. ``data == original[:len(data)]``
    and ``total_bytes == len(original)``. Pass ``total_bytes=len(data)``
    when ``data`` is already the complete content (nothing was cut by
    the caller's own byte cap).

    Returns ``(text, undelivered_bytes, lossy)``:

    - ``text`` — the decoded prefix. Never ends mid-codepoint, so
      capping pure-CJK content never emits a trailing U+FFFD.
    - ``undelivered_bytes`` — bytes NOT represented in ``text``,
      counted from ``total_bytes`` (i.e. ``total_bytes -
      len(text.encode("utf-8"))``). This already accounts for any
      extra bytes trimmed to land on a codepoint boundary — it is NOT
      simply ``total_bytes - len(data)``.
    - ``lossy`` — True iff ``data`` contains a genuine invalid UTF-8
      byte sequence (e.g. a GBK-encoded file), as opposed to merely
      ending mid-codepoint because the caller's cap landed there. When
      True, ``text`` is a best-effort ``errors="replace"`` decode of
      ``data`` and ``undelivered_bytes`` is the caller's cap shortfall
      only (``total_bytes - len(data)``, not codepoint-exact) —
      callers must surface ``lossy`` to the caller as an explicit
      signal rather than trust the byte count to the same precision as
      the clean-cut case.
    """
    if not data:
        return "", max(0, total_bytes), False
    more_after_cut = total_bytes > len(data)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(data) and more_after_cut:
            # `data` was cut mid-codepoint by the caller's own byte cap
            # (there is more source beyond it) — not a genuine encoding
            # problem. Everything before `exc.start` already decoded
            # cleanly; trim there instead of guessing a byte count.
            valid = data[: exc.start]
            return valid.decode("utf-8"), total_bytes - len(valid), False
        lossy_text = data.decode("utf-8", errors="replace")
        return lossy_text, max(0, total_bytes - len(data)), True
    return text, max(0, total_bytes - len(data)), False


__all__ = ["truncate_utf8"]
