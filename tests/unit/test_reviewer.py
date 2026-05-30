"""Unit tests for :mod:`jarvis.decision.reviewer` (ADR-0002 Step 9).

LLM-free: a :class:`FakeLLMClient` mocks :class:`jarvis.decision.llm.LLMClient`
with deterministic responses. The reviewer module under test is exercised
end-to-end:

- ok / fail verdict pass-through;
- ``malformed=True`` regex fallback path on unparseable JSON;
- token-count forwarding from :class:`ChatResult` -> :class:`ReviewerVerdict`;
- empty-diff canonical "no diff produced" reason;
- fresh-context wrap: chat is called inside the contextmanager.

The fake client mirrors :meth:`LLMClient.chat`'s keyword-only signature
so the reviewer's call site stays honest (a signature drift in the real
client would break the tests' fake the same way it would break Step-12's
call site).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from jarvis.decision.llm import ChatResult
from jarvis.decision.reviewer import ReviewerVerdict, review_diff

if TYPE_CHECKING:
    from collections.abc import Iterator


def _chat_result(
    text: str,
    *,
    tokens_in: int = 10,
    tokens_out: int = 20,
    model: str = "gpt-5.5",
) -> ChatResult:
    """Build a :class:`ChatResult` with the minimum fields needed by the reviewer."""
    return ChatResult(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        raw={},
        model_used=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


class FakeLLMClient:
    """Stand-in for :class:`LLMClient` — records calls, returns a canned text."""

    def __init__(
        self,
        *,
        response_text: str = "",
        tokens_in: int = 10,
        tokens_out: int = 20,
        model: str = "gpt-5.5",
    ) -> None:
        """Configure the fake response + token counts for the next chat call."""
        self.response_text = response_text
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.model = model
        self.fresh_context_calls = 0
        self.chat_calls: list[dict[str, Any]] = []

    @contextmanager
    def fresh_context(self) -> Iterator[FakeLLMClient]:
        """Increment a counter so tests can assert the wrap was entered."""
        self.fresh_context_calls += 1
        yield self

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = "auto",
    ) -> ChatResult:
        """Record the call kwargs verbatim and return the canned response."""
        self.chat_calls.append(
            {
                "messages": messages,
                "system": system,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        return _chat_result(
            self.response_text,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            model=self.model,
        )


# --- happy-path verdicts ----------------------------------------------------


def test_review_ok_verdict_passes_through() -> None:
    """A strict ``{"verdict":"ok"}`` response yields ``verdict='ok'`` directly."""
    client = FakeLLMClient(
        response_text='{"verdict": "ok", "reasons": ["matches goal"]}'
    )
    verdict = review_diff(
        task_goal="implement X", diff_text="<diff>", llm_client=client
    )
    assert isinstance(verdict, ReviewerVerdict)
    assert verdict.verdict == "ok"
    assert verdict.reasons == ("matches goal",)
    assert verdict.malformed is False
    assert client.fresh_context_calls == 1
    assert len(client.chat_calls) == 1


def test_review_fail_verdict_passes_through() -> None:
    """A strict ``{"verdict":"fail"}`` response yields ``verdict='fail'``."""
    client = FakeLLMClient(
        response_text='{"verdict": "fail", "reasons": ["unrelated change"]}'
    )
    verdict = review_diff(
        task_goal="implement X", diff_text="<diff>", llm_client=client
    )
    assert verdict.verdict == "fail"
    assert verdict.reasons == ("unrelated change",)
    assert verdict.malformed is False


def test_review_multiple_reasons_preserved_in_order() -> None:
    """All entries in ``reasons`` are forwarded as a tuple in input order."""
    client = FakeLLMClient(
        response_text=(
            '{"verdict": "fail", "reasons": ["wrong file", "missing test", "no docstring"]}'
        )
    )
    verdict = review_diff(task_goal="g", diff_text="d", llm_client=client)
    assert verdict.reasons == ("wrong file", "missing test", "no docstring")


# --- malformed-JSON fallback ------------------------------------------------


def test_review_malformed_json_falls_back_to_fail() -> None:
    """Non-JSON text triggers the regex fallback; default verdict is ``fail``."""
    client = FakeLLMClient(response_text="not even close to JSON")
    verdict = review_diff(task_goal="goal", diff_text="diff", llm_client=client)
    assert verdict.malformed is True
    assert verdict.verdict == "fail"
    assert "malformed" in verdict.reasons[0].lower()


def test_review_malformed_json_with_embedded_ok_recovers_ok() -> None:
    """Regex fallback recovers ``ok`` when noise contains the verdict literal."""
    client = FakeLLMClient(
        response_text='here is some prose then {"verdict": "ok"} blah blah'
    )
    verdict = review_diff(task_goal="g", diff_text="d", llm_client=client)
    assert verdict.malformed is True
    assert verdict.verdict == "ok"
    assert "regex" in verdict.reasons[0].lower()


def test_review_invalid_verdict_value_falls_back() -> None:
    """A verdict outside the (ok, fail) set is treated as malformed."""
    client = FakeLLMClient(response_text='{"verdict": "maybe", "reasons": []}')
    verdict = review_diff(task_goal="g", diff_text="d", llm_client=client)
    assert verdict.malformed is True
    assert verdict.verdict == "fail"


# --- token forwarding -------------------------------------------------------


def test_review_token_counts_forwarded() -> None:
    """ChatResult.tokens_in/out land on ReviewerVerdict for the Step-12 cost emit."""
    client = FakeLLMClient(
        response_text='{"verdict": "ok", "reasons": []}',
        tokens_in=137,
        tokens_out=42,
    )
    verdict = review_diff(task_goal="g", diff_text="d", llm_client=client)
    assert verdict.tokens_in == 137
    assert verdict.tokens_out == 42
    assert verdict.model == "gpt-5.5"


def test_review_records_model_id_from_chat_result() -> None:
    """ReviewerVerdict.model reflects the actual ChatResult.model_used."""
    client = FakeLLMClient(
        response_text='{"verdict": "ok", "reasons": []}',
        model="gpt-5.5-deep",
    )
    verdict = review_diff(task_goal="g", diff_text="d", llm_client=client)
    assert verdict.model == "gpt-5.5-deep"


# --- empty diff -------------------------------------------------------------


def test_review_empty_diff_returns_fail_via_prompt() -> None:
    """An empty diff yields the canonical "no diff produced" fail reason."""
    client = FakeLLMClient(
        response_text='{"verdict": "fail", "reasons": ["no diff produced"]}'
    )
    verdict = review_diff(task_goal="g", diff_text="", llm_client=client)
    assert verdict.verdict == "fail"
    assert "no diff" in verdict.reasons[0]


# --- fresh-context wrap -----------------------------------------------------


def test_review_fresh_context_wraps_chat() -> None:
    """The chat call executes INSIDE the fresh_context with-block, not outside."""
    seen: list[tuple[str, bool]] = []

    class TracingClient:
        """Records whether each chat call happened inside fresh_context."""

        def __init__(self) -> None:
            """Start outside the contextmanager."""
            self.in_fresh = False

        @contextmanager
        def fresh_context(self) -> Iterator[TracingClient]:
            """Flip the in_fresh flag for the duration of the with-block."""
            self.in_fresh = True
            try:
                yield self
            finally:
                self.in_fresh = False

        def chat(
            self,
            *,
            messages: list[dict[str, Any]],
            system: str,
            tools: list[dict[str, Any]] | None = None,
            tool_choice: str | None = "auto",
        ) -> ChatResult:
            """Record the current in_fresh state and return a canned ok verdict."""
            del messages, system, tools, tool_choice  # signature-faithful unused
            seen.append(("chat", self.in_fresh))
            return _chat_result('{"verdict":"ok","reasons":[]}')

    review_diff(task_goal="g", diff_text="d", llm_client=TracingClient())
    assert seen == [("chat", True)]


def test_review_passes_system_prompt_and_user_message() -> None:
    """Reviewer sends the reviewer system prompt + a user message with goal + diff."""
    client = FakeLLMClient(
        response_text='{"verdict": "ok", "reasons": []}'
    )
    review_diff(
        task_goal="implement create_task tool",
        diff_text="diff --git a/foo b/foo\n+hello",
        llm_client=client,
    )
    assert len(client.chat_calls) == 1
    call = client.chat_calls[0]
    assert "code reviewer" in call["system"].lower()
    assert call["tools"] is None
    user_content = call["messages"][0]["content"]
    assert "implement create_task tool" in user_content
    assert "+hello" in user_content


def test_review_truncates_oversized_diff() -> None:
    """Diffs longer than the 50K char cap are truncated before prompt assembly."""
    client = FakeLLMClient(
        response_text='{"verdict": "ok", "reasons": []}'
    )
    huge_diff = "x" * 100_000
    review_diff(task_goal="g", diff_text=huge_diff, llm_client=client)
    user_content = client.chat_calls[0]["messages"][0]["content"]
    # The user message has goal + scaffolding + diff; the diff portion
    # must be capped, so total content stays well below the raw 100K.
    assert len(user_content) < 60_000
