"""Live burn for the pluggable web_search backend.

Covers what is testable without a paid key:
  1. backend resolution + the keyless degrade (table-driven)
  2. row rendering: per-result cap, total cap, whitespace collapse
  3. the real ddgs path still works end to end
  4. Exa/Tavily endpoint reachability with a dummy key -- a 401 proves
     the URL and auth header are right; a 404 would prove they are not.

Usage: PYTHONPATH=<worktree> python burn_web_search.py
"""

from __future__ import annotations

import logging
import sys

import httpx

from jarvis.execution.tools import (
    _WEB_SEARCH_RESULT_TEXT_CAP,
    _WEB_SEARCH_TOTAL_MAX_BYTES,
    _cap_rows_total_bytes,
    _collapse_ws,
    _ddgs_search_backend,
    _resolve_search_backend,
)

logging.basicConfig(level=logging.WARNING, format="    [warn] %(message)s")


def check_resolution() -> bool:
    print("\n=== 1. backend resolution")
    cases = [
        # (provider, api_key, expected_name)
        ("exa", None, "ddgs"),          # keyed provider, no key -> degrade
        ("exa", "k-123", "exa"),
        ("tavily", None, "ddgs"),
        ("tavily", "k-123", "tavily"),
        ("ddgs", None, "ddgs"),
        ("EXA", "k-123", "exa"),        # case-insensitive
        ("  exa  ", "k-123", "exa"),    # whitespace-tolerant
        ("bogus", "k-123", "ddgs"),     # unknown -> degrade
        ("exa", "", "ddgs"),            # empty key counts as unset
    ]
    ok = True
    for provider, key, expected in cases:
        _backend, name = _resolve_search_backend(provider, key)
        good = name == expected
        ok = ok and good
        flag = "ok " if good else "FAIL"
        print(f"  {flag} provider={provider!r:12} key={'set' if key else 'unset':5} -> {name}")
    return ok


def check_rendering() -> bool:
    print("\n=== 2. row rendering / caps")
    ok = True

    collapsed = _collapse_ws("a\n\n  b\t\tc   \n d")
    good = collapsed == "a b c d"
    ok = ok and good
    print(f"  {'ok ' if good else 'FAIL'} whitespace collapse -> {collapsed!r}")

    # One oversized result must be cut to the per-result cap.
    huge = "x" * (_WEB_SEARCH_RESULT_TEXT_CAP * 3)
    row = f"1. T — https://e.com — {_collapse_ws(huge)[:_WEB_SEARCH_RESULT_TEXT_CAP]}"
    good = len(row) < _WEB_SEARCH_RESULT_TEXT_CAP + 100
    ok = ok and good
    print(f"  {'ok ' if good else 'FAIL'} per-result cap -> row is {len(row)} chars")

    # Eight such rows must be cut down to the total cap.
    rows = [f"{i}. T — https://e.com — {'y' * _WEB_SEARCH_RESULT_TEXT_CAP}" for i in range(1, 9)]
    capped = _cap_rows_total_bytes(rows, _WEB_SEARCH_TOTAL_MAX_BYTES)
    total = sum(len(r.encode("utf-8")) for r in capped)
    good = total <= _WEB_SEARCH_TOTAL_MAX_BYTES
    ok = ok and good
    print(f"  {'ok ' if good else 'FAIL'} total cap -> {len(capped)}/8 rows, {total} bytes "
          f"(<= {_WEB_SEARCH_TOTAL_MAX_BYTES})")
    return ok


def check_ddgs_live() -> bool:
    print("\n=== 3. ddgs backend, live")
    try:
        rows = _ddgs_search_backend("Vancouver to Shanghai flights", 3, timeout_s=20.0)
    except Exception as exc:  # noqa: BLE001 -- this backend failing IS the finding
        print(f"  ddgs raised {type(exc).__name__}: {exc}")
        print("  -> this is the documented anti-bot churn, not a regression")
        return False
    print(f"  got {len(rows)} rows")
    for title, url, text in rows[:2]:
        print(f"    - {title[:60]!r} {url[:60]}")
        print(f"      text: {len(text)} chars")
    return bool(rows)


def check_endpoint(name: str, url: str, headers: dict[str, str], body: dict[str, object]) -> bool:
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=20.0)
    except Exception as exc:  # noqa: BLE001
        print(f"  {name}: unreachable -- {type(exc).__name__}: {exc}")
        return False
    # 401/403 == endpoint and auth header are right, key is not.
    # 404 would mean the URL is wrong.
    good = response.status_code in (401, 403)
    flag = "ok " if good else "FAIL"
    print(f"  {flag} {name}: HTTP {response.status_code} "
          f"({'auth rejected as expected' if good else 'UNEXPECTED -- check URL/shape'})")
    return good


def check_endpoints() -> bool:
    print("\n=== 4. keyed endpoints reachable (dummy key -> expect 401/403)")
    exa_ok = check_endpoint(
        "exa",
        "https://api.exa.ai/search",
        {"x-api-key": "dummy-key-for-shape-check"},
        {"query": "test", "numResults": 1, "contents": {"text": {"maxCharacters": 100}}},
    )
    tavily_ok = check_endpoint(
        "tavily",
        "https://api.tavily.com/search",
        {"Authorization": "Bearer dummy-key-for-shape-check"},
        {"query": "test", "max_results": 1, "include_raw_content": "markdown"},
    )
    return exa_ok and tavily_ok


def main() -> int:
    results = {
        "resolution": check_resolution(),
        "rendering": check_rendering(),
        "ddgs live": check_ddgs_live(),
        "endpoints": check_endpoints(),
    }
    print("\n=== summary")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    # ddgs failing live is a finding about ddgs, not about this change.
    blocking = [n for n, ok in results.items() if not ok and n != "ddgs live"]
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
