"""L3 guard that gives full and streamed provider calls one disposition."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from jarvis.shared.pricing import compute_cost_usd
from jarvis.shared.realtime import (
    CostAccountingDisposition,
    CostAccountingOutcome,
    LLMRequestOutcome,
    LLMUsageStatus,
)
from jarvis.state.cost_accounting import record_cost_disposition_once

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator, Mapping

    from jarvis.decision.llm import ChatResult, ChatStreamChunk, LLMClient
    from jarvis.state.committed_event_bus import CommittedEventBus


class MissingLLMRequestIdentityError(RuntimeError):
    """A provider request escaped without its pre-I/O stable identity."""


class CostRecorder:
    """L3 owner for completion/cancel/error accounting disposition."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        pricing_table: Mapping[str, Mapping[str, float]] | None = None,
        committed_event_bus: CommittedEventBus | None = None,
    ) -> None:
        """Bind one Event Log connection and optional pricing snapshot."""
        self._conn = conn
        self._pricing_table = {} if pricing_table is None else dict(pricing_table)
        self._committed_event_bus = committed_event_bus
        self._last_outcome: CostAccountingOutcome | None = None

    @property
    def last_outcome(self) -> CostAccountingOutcome | None:
        """Return the latest committed or replayed accounting result."""
        return self._last_outcome

    def _correlation(
        self,
        *,
        turn_id: str | None,
        run_id: str | None,
    ) -> dict[str, str] | None:
        correlation: dict[str, str] = {}
        if turn_id is not None:
            correlation["turn_id"] = turn_id
        if run_id is not None:
            correlation["run_id"] = run_id
        return correlation or None

    def _commit(
        self,
        disposition: CostAccountingDisposition,
        *,
        turn_id: str | None,
        run_id: str | None,
    ) -> CostAccountingOutcome:
        outcome = record_cost_disposition_once(
            self._conn,
            disposition,
            correlation=self._correlation(turn_id=turn_id, run_id=run_id),
            committed_event_bus=self._committed_event_bus,
        )
        self._last_outcome = outcome
        return outcome

    def _known_cost(
        self,
        *,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_write_tokens: int | None,
    ) -> float | None:
        if input_tokens is None or output_tokens is None:
            return None
        return compute_cost_usd(
            model or None,
            input_tokens,
            output_tokens,
            cache_read_tokens or 0,
            cache_write_tokens or 0,
            dict(self._pricing_table),
        )

    def _from_result(
        self,
        result: ChatResult,
        *,
        kind: str,
        client: LLMClient,
    ) -> CostAccountingDisposition:
        return CostAccountingDisposition(
            llm_request_id=result.llm_request_id,
            kind=kind,
            provider=client.provider,
            model=result.model_used or client.model,
            outcome="completed",
            usage_status=result.usage_status,
            provider_response_id=result.provider_response_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=(
                result.cache_read_in if result.usage_status != "unavailable" else None
            ),
            cache_write_tokens=(
                result.cache_write_in if result.usage_status != "unavailable" else None
            ),
            cost_usd=self._known_cost(
                model=result.model_used or client.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_in,
                cache_write_tokens=result.cache_write_in,
            ),
        )

    def _from_client(
        self,
        client: LLMClient,
        *,
        kind: str,
        outcome: LLMRequestOutcome,
        error_code: str | None = None,
    ) -> CostAccountingDisposition:
        request_id = client.last_llm_request_id
        if request_id is None:
            msg = "LLM client attempted provider I/O without llm_request_id"
            raise MissingLLMRequestIdentityError(msg)
        metadata = client.last_metadata
        response_id_raw = metadata.get("response_id")
        provider_response_id = response_id_raw if isinstance(response_id_raw, str) else None
        usage_status: LLMUsageStatus = client.last_usage_status
        if usage_status == "unavailable" and (
            client.last_input_tokens is not None or client.last_output_tokens is not None
        ):
            usage_status = "partial"
        return CostAccountingDisposition(
            llm_request_id=request_id,
            kind=kind,
            provider=client.provider,
            model=client.model,
            outcome=outcome,
            usage_status=usage_status,
            provider_response_id=provider_response_id,
            input_tokens=client.last_input_tokens,
            output_tokens=client.last_output_tokens,
            cache_read_tokens=client.last_cache_read_tokens,
            cache_write_tokens=client.last_cache_write_tokens,
            cost_usd=self._known_cost(
                model=client.model,
                input_tokens=client.last_input_tokens,
                output_tokens=client.last_output_tokens,
                cache_read_tokens=client.last_cache_read_tokens,
                cache_write_tokens=client.last_cache_write_tokens,
            ),
            error_code=error_code,
        )

    def record_chat_result(
        self,
        result: ChatResult,
        *,
        client: LLMClient,
        kind: str,
        turn_id: str | None,
        run_id: str | None = None,
    ) -> CostAccountingOutcome:
        """Record a completed full-response request idempotently."""
        return self._commit(
            self._from_result(result, kind=kind, client=client),
            turn_id=turn_id,
            run_id=run_id,
        )

    def chat(  # noqa: PLR0913 - mirrors LLMClient.chat plus audit correlation
        self,
        client: LLMClient,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
        kind: str,
        turn_id: str | None,
        run_id: str | None = None,
    ) -> ChatResult:
        """Run normal chat and commit completion or error exactly once."""
        try:
            result = client.chat(
                messages=messages,
                system=system,
                tools=tools,
                tool_choice=tool_choice,
            )
        except BaseException as exc:
            outcome: LLMRequestOutcome = (
                "cancelled"
                if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt)
                else "error"
            )
            self._commit(
                self._from_client(
                    client,
                    kind=kind,
                    outcome=outcome,
                    error_code=type(exc).__name__,
                ),
                turn_id=turn_id,
                run_id=run_id,
            )
            raise
        self.record_chat_result(
            result,
            client=client,
            kind=kind,
            turn_id=turn_id,
            run_id=run_id,
        )
        return result

    def chat_stream(  # noqa: PLR0913 - mirrors the existing adapter plus correlation
        self,
        client: LLMClient,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        kind: str,
        turn_id: str | None,
        run_id: str | None = None,
    ) -> Iterator[ChatStreamChunk]:
        """Yield the legacy stream while guaranteeing one disposition.

        A provider-final chunk is accounted before it is yielded, so a
        consumer that closes immediately after seeing the terminal cannot
        accidentally rewrite completion as cancellation.  Closing earlier
        records explicit partial/unavailable cancellation instead of zeros.
        """
        stream = client.chat_stream(messages=messages, system=system, tools=tools)
        recorded = False
        try:
            for chunk in stream:
                if chunk.is_final and not recorded:
                    self._commit(
                        self._from_client(client, kind=kind, outcome="completed"),
                        turn_id=turn_id,
                        run_id=run_id,
                    )
                    recorded = True
                yield chunk
        except GeneratorExit:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
            if not recorded:
                self._commit(
                    self._from_client(client, kind=kind, outcome="cancelled"),
                    turn_id=turn_id,
                    run_id=run_id,
                )
            raise
        except BaseException as exc:
            if not recorded:
                outcome: LLMRequestOutcome = (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                )
                self._commit(
                    self._from_client(
                        client,
                        kind=kind,
                        outcome=outcome,
                        error_code=type(exc).__name__,
                    ),
                    turn_id=turn_id,
                    run_id=run_id,
                )
            raise
        else:
            if not recorded:
                self._commit(
                    self._from_client(client, kind=kind, outcome="completed"),
                    turn_id=turn_id,
                    run_id=run_id,
                )


__all__ = ["CostRecorder", "MissingLLMRequestIdentityError"]
