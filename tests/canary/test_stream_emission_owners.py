"""Pin stream semantic, durable, and presentation owners across the layer seam."""

from __future__ import annotations

import ast

from tests.canary._helpers import iter_jarvis_py_files, parse, relative_to_repo

_OWNERS = {
    "EmissionPermit": "jarvis/decision/stream_gate.py",
    "append_stream_gate": "jarvis/decision/stream_gate.py",
    "append_permitted_segment": "jarvis/surface/stream_emission.py",
}


def test_permit_construction_and_consumption_have_single_layer_owners() -> None:
    """L5 cannot mint a receipt and L3 cannot bypass surface admission."""
    violations: list[str] = []
    for path in iter_jarvis_py_files():
        relative = relative_to_repo(path)
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name in _OWNERS and relative != _OWNERS[name]:
                violations.append(f"{relative}:{node.lineno}: unauthorized {name} caller")
            if name not in {"emit_event", "append_event_in_transaction"}:
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "type"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "surface.response_chunk"
                    and relative
                    not in {
                        "jarvis/state/stream_emission.py",
                        "jarvis/surface/cli_render.py",
                    }
                ):
                    violations.append(  # noqa: PERF401 - keep the invariant beside its diagnosis
                        f"{relative}:{node.lineno}: bypasses chunk admission",
                    )
    assert not violations, "\n".join(violations)
