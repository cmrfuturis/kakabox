"""Routing eines ASR-Transkripts auf eine Box-Aktion — pure Funktion.

Extrahiert aus ``main.py::_run_voice_activation`` (ASR-Plan 2026-07-07,
Stufe 0): Die Entscheidungskette (Titel-Frage → Random → Catalog-Match →
Bare-Title-Fallback) muss von Produktion UND Eval-Harness geteilt werden,
sonst misst der Harness etwas anderes, als die Box tut.

Die REIHENFOLGE ist semantisch tragend (siehe Kommentare in intent.py):
"was spielt gerade" enthält das Play-Verb "spielt" und reduziert sich auf
"was" (∈ Random-Wörter) — die Titel-Frage MUSS deshalb vor dem Random-Check
laufen. Der Bare-Title-Fallback läuft zuletzt mit strengerem Threshold.

Seiteneffekte (Prompts, LEDs, Playback, Zauberwort-Nachfrage) bleiben in
main.py — hier wird nur ENTSCHIEDEN, nie ausgeführt.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from voice.intent import (
    Candidate,
    PlayCommand,
    has_magic_word,
    is_random_request,
    is_song_name_question,
    parse_play_command,
)

# Bekannte Whisper-Halluzinationen auf Stille/Rauschen (ASR-Plan Stufe 1.5d).
# whisper-tiny produziert auf Near-Silence systematisch Trainingsdaten-Artefakte
# — meist Untertitel-Credits. Vergleich läuft normalisiert (lowercase, ohne
# Satzzeichen), Teilstring-Match damit auch "  Untertitelung des ZDF, 2020"
# gefangen wird.
HALLUCINATION_PHRASES = (
    "untertitelung des zdf",
    "untertitel im auftrag des zdf",
    "untertitel von stephanie geiges",
    "untertitel der amara org gemeinschaft",
    "copyright wdr",
    "swr 2021",
    "vielen dank fürs zuschauen",
    "vielen dank für ihre aufmerksamkeit",
    "das war's für heute",
    "bis zum nächsten mal",
)


def _normalize(text: str) -> str:
    return " ".join(
        "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text.lower()).split()
    )


# Eine Halluzinations-Phrase muss den Transkript-Text DOMINIEREN (nicht nur
# irgendwo als Teilstring vorkommen), sonst würde ein echter Befehl mit
# angehängter Whisper-Floskel ("spiele superkind bis zum nächsten mal") komplett
# verworfen. 0.6 = die Phrase macht ≥60 % des Textes aus.
_HALLUCINATION_MIN_COVERAGE = 0.6


def is_probable_hallucination(text: str) -> bool:
    """True für leere Transkripte und bekannte Whisper-Stille-Halluzinationen.

    Die Phrase muss den Text dominieren (Längenanteil ≥ Coverage-Schwelle) —
    ein kurzer echter Befehl mit angehängter Floskel bleibt so ein echter Befehl.
    """
    norm = _normalize(text)
    if not norm:
        return True
    for phrase in HALLUCINATION_PHRASES:
        if phrase in norm and len(phrase) >= _HALLUCINATION_MIN_COVERAGE * len(norm):
            return True
    return False


@dataclass(frozen=True)
class RouteResult:
    """Entschiedene Aktion für ein Transkript.

    ``action``:
      - "title_question" — "Wie heißt dieses Lied?" beantworten
      - "random"         — Random-Modus starten
      - "play"           — ``command.target`` abspielen
      - "no_match"       — nichts erkannt → Error-Feedback
      - "hallucination"  — Transkript ist leer/bekannter Whisper-Müll →
                           wie no_match behandeln, aber getrennt gezählt
                           (Eval-Harness-Metrik)
    ``needs_magic_word``: True wenn der Zauberwort-Modus aktiv ist und
    "bitte" im Transkript FEHLT — der Aufrufer muss dann nachfragen, bevor
    er die Aktion ausführt (gilt für "random" und "play", nie für die Frage).
    """
    action: str
    command: Optional[PlayCommand] = None
    needs_magic_word: bool = False


def route_transcript(
    text: str,
    catalog: Sequence[Candidate],
    *,
    zauberwort_enabled: bool = False,
    play_threshold: float = 0.55,
    bare_title_threshold: float = 0.70,
) -> RouteResult:
    """Entscheidet, was die Box mit einem Transkript tun soll (pure Funktion)."""
    if is_probable_hallucination(text):
        return RouteResult(action="hallucination")

    # Titel-Frage MUSS als erste Verzweigung laufen (siehe Modul-Doku).
    # Kein Zauberwort-Gate — eine Frage ist kein Play.
    if is_song_name_question(text):
        return RouteResult(action="title_question")

    needs_magic = zauberwort_enabled and not has_magic_word(text)

    if is_random_request(text):
        return RouteResult(action="random", needs_magic_word=needs_magic)

    if not catalog:
        return RouteResult(action="no_match")

    cmd = parse_play_command(text, catalog, threshold=play_threshold)
    if cmd is None:
        # Bare-Title-Fallback: Kinder sagen oft nur den Titel ohne "spiele"
        # davor. Strenger Threshold gegen Fehltreffer durch Gerede/Nuscheln.
        cmd = parse_play_command(
            text, catalog,
            threshold=bare_title_threshold,
            require_play_verb=False,
        )
    if cmd is None:
        return RouteResult(action="no_match")
    return RouteResult(action="play", command=cmd, needs_magic_word=needs_magic)
