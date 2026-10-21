"""The harness that calls the daily tools through the flat dispatcher."""

from __future__ import annotations

import json
from contextlib import closing
from typing import TYPE_CHECKING, Any

from jarvis.execution.tools import Tool, build_default_registry
from jarvis.state.memory_db import append_record, open_memory_db
from tests.support.tool_world import ToolWorld, tool_request

if TYPE_CHECKING:
    from pathlib import Path


class DailyHarness:
    """Use the current flat dispatcher and real default registry."""

    def __init__(
        self,
        root: Path,
        *,
        timesink_path: Path | None = None,
    ) -> None:
        """Create an isolated default-registry fixture."""
        self.memory = root / "memory.db"
        with closing(open_memory_db(self.memory)):
            pass
        self.tools = build_default_registry(
            memory_db_path=self.memory, timesink_db_path=timesink_path
        ).get_definitions()
        self.fx = ToolWorld(root, tools=self.tools)
        self.sequence = 0

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch and decode the actual persisted tool output."""
        self.sequence += 1
        result = self.fx.dispatch(tool_request(name, f"daily{self.sequence}", arguments=args))
        assert result.slots[0].tool_output is not None
        output = json.loads(result.slots[0].tool_output)
        assert output.get("truncated") is not True
        caps = {tool.name: tool.max_result_chars for tool in self.tools if isinstance(tool, Tool)}
        assert len(result.slots[0].tool_output) <= caps[name]
        return dict(output)

    def record(self, identity: str, text: str = "Use local todos") -> None:
        """Append real conversation material without a model."""
        append_record(self.memory, record_id=identity, source="allen", text=text)
