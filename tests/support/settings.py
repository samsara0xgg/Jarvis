"""A settings file and the paired devices it names."""

from __future__ import annotations

from typing import Any

# The keys the page reads, as config/jarvis.yaml held them on 2026-09-25.
YAML: dict[str, Any] = {
    "llm": {
        "default_preset": "luna",
        "presets": {
            "luna": {"model": "gpt-5.6-luna"},
            "gpt6-luna": {"model": "gpt-6-luna"},
            "gpt6-sol": {"model": "gpt-6-sol"},
        },
    },
    "work_state": {"preset": "gpt6-luna"},
    "daily_report": {"preset": "gpt6-sol"},
    "observer": {
        "repos": ["~/Projects/jarvis", "~/Projects/typlus"],
        "timesink": {"enabled": True},
    },
    "memory": {"retain_audio": True, "audio_retention_days": 30},
    "tools": {"screen": {"retention_days": 7}},
    "realtime": {
        "wake_threshold": 0.95,
        "tts_voice": "Chinese (Mandarin)_Warm_Bestie",
        "output_device": None,
        "gpt_live": {"enabled": True},
        "single_audio_ingress": {"echo_cancellation": "auto"},
    },
}


DEVICES = {
    "input": ["reSpeaker XVF3800 4-Mic Array", "MacBook Pro Microphone"],
    "output": ["MacBook Pro Speakers", "Multi-Output Device 2"],
}
