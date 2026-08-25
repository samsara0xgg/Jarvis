"""Canary — a turn routed to a non-speaking channel never reaches TTS (ADR-0009 D4/§8).

D4's last bullet is the pin:

> **System turns are silent, enforced**: the daemon streams every turn
> (``streaming_enabled=True``) and ``_tts_watcher`` currently feeds
> **every** response chunk to TTS regardless of channel — a 3am orphan
> closure would speak.

The regression this guards is a deletion, not an addition: drop the
channel guard out of ``_tts_watcher`` (or forget to extend it when a
fourth attention channel is implemented) and the supervisor sweep's
``queue_review`` limitation is spoken aloud at 3am — the exact
violation of spec §3.2.5 安静优先 that the ADR-0002 Limitation-routing
amendment routes around.

Two static statements, neither hard-coded against today's literals:

1. **Every non-speaking channel L3 can actually return is in the
   watcher's suppression set.** The required set is DERIVED, not
   listed: the channels come from the ``AttentionChannel`` Literal in
   ``jarvis/decision/gates.py``, "speaks" comes from
   ``ATTENTION_CHANNEL_TO_SURFACES`` (``jarvis/surface/notify.py``)
   crossed with the surfaces ``jarvis/surface/cli_render.py`` maps to
   the ``voice`` physical surface. So the day someone widens the
   ``AttentionChannel`` Literal to a channel with no ``say`` in its
   surface tuple, this canary demands the watcher learn about it —
   without anyone editing this file. Vacuity guards assert each of the
   three scans found something.
2. **The channel actually reaches the watcher.** The filter reads
   ``attention_channel`` off the ``surface.response_open`` header, so
   the emitter must put it there (from a variable — a hard-coded label
   would make every turn look speakable) and the registry must accept
   it. Without this half, statement 1 could hold while the guard never
   sees a channel and every turn speaks.

Implementation is the canary house style — stdlib ``ast`` only, no
``unittest.mock``, no monkeypatching of jarvis internals.
"""

from __future__ import annotations

import ast

from jarvis.state.event_log import EventTypeRegistry
from tests.canary._helpers import parse, repo_root

_INHERENT_LOOP_REL = "jarvis/runtime/inherent_loop.py"
_CLI_RENDER_REL = "jarvis/surface/cli_render.py"
_NOTIFY_REL = "jarvis/surface/notify.py"
_GATES_REL = "jarvis/decision/gates.py"

_TTS_WATCHER_NAME = "_tts_watcher"
_ATTENTION_CHANNEL_ALIAS = "AttentionChannel"
_CHANNEL_TO_SURFACES_NAME = "ATTENTION_CHANNEL_TO_SURFACES"
_SURFACE_TO_PHYSICAL_NAME = "_L3_SURFACE_TO_PHYSICAL"

# The physical surface name that means "a speaker produces sound". Read
# out of cli_render's own surface->physical map, so ``say`` / ``say_bell``
# are never spelled here.
_VOICE_PHYSICAL = "voice"

_OPEN_EVENT_TYPE = "surface.response_open"
_CHANNEL_PAYLOAD_KEY = "attention_channel"


# --- AST plumbing -----------------------------------------------------------


def _module(rel_path: str) -> ast.Module:
    """Parse a repo-relative production module, asserting it still exists."""
    path = repo_root() / rel_path
    assert path.is_file(), (
        f"{rel_path} is missing — the ADR-0009 D4 TTS filter moved or was "
        "deleted; this canary has nothing left to pin."
    )
    return parse(path)


def _top_level_value(module: ast.Module, name: str) -> ast.expr | None:
    """Return the value expression bound to top-level ``name``, if any."""
    for node in module.body:
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                return node.value
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return node.value
    return None


def _bound_names(node: ast.stmt) -> list[str]:
    """Return the plain target names bound by one top-level assignment."""
    if isinstance(node, ast.AnnAssign):
        return [node.target.id] if isinstance(node.target, ast.Name) else []
    if isinstance(node, ast.Assign):
        return [tgt.id for tgt in node.targets if isinstance(tgt, ast.Name)]
    return []


def _bound_value(node: ast.stmt) -> ast.expr | None:
    """Return the value expression of one top-level assignment, if it is one."""
    if isinstance(node, ast.AnnAssign | ast.Assign):
        return node.value
    return None


