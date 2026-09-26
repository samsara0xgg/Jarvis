"""The desktop switches Jarvis's fixed text between Chinese and English.

Each check asserts what a caller observes: the route's status and answer, the
user's settings.yaml afterwards, and the fixed sentence Jarvis now says.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from jarvis.runtime import save_language
from jarvis.shared import lang
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

DESKTOP = {"Authorization": "Bearer desktop"}
SETTINGS = """# Allen's own settings.
language: zh

projects:
  - id: school
    name: 学业
"""


def _app(settings: Path) -> TestClient:
    return TestClient(
        create_app(
            InherentDeps(
                submit_callable=lambda _text: None,
                broadcaster=InherentBroadcaster(),
                plugin_authorize=lambda header: header == DESKTOP["Authorization"],
                language_save=functools.partial(save_language, settings),
            )
        )
    )


def test_the_switch_needs_the_desktop_credential_and_a_known_language(tmp_path: Path) -> None:
    """No credential 401, an unknown language 400; neither touches the file or the speech."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(SETTINGS, encoding="utf-8")
    with _app(settings) as client:
        assert client.post("/inherent/language", json={"language": "en"}).status_code == 401
        bad = client.post("/inherent/language", headers=DESKTOP, json={"language": "fr"})
        assert bad.status_code == 400
        assert client.get("/inherent/language").json() == {"language": "zh"}
    assert settings.read_text(encoding="utf-8") == SETTINGS
    assert lang.t("tier0.date_today", spoken_date="9月26日") == "今天是9月26日。"


def test_switching_to_english_changes_the_next_sentence_and_only_the_language_line(
    tmp_path: Path,
) -> None:
    """The answer, the next fixed sentence and settings.yaml all say en; the rest stays."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(SETTINGS, encoding="utf-8")
    with _app(settings) as client:
        answer = client.post("/inherent/language", headers=DESKTOP, json={"language": "en-CA"})
        assert answer.status_code == 200
        assert answer.json() == {"language": "en"}
        assert client.get("/inherent/language").json() == {"language": "en"}
    assert lang.t("tier0.date_today", spoken_date="September 26") == "Today is September 26."
    assert settings.read_text(encoding="utf-8") == SETTINGS.replace("language: zh", "language: en")


def test_a_file_without_the_line_gets_it_appended(tmp_path: Path) -> None:
    """A user who never set a language gets one line added at the end, nothing else."""
    settings = tmp_path / "settings.yaml"
    settings.write_text("projects: []", encoding="utf-8")
    with _app(settings) as client:
        answer = client.post("/inherent/language", headers=DESKTOP, json={"language": "zh"})
        assert answer.json() == {"language": "zh"}
    assert settings.read_text(encoding="utf-8") == "projects: []\nlanguage: zh\n"
