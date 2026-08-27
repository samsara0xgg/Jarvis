"""Live burn for the web_fetch extract-then-cap fix.

Exercises the REAL network path the handler uses -- `_fetch_url_backend`
(SSRF guard, redirect hops, drain cap) then the same decode / extract /
cap chain -- against real pages, including the exact Kayak URL whose
fetch returned zero body text in turn Te3815a16.

Usage: PYTHONPATH=<worktree> python burn_web_fetch.py
"""

from __future__ import annotations

import sys

from jarvis.execution.tools import (
    DEFAULT_WEB_FETCH_MAX_BYTES,
    DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    _cap_extracted_text,
    _decode_hop_body,
    _extract_readable_html,
    _fetch_url_backend,
)

URLS = [
    # The one that failed in production: 150 KB, all <head> under the old cap.
    "https://www.ca.kayak.com/flight-routes/Vancouver-Intl-YVR/Shanghai-Pu-Dong-PVG",
    # A large, text-heavy page -- should exercise the TEXT cap, not the drain cap.
    "https://en.wikipedia.org/wiki/Shanghai_Pudong_International_Airport",
    # A small page -- should trip neither cap.
    "https://example.com/",
]


def run(url: str) -> bool:
    print(f"\n=== {url}")
    outcome = _fetch_url_backend(
        url, timeout_s=20.0, max_bytes=DEFAULT_WEB_FETCH_MAX_BYTES,
    )
    if not outcome.ok:
        print(f"  FETCH FAILED: {outcome.error_code}: {outcome.error_message}")
        return False

    text, undelivered, lossy = _decode_hop_body(
        outcome.body, outcome.total_bytes, outcome.content_type,
    )
    body_truncated = undelivered > 0
    title, html_text = _extract_readable_html(text)
    capped, text_undelivered = _cap_extracted_text(
        html_text, DEFAULT_WEB_FETCH_MAX_TEXT_BYTES,
    )

    print(f"  status          {outcome.status_code}")
    print(f"  total_bytes     {outcome.total_bytes} (exact={outcome.total_bytes_exact})")
    print(f"  body drained    {len(outcome.body)} bytes (truncated={body_truncated})")
    print(f"  title           {title!r}")
    print(f"  extracted text  {len(html_text.encode('utf-8'))} bytes")
    print(f"  after text cap  {len(capped.encode('utf-8'))} bytes "
          f"(undelivered={text_undelivered}, lossy={lossy})")
    preview = " ".join(capped.split())[:220]
    print(f"  preview         {preview!r}")

    # The bug being fixed: extraction yielded NOTHING on real pages.
    # A non-2xx is a failure even when the error body extracts cleanly.
    http_ok = 200 <= (outcome.status_code or 0) < 300
    ok = http_ok and bool(capped.strip())
    if not http_ok:
        print(f"  -> HTTP {outcome.status_code}  <-- REQUEST REFUSED")
    print(f"  -> body text extracted: {'YES' if ok else 'NO  <-- STILL BROKEN'}")
    return ok


def main() -> int:
    print(f"drain cap = {DEFAULT_WEB_FETCH_MAX_BYTES} bytes")
    print(f"text  cap = {DEFAULT_WEB_FETCH_MAX_TEXT_BYTES} bytes")
    results = [run(u) for u in URLS]
    passed = sum(results)
    print(f"\n=== {passed}/{len(results)} pages yielded real body text")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
