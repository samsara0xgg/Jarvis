from __future__ import annotations

from core.media_ducking import SystemAudioDucker, VolumeSnapshot


def test_parse_snapshot_clamps_volume_and_reads_mute():
    assert SystemAudioDucker.parse_snapshot("125,true") == VolumeSnapshot(
        output_volume=100,
        output_muted=True,
    )
    assert SystemAudioDucker.parse_snapshot("-1,false") == VolumeSnapshot(
        output_volume=0,
        output_muted=False,
    )


def test_duck_and_restore_round_trip():
    scripts: list[str] = []

    def runner(script: str) -> str:
        scripts.append(script)
        if "get volume settings" in script:
            return "42,false"
        return ""

    ducker = SystemAudioDucker(platform="darwin", runner=runner)
    assert ducker.duck() is True
    assert ducker.active is True
    ducker.restore()

    assert ducker.active is False
    assert len(scripts) == 3
    assert "output volume 0" in scripts[1]
    assert "output muted true" in scripts[1]
    assert "output volume 42" in scripts[2]
    assert "output muted false" in scripts[2]


def test_nested_ducks_restore_only_after_final_release():
    scripts: list[str] = []

    def runner(script: str) -> str:
        scripts.append(script)
        if "get volume settings" in script:
            return "17,true"
        return ""

    ducker = SystemAudioDucker(platform="darwin", runner=runner)
    assert ducker.duck() is True
    assert ducker.duck() is True
    ducker.restore()
    assert ducker.active is True
    ducker.restore()

    assert ducker.active is False
    assert len(scripts) == 3
    assert "output volume 17" in scripts[-1]
    assert "output muted true" in scripts[-1]


def test_non_macos_is_noop():
    called = False

    def runner(script: str) -> str:
        nonlocal called
        called = True
        return ""

    ducker = SystemAudioDucker(platform="linux", runner=runner)
    assert ducker.duck() is False
    ducker.restore()
    assert called is False
