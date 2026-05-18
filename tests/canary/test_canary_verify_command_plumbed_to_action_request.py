"""Canary - ``verify_command`` flows from Task Ledger -> ActionRequest.payload -> L4.

Per ADR-0002 § Verify_command plumbing (lines 875-913) + Step 12
build-order canary list (lines 1700-1707):

    ``test_canary_verify_command_plumbed_to_action_request`` - AST scan:
    the L3 Result Interpreter site that constructs the ``verify_diff``
    ActionRequest assigns ``payload["verify_command"] =
    task_record.verify_command`` (literal key match); ensures the Task
    Ledger projection's ``verify_command`` reaches L4 unchanged. The
    complementary L4-side read
    (``action_request.payload.get("verify_command")``) is asserted in
    the same canary.

The canary inspects two files:

* ``jarvis/decision/__init__.py`` - the L3 site that builds the
  ``verify_diff`` ActionRequest. Looks for ``payload["verify_command"]``
  (Subscript-assign form) OR ``"verify_command": ...`` (Dict-literal
  form). Either spelling satisfies the contract.
* ``jarvis/execution/tools.py`` - the L4 ``verify_diff_handler``. Looks
  for ``.payload.get("verify_command")``.

Both ends must reference the LITERAL key ``"verify_command"``; a typo
or a constant-named indirection would break the runtime plumbing
silently because the wrong key returns ``None`` and slot 2 is skipped.
"""

from __future__ import annotations

import ast

from tests.canary._helpers import parse, repo_root

_L3_PATH = "jarvis/decision/__init__.py"
_L4_PATH = "jarvis/execution/tools.py"


def _has_string_key_literal(module: ast.Module, *, key: str) -> bool:
    """True iff any Dict literal in ``module`` has the string key ``key``."""
    for node in ast.walk(module):
        if not isinstance(node, ast.Dict):
            continue
        for k in node.keys:
            if isinstance(k, ast.Constant) and k.value == key:
                return True
    return False


def _has_subscript_assign(module: ast.Module, *, key: str) -> bool:
    """True iff any ``...[ "key" ] = ...`` assignment exists in ``module``."""
    for node in ast.walk(module):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == key
            ):
                return True
    return False


def _has_payload_get_call(module: ast.Module, *, key: str) -> bool:
    """True iff any call ``X.payload.get("key")`` exists in ``module``."""
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "get":
            continue
        receiver = func.value
        if not (
            isinstance(receiver, ast.Attribute) and receiver.attr == "payload"
        ):
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and first_arg.value == key:
            return True
    return False


def test_canary_verify_command_in_l3_action_request_payload() -> None:
    """The L3 ``verify_diff`` ActionRequest site sets payload['verify_command']."""
    path = repo_root() / _L3_PATH
    module = parse(path)

    seen_in_literal = _has_string_key_literal(module, key="verify_command")
    seen_in_subscript = _has_subscript_assign(module, key="verify_command")

    assert seen_in_literal or seen_in_subscript, (
        f"{_L3_PATH}: ADR-0002 § Verify_command plumbing requires the L3 "
        "Result Interpreter site that builds the verify_diff ActionRequest "
        "to set payload['verify_command'] (either as a dict-literal key or "
        "via a subscript assignment). Neither form was found."
    )


def test_canary_verify_command_read_from_l4_handler_payload() -> None:
    """L4 ``verify_diff_handler`` reads ``action_request.payload.get('verify_command')``."""
    path = repo_root() / _L4_PATH
    module = parse(path)

    assert _has_payload_get_call(module, key="verify_command"), (
        f"{_L4_PATH}: ADR-0002 § Verify_command plumbing requires "
        "verify_diff_handler to read "
        "`action_request.payload.get('verify_command')` (literal key). "
        "Missing or typo'd key silently disables slot 2."
    )
