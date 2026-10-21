"""``/inherent/settings``: Jarvis's own settings page, and the changes posted to it (ADR 0052).

The desktop's Settings page draws every value and option; the phone's sheet reads the conversation
model and effort and posts a pick. The examples are what the guarded route answers over the real
``Settings`` page and the repository's own ``config/jarvis.yaml``, with a fixed list of audio
devices in place of the machine's. The changes are what the pages post: the brain must take every
valid one, refuse every invalid one with a 400 or 422 and save nothing of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from jarvis.runtime.daemon.routes import save_settings
from jarvis.runtime.settings import SETTINGS_FILE, Settings, apply_settings
from tests.support.contracts import WORLD_NOW_MS, Contracts, Inputs, brain_at
from tests.support.timeline import Peers, peers_with

ROUTE = "POST /inherent/settings"
CONFIG = Path(__file__).resolve().parents[2] / "config" / "jarvis.yaml"
DEVICES = {"output": ["MacBook Pro Speakers", "AirPods Pro"], "input": ["MacBook Pro Microphone"]}
DEFAULT_DEVICE = {"output": "MacBook Pro Speakers", "input": "MacBook Pro Microphone"}


def _page(root: Path) -> Settings:
    """The page as the runtime builds it: the boot config under the saved file, fixed devices."""
    root.mkdir()
    config = apply_settings(yaml.safe_load(CONFIG.read_text(encoding="utf-8")), root)
    return Settings(root, config, lambda kind: DEVICES[kind], DEFAULT_DEVICE.get)


def _peers(root: Path, page: Settings) -> Peers:
    async def read() -> dict[str, Any]:
        return page.read()

    keys = root / "keys"
    keys.mkdir()
    return peers_with(
        keys, settings_read=read, settings_update=lambda changes: save_settings(page, changes),
    )


def _saved(root: Path) -> dict[str, Any]:
    path = root / SETTINGS_FILE
    saved: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return saved


def test_the_page_the_desktop_draws_and_the_phone_reads_before_and_after_a_change(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """Values, the options of each choice, what "System default" is now, and a restart pending."""
    page = _page(tmp_path / "root")
    peers = _peers(tmp_path / "root", page)
    with brain_at():
        fresh = peers.paired.get("/inherent/settings")
        changed = peers.paired.post(
            "/inherent/settings",
            json={"changes": {
                "wake_threshold": 0.9, "conversation_model": "opus",
                "tts_voice": "Chinese (Mandarin)_Warm_Bestie", "output_device": "AirPods Pro",
            }},
        )
    assert (fresh.status_code, changed.status_code) == (200, 200)
    contracts.output(
        "settings/page",
        examples={"fresh": fresh.json(), "changed": changed.json()},
        exact=("restart_pending",), now_ms=WORLD_NOW_MS, route="GET /inherent/settings",
        description=(
            "The Settings page. `values` is every setting by key as the page shows it (a device "
            "or a voice by name, `System default` for none) with the models the daemon runs in "
            "`model_*`; `options` is what a choice may be (`tts_voice`, `output_device` and "
            "`input_device` by name, `audio_days` and `screenshot_days` in days with null for "
            "forever, `conversation_model` and, per model, `conversation_effort`); `defaults` "
            "names what `System default` is now (null when unknown); `restart_pending` is true "
            "when a saved value waits for a restart. The POST answers with the same body after "
            "saving. A page that is not given a key leaves its row out: a client reads what it "
            "knows."
        ),
    )
    assert fresh.json()["restart_pending"] is False
    assert changed.json()["restart_pending"] is True
    assert changed.json()["values"]["wake_threshold"] == 0.9
    assert changed.json()["values"]["conversation_model"] == "opus"
    assert fresh.json()["options"]["output_device"] == ["System default", *DEVICES["output"]]
    assert fresh.json()["defaults"] == {
        "output_device": "MacBook Pro Speakers", "input_device": "MacBook Pro Microphone",
    }
    assert changed.json()["values"]["output_device"] == "AirPods Pro"
    assert isinstance(changed.json()["values"]["tts_voice"], str)


def _refusals(inputs: Inputs, statuses: list[int]) -> dict[str, str]:
    return {case.name: str(status) for case, status in zip(inputs.invalid, statuses, strict=True)}


def test_the_brain_saves_every_valid_change_and_refuses_the_rest_saving_nothing_of_them(
    tmp_path: Path, contracts: Contracts,
) -> None:
    """A valid body answers the page with the value in it; a refused one leaves the file alone."""
    inputs = contracts.inputs("settings/changes", route=ROUTE)
    page = _page(tmp_path / "root")
    peers = _peers(tmp_path / "root", page)
    with brain_at():
        taken = {
            case.name: peers.paired.post("/inherent/settings", json=case.value)
            for case in inputs.valid
        }
        saved_after_valid = _saved(tmp_path / "root")
        refused = []
        for case in inputs.invalid:
            answer = peers.paired.post("/inherent/settings", json=case.value)
            refused.append(answer.status_code)
            assert _saved(tmp_path / "root") == saved_after_valid, f"{case.name} saved something"
    assert {name: answer.status_code for name, answer in taken.items()} == {
        case.name: 200 for case in inputs.valid
    }
    assert _refusals(inputs, refused) == {case.name: case.refusal for case in inputs.invalid}
    for case in inputs.valid:
        body = taken[case.name].json()
        assert set(body) >= {"values", "options", "defaults", "restart_pending"}
    # a body names what to change by key; the page answers each key with what it will show
    kept = {key for case in inputs.valid for key in case.value["changes"]}
    assert kept <= set(saved_after_valid), kept - set(saved_after_valid)
