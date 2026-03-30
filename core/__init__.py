"""Core modules for recording, transcription, parsing, and speaker verification."""

from .audio_recorder import AudioRecorder
from .command_parser import CommandParser
from .speaker_encoder import SpeakerEncoder
from .speaker_verifier import SpeakerVerifier, VerificationResult
from .speech_recognizer import SpeechRecognizer, TranscriptionResult

__all__ = [
    "AudioRecorder",
    "CommandParser",
    "SpeakerEncoder",
    "SpeakerVerifier",
    "SpeechRecognizer",
    "TranscriptionResult",
    "VerificationResult",
]
