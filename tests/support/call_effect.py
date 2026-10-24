"""A tool whose calls differ in effect, and a decision context over it (ADR 0361)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import DecideContext, LifecycleLike, RuntimePathsLike, ToolRegistryLike
from jarvis.decision.confirm_grammar import load_confirm_grammar
from jarvis.execution.tools import (
    ActionLifecycle,
    Receipted,
    Tool,
    ToolError,
    build_default_registry,
)
from jarvis.shared import CallerPrincipal
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.shared.receipts import Receipt
from tests.support.consent import REPO
from tests.support.plugin_approval import Paths

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.decision.llm import LLMClient
    from jarvis.execution.tools import ToolContext
    from jarvis.shared import RiskLevel
    from tests.support.model import ScriptedModel

_LEVELS: dict[str, RiskLevel] = {"look": "L0", "note": "L1", "send": "L3"}
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(_LEVELS)},
        "to": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": False,
}
PEOPLE = {"Sam": "Sam Lee <sam@example.com>"}
UNDO = "evt-note-1"


def effect(args: Mapping[str, Any]) -> RiskLevel:
    """``look`` reads, ``note`` changes his own things, ``send`` speaks for him."""
    return _LEVELS[str(args.get("action", "look"))]


def complete(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
    """A name becomes the person's address; two Pats need him; nobody is impossible; Boom breaks."""
    name = str(args.get("to", ""))
    if name == "Boom":
        msg = "the address book fell over"
        raise RuntimeError(msg)
    if name == "Pat":
        msg = "two people are called Pat: Pat Kim (last mail 3 May), Pat Lo (last mail 9 Jun)"
        raise ToolError(msg, code="needs_choice_of_person", kind="needs_him")
    if name in PEOPLE:
        return {**args, "to": PEOPLE[name]}
    if "@" not in name:
        msg = f"nobody called {name} in his mail; ask him for the address"
        raise ToolError(msg, code="no_such_person", kind="impossible")
    return dict(args)


def _run(args: Mapping[str, Any], _ctx: ToolContext) -> Receipted:
    action = str(args["action"])
    answer = {"summary": f"{action} done", "to": args.get("to", "")}
    if action == "look":
        return Receipted(answer, Receipt("done", reads_only=True, line="looked"))
    if action == "note":
        return Receipted(
            answer, Receipt("done", reads_only=False, line="noted", object="note:n1", undo=UNDO),
        )
    return Receipted(answer, Receipt("done", reads_only=False, line="sent", object="mail:m1"))


def depot() -> Tool:
    """The tool: highest level L3, each call's level from ``effect``."""
    return Tool(
        name="depot",
        description="Look, note or send.",
        input_schema=SCHEMA,
        handler=_run,
        allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
        risk_level="L3",
        read_only=False,
        effect=effect,
        complete=complete,
    )


def context(tmp_path: Path, llm: ScriptedModel) -> DecideContext:
    """A real ``decide()`` context over a registry holding ``depot`` and a fresh log."""
    from jarvis.state.event_log import open_event_log  # noqa: PLC0415 — only this builder needs it.

    registry = build_default_registry(confirmation_dispatch_outbox=True)
    registry.register(depot())
    paths = Paths(event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts")
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    return DecideContext(
        conn=open_event_log(paths.event_log),
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
        confirm_grammar_table=load_confirm_grammar(REPO / "config" / "confirm_grammar.yaml"),
        wave1_features=Wave1FeatureFlags(confirmation_dispatch_outbox=True),
    )
