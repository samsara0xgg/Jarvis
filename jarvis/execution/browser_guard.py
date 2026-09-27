"""L4 — in a driven browser the payment step and sign-ins stay Allen's (ADR 0059).

A server whose tools include ``browser_snapshot`` is a Playwright-style
browser. Before every call that changes the page, and before a search of it,
the guard takes a fresh snapshot of its own. It refuses the call when:

- the target is not a ref the snapshot shows (a selector could name any element);
- the target, or a control it sits inside, commits money;
- the call types into a sign-in, verification or card field;
- the page shows a card field;
- a key would press or submit something (only movement keys pass);
- a dialog other than an alert would be accepted.

A result that carries a filled sign-in, verification or card field is withheld
whole, and a search runs only on a page holding none.

Layer boundary (`.importlinter`): stdlib and this layer's own modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import ToolError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

SNAPSHOT_TOOL: Final = "browser_snapshot"

ACTING: Final = frozenset(
    {
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_select_option",
        "browser_fill_form",
        "browser_drag",
        "browser_drop",
        "browser_handle_dialog",
    }
)
"""Calls that change the page; reading, waiting and navigating are not guarded."""

LOOKING: Final = ACTING | {"browser_find"}
"""Calls the guard takes its own snapshot for first."""

_TYPING: Final = frozenset({"browser_type", "browser_fill_form"})
_MOVE_KEYS: Final = frozenset(
    {
        "arrowup", "arrowdown", "arrowleft", "arrowright", "tab", "shift+tab", "escape",
        "pageup", "pagedown", "home", "end", "backspace", "delete",
    }
)  # fmt: skip
_FIELDS: Final = frozenset({"textbox", "searchbox", "spinbutton", "combobox"})
_CONTROLS: Final = frozenset({"button", "link", "menuitem", "option", "tab", "checkbox", "radio"})

# ponytail: word lists, not a classifier. A money control or a card field named
# outside them gets through; Allen watching the window is the backstop.
_MONEY: Final = re.compile(
    r"\b(?:place (?:your )?order|buy now|pay(?: now)?|purchase|submit order|book and pay"
    r"|complete (?:your )?(?:booking|purchase|order|reservation|payment)"
    r"|confirm (?:and pay|booking|purchase|order|reservation|payment))\b"
    r"|支付|付款|下单|购买|提交订单|确认订单|确认预订",
    re.IGNORECASE,
)
_CARD: Final = re.compile(
    r"card ?number|credit card|debit card|cvv|cvc|security code|卡号|安全码", re.IGNORECASE
)
_SECRET: Final = re.compile(
    rf"{_CARD.pattern}|password|passcode|\bpin\b|one[- ]time|verification code|\botp\b"
    r"|密码|验证码",
    re.IGNORECASE,
)
_REF: Final = re.compile(r"(?:f\d+)?e\d+")
# An element line: role, optional quoted name (escapes allowed), attributes, then its
# own ref. Text after the ref (a value, or page text) can never supply the ref.
_LINE: Final = re.compile(
    r"""^(\s*)- '?([\w-]+)(?: "((?:[^"\\\n]|\\.)*)")?(?: \[[^\]\n]*\])*? """
    r"""\[ref=((?:f\d+)?e\d+)\](?: \[[^\]\n]*\])*:?(.*)$"""
)

_PAYMENT_STEP: Final = (
    "Refused: this is the payment step, which is Allen's. Stop here and tell him the "
    "order is ready for him to pay in the Jarvis browser window."
)
_SIGN_IN: Final = (
    "Refused: sign-in, verification and card fields are Allen's. Ask him to fill it in "
    "the Jarvis browser window, then continue."
)
_TYPED_BY_ALLEN: Final = (
    "Refused: the page holds a password, code or card number Allen typed. Wait until he "
    "has finished, then read the page again."
)
_NOT_A_REF: Final = (
    "Refused: target must be a ref from the latest browser_snapshot (such as e12), not a selector."
)


@dataclass(frozen=True)
class _Node:
    index: int
    indent: int
    role: str
    name: str
    value: str


def _nodes(lines: list[str]) -> dict[str, _Node]:
    """Ref -> the element line that owns it."""
    nodes: dict[str, _Node] = {}
    for i, line in enumerate(lines):
        if m := _LINE.match(line):
            value = m[5].strip().rstrip("'").strip()
            nodes.setdefault(m[4], _Node(i, len(m[1]), m[2], m[3] or "", value))
    return nodes


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _children(lines: list[str], node: _Node) -> list[str]:
    end = next(
        (j for j in range(node.index + 1, len(lines)) if _indent(lines[j]) <= node.indent),
        len(lines),
    )
    return lines[node.index + 1 : end]


