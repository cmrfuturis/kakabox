"""Voice-Eingabe für die Kakabox.

Architektur:
    [Audio] ─► ASR ─► Text ─► Intent-Parser ─► Catalog-Match ─► Action
              Vosk          (regex+stopwords)   (difflib)         (Player)

Aktueller Stand: Intent-Parser + ASR-Wrapper sind fertig und unabhängig
testbar. Push-to-Talk-Verkabelung an die Hardware kommt, sobald das
Mikrofon physisch da ist (siehe device/voice/README.md).
"""
from .intent import (
    Candidate,
    PlayCommand,
    has_play_intent,
    parse_play_command,
)

__all__ = [
    "Candidate",
    "PlayCommand",
    "has_play_intent",
    "parse_play_command",
]