def _str_constant_names(module: ast.Module) -> dict[str, str]:
    """Map top-level ``NAME = "literal"`` bindings to their string value."""
    out: dict[str, str] = {}
    for node in module.body:
        value = _bound_value(node)
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for name in _bound_names(node):
            out[name] = value.value
    return out


def _string_elements(value: ast.expr | None) -> tuple[str, ...]:
    """Return the literal strings inside a tuple / list / set display."""
    if not isinstance(value, ast.Tuple | ast.List | ast.Set):
        return ()
    return tuple(
        elt.value
        for elt in value.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    )


def _literal_alias_members(module: ast.Module, name: str) -> tuple[str, ...]:
    """Return the string members of a top-level ``X = Literal[...]`` alias."""
    value = _top_level_value(module, name)
    if not isinstance(value, ast.Subscript):
        return ()
    sliced = value.slice
    if isinstance(sliced, ast.Constant) and isinstance(sliced.value, str):
        return (sliced.value,)
    return _string_elements(sliced)


def _dict_of_string_tuples(module: ast.Module, name: str) -> dict[str, tuple[str, ...]]:
    """Return a top-level ``{"key": ("a", "b")}`` mapping as a plain dict."""
    value = _top_level_value(module, name)
    if not isinstance(value, ast.Dict):
        return {}
    return {
        key.value: _string_elements(val)
        for key, val in zip(value.keys, value.values, strict=True)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _dict_of_named_constants(module: ast.Module, name: str) -> dict[str, str]:
    """Return a top-level ``{"key": _SOME_CONST}`` mapping with names resolved."""
    value = _top_level_value(module, name)
    if not isinstance(value, ast.Dict):
        return {}
    constants = _str_constant_names(module)
    out: dict[str, str] = {}
    for key, val in zip(value.keys, value.values, strict=True):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            out[key.value] = val.value
        elif isinstance(val, ast.Name) and val.id in constants:
            out[key.value] = constants[val.id]
    return out


def _frozenset_constants(module: ast.Module) -> dict[str, frozenset[str]]:
    """Map every top-level ``NAME = frozenset({...})`` to its string members."""
    out: dict[str, frozenset[str]] = {}
    for node in module.body:
        value = _bound_value(node)
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        if not (isinstance(func, ast.Name) and func.id == "frozenset"):
            continue
        if not value.args:
            continue
        members = frozenset(_string_elements(value.args[0]))
        for name in _bound_names(node):
            out[name] = members
    return out


def _function_def(module: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the (async) function definition named ``name``."""
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    msg = (
        f"{name} not found in {_INHERENT_LOOP_REL} — the TTS watcher was "
        "renamed or removed; update this canary before shipping."
    )
    raise AssertionError(msg)


# --- 1. every non-speaking L3 channel is in the watcher's suppression set ----


def test_canary_system_turns_never_tts() -> None:
    """``_tts_watcher`` suppresses every attention channel that has no voice surface."""
    gates = _module(_GATES_REL)
    notify = _module(_NOTIFY_REL)
    cli_render = _module(_CLI_RENDER_REL)
    loop = _module(_INHERENT_LOOP_REL)

    channels = _literal_alias_members(gates, _ATTENTION_CHANNEL_ALIAS)
    assert channels, (
        f"{_GATES_REL}: could not read the members of the "
        f"{_ATTENTION_CHANNEL_ALIAS} Literal alias — L3's channel vocabulary "
        "moved, so this canary would derive an empty requirement and pass "
        "vacuously."
    )

    channel_surfaces = _dict_of_string_tuples(notify, _CHANNEL_TO_SURFACES_NAME)
    assert channel_surfaces, (
        f"{_NOTIFY_REL}: {_CHANNEL_TO_SURFACES_NAME} is no longer a literal "
        "dict of surface tuples; the speaks/does-not-speak derivation is blind."
    )
    unmapped = sorted(set(channels) - set(channel_surfaces))
    assert not unmapped, (
        f"{_NOTIFY_REL}: {_CHANNEL_TO_SURFACES_NAME} has no row for L3 "
        f"channel(s) {unmapped!r}. An unmapped channel has no known surfaces, "
        "so this canary cannot tell whether it speaks."
    )

    voice_surfaces = {
        surface
        for surface, physical in _dict_of_named_constants(
            cli_render, _SURFACE_TO_PHYSICAL_NAME
        ).items()
        if physical == _VOICE_PHYSICAL
    }
    assert voice_surfaces, (
        f"{_CLI_RENDER_REL}: no surface in {_SURFACE_TO_PHYSICAL_NAME} maps to "
        f"the {_VOICE_PHYSICAL!r} physical surface — the canary would then "
        "consider every channel silent and pass vacuously."
    )

    must_not_speak = {
        channel
        for channel in channels
        if not set(channel_surfaces[channel]) & voice_surfaces
    }
    assert must_not_speak, (
        "Every L3 attention channel now maps to a voice surface, which would "
        "make ADR-0009 D4 unenforceable. That is a spec §3.2.5 problem, not a "
        "canary problem — check jarvis/surface/notify.py."
    )

    watcher = _function_def(loop, _TTS_WATCHER_NAME)
    constants = _frozenset_constants(loop)
    referenced: set[str] = set()
    hits: list[str] = []
    for node in ast.walk(watcher):
        if isinstance(node, ast.Name) and node.id in constants:
            referenced |= constants[node.id]
            hits.append(node.id)
    assert hits, (
        f"{_INHERENT_LOOP_REL}: {_TTS_WATCHER_NAME} references no module-level "
        "frozenset-of-strings constant. The ADR-0009 D4 channel filter was "
        "removed or inlined beyond this canary's reach — every turn now "
        "reaches the TTS pipeline, including the 3am supervisor sweep's."
    )

    missing = sorted(must_not_speak - referenced)
    assert not missing, (
        "ADR-0009 D4: attention channel(s) "
        f"{missing!r} route to no voice surface, yet "
        f"{_TTS_WATCHER_NAME} does not suppress them "
        f"(it filters on {sorted(referenced)!r} via {sorted(set(hits))!r}).\n"
        "A turn on such a channel would be spoken aloud — spec §3.2.5 安静优先, "
        "and the ADR-0002 Limitation-routing amendment exists precisely so "
        "these do NOT interrupt."
    )


# --- 2. the channel actually reaches the watcher ----------------------------


def test_canary_response_open_carries_the_channel_the_filter_reads() -> None:
    """The open header carries a plumbed ``attention_channel`` the registry accepts."""
    schema = EventTypeRegistry.get(_OPEN_EVENT_TYPE)
    assert schema is not None, (
        f"{_OPEN_EVENT_TYPE} is unregistered — ADR-0009 §4 amends this entry."
    )
    accepted = set(schema.required_payload) | set(schema.optional_payload)
    assert _CHANNEL_PAYLOAD_KEY in accepted, (
        f"ADR-0009 §4: {_OPEN_EVENT_TYPE} no longer accepts "
        f"{_CHANNEL_PAYLOAD_KEY!r}. The D4 TTS filter reads the channel off "
        "this header; without the registry row the emitter cannot ship it."
    )

    cli_render = _module(_CLI_RENDER_REL)
    payloads: list[ast.Dict] = []
    for node in ast.walk(cli_render):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if callee != "emit_event":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        type_arg = kwargs.get("type")
        if not (isinstance(type_arg, ast.Constant) and type_arg.value == _OPEN_EVENT_TYPE):
            continue
        payload = kwargs.get("payload")
        if isinstance(payload, ast.Dict):
            payloads.append(payload)

    assert payloads, (
        f"{_CLI_RENDER_REL}: no emit_event(type={_OPEN_EVENT_TYPE!r}, "
        "payload={...}) call with a literal payload dict. The open header "
        "emitter moved; the D4 filter's input is unverifiable from here."
    )

    for payload in payloads:
        entries = {
            key.value: val
            for key, val in zip(payload.keys, payload.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        channel_value = entries.get(_CHANNEL_PAYLOAD_KEY)
        assert channel_value is not None, (
            f"{_CLI_RENDER_REL}:{payload.lineno}: the {_OPEN_EVENT_TYPE} payload "
            f"omits {_CHANNEL_PAYLOAD_KEY!r}. ADR-0009 D4's TTS / broadcaster "
            "filters key off this header — without it every turn looks "
            "speakable and the 3am sweep talks."
        )
        assert not isinstance(channel_value, ast.Constant), (
            f"{_CLI_RENDER_REL}:{payload.lineno}: {_CHANNEL_PAYLOAD_KEY!r} is a "
            "hard-coded literal on the open header. It must be plumbed from "
            "the L3 verdict render_response was called with, or every turn "
            "reports the same channel and the filter is decorative."
        )