def _ancestors(lines: list[str], node: _Node) -> list[str]:
    found, depth = [], node.indent
    for line in reversed(lines[: node.index]):
        if line.strip() and _indent(line) < depth:
            found.append(line)
            depth = _indent(line)
    return found


def _is_secret(node: _Node) -> bool:
    return node.role in _FIELDS and bool(_SECRET.search(node.name))


def _holds_secret(text: str) -> bool:
    """A sign-in, verification or card field with a value, on its line or a child line."""
    lines = text.splitlines()
    for node in _nodes(lines).values():
        if not _is_secret(node):
            continue
        filled = [c for c in _children(lines, node) if not c.lstrip().startswith("- /")]
        if node.value or filled:
            return True
    return False


def _money_control(line: str) -> bool:
    m = _LINE.match(line)
    return bool(m and m[2] in _CONTROLS and _MONEY.search(line))


def _targets(tool: str, args: Mapping[str, Any]) -> list[str]:
    if tool == "browser_fill_form":
        return [str(f.get("target", "")) for f in args.get("fields") or []]
    keys = ("startTarget", "endTarget") if tool == "browser_drag" else ("target",)
    return [str(args.get(k, "")) for k in keys if k in args]


def _target_refusal(tool: str, args: Mapping[str, Any], lines: list[str]) -> str | None:
    nodes = _nodes(lines)
    for target in _targets(tool, args):
        if not _REF.fullmatch(target):
            return _NOT_A_REF
        node = nodes.get(target)
        if node is None:
            return f"Refused: {target} is not on the current page; take a new browser_snapshot."
        block = "\n".join([lines[node.index], *_children(lines, node)])
        # A label types into its field, so the whole block counts, not only the role.
        if tool in _TYPING and (_is_secret(node) or _SECRET.search(block)):
            return _SIGN_IN
        if _MONEY.search(block) or any(map(_money_control, _ancestors(lines, node))):
            return _PAYMENT_STEP
    return None


def _argument_refusal(tool: str, args: Mapping[str, Any], snapshot: str) -> str | None:
    """Refusals the call's own arguments settle, whatever the page shows."""
    url = str(args.get("url") or "")
    reason = None
    if tool == SNAPSHOT_TOOL and args.get("filename"):
        reason = "Refused: a snapshot is read in the response, never saved to a file."
    elif url and not url.lower().startswith(("http://", "https://")):
        reason = "Refused: the browser opens http(s) pages only."
    elif tool == "browser_press_key" and str(args.get("key", "")).lower() not in _MOVE_KEYS:
        reason = (
            "Refused: only movement keys (arrows, Tab, Escape, Home, End, Page Up/Down, "
            "Backspace, Delete). Type text with browser_type; press a button with browser_click."
        )
    elif tool == "browser_type" and args.get("submit"):
        reason = "Refused: submit presses Enter, which can send a form; click its button instead."
    elif tool == "browser_handle_dialog" and (
        args.get("promptText") or (args.get("accept") and '["alert" dialog' not in snapshot)
    ):
        reason = "Refused: only an alert is accepted here; dismiss this dialog or ask Allen."
    return reason


def refusal(tool: str, args: Mapping[str, Any], snapshot: str) -> str | None:  # noqa: PLR0911 — one early return per refusal reason.
    """Why ``tool`` may not run on the page ``snapshot`` shows, or None when it may."""
    if reason := _argument_refusal(tool, args, snapshot):
        return reason
    if tool not in LOOKING or tool == "browser_handle_dialog":
        return None
    if _holds_secret(snapshot):
        return _TYPED_BY_ALLEN
    if tool == "browser_find":
        return None
    lines = snapshot.splitlines()
    nodes = _nodes(lines).values()
    if any(n.role in _FIELDS and _CARD.search(n.name) for n in nodes):
        return _PAYMENT_STEP
    if not nodes:
        return "Refused: the page could not be read; take a browser_snapshot first."
    return _target_refusal(tool, args, lines)


def guarded(
    tool: str, call: Callable[[Mapping[str, Any]], dict[str, Any]], snapshot: Callable[[], str]
) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """``call`` behind the guard: refused before it reaches the page, withheld after."""

    def run(args: Mapping[str, Any]) -> dict[str, Any]:
        reason = refusal(tool, args, snapshot() if tool in LOOKING else "")
        if reason:
            raise ToolError(reason, code="allens_step")
        result = call(args)
        if any(isinstance(v, str) and _holds_secret(v) for v in result.values()):
            raise ToolError(_TYPED_BY_ALLEN, code="allens_step")
        return result

    return run
