"""Tier 2 scenario tests — exercise the live cloud LLM end-to-end.

Per ADR 0001 § Tier 2 invocation command. Scenarios are marked with
``@pytest.mark.live_llm`` and skipped unless ``--live-llm`` is passed
on the pytest CLI (see ``conftest.py`` in this package).

Running ``pytest tests/`` does NOT execute scenarios; only ``pytest
tests/scenarios/ -v --live-llm`` enables them. The conftest also
verifies ``OPENROUTER_PROXY_KEY`` is present (non-stub) before any LLM
call.
"""
