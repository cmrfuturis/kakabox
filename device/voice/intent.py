"""Intent-Parser für Sprachbefehle der Box.

Aus einem ASR-Transkript wie "spiele bitte das Album Bibi Blocksberg" wird
ein ``PlayCommand`` mit gematchtem Album/Track/Kaka aus einem übergebenen
Katalog herausgezogen. Reine Funktion, keine I/O — voll unit-testbar.

Aktuell nur Play-Intent. Erweiterbar (pause, stop, weiter, lauter, …) ohne
das Schema zu sprengen — dann zusätzliche ``parse_*_command`` Funktionen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Sequence

# Verben, die Wiedergabe anstoßen. Stamm-Match ("spiel*") deckt Konjugationen ab.
_PLAY_VERB_STEMS = ("spiel", "abspiel", "play")
_PLAY_VERB_EXTRA = {"starte", "starten", "leg"}

# Höflichkeits-/Füllwörter, die das eigentliche Entity verschleiern.
_COURTESY = {"bitte", "mal", "doch", "nochmal", "noch"}
_FILLER = {
    "ich", "wir", "mir", "uns", "du", "dir",
    "das", "der", "die", "den", "dem", "des",
    "ein", "eine", "einen", "einer", "eines",
    "lied", "song", "musik", "album", "track", "songs", "lieder",
    "von", "mit", "vom", "im",
    "gerade", "jetzt", "dann", "bitte",
    "kakabox", "box",  # Selbstanrede / Wake-Word
}
_STOPWORDS = _COURTESY | _FILLER | _PLAY_VERB_EXTRA

# Wir splitten an allem außer Wortzeichen + deutschen Sonderzeichen.
_TOKEN_SPLIT = re.compile(r"[^\wäöüß]+", flags=re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    """Ein Eintrag, der per Voice angesprochen werden kann.

    ``aliases`` ergänzt ``name`` um zusätzliche Aufrufnamen (z.B.
    Spitznamen, alternative Schreibweisen, Filmtitel) — Kinder rufen
    "Eiskönigin" statt "Frozen". Die Aliase werden im Webapp-Backend
    pro Song gepflegt und kommen via audio-manifest auf die Box. Beim
    Matching zählt der höchste Score über alle Namen+Aliase.

    ``content_ids`` enthält die Backend-Content-IDs, die abgespielt werden,
    wenn dieser Kandidat triggert: bei kind="track" eine einzelne ID, bei
    kind="artist" mehrere (Reihenfolge entscheidet die Wiedergabe-Reihenfolge).
    """
    id: str
    name: str
    kind: str  # "album" | "kaka" | "track" | "artist"
    aliases: tuple[str, ...] = ()
    content_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PlayCommand:
    target: Candidate
    score: float       # 0.0–1.0, höher = besser passend
    raw_text: str      # Original-Transkript
    query: str         # extrahiertes Entity-Fragment


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t]


def _is_play_verb(token: str) -> bool:
    if token in _PLAY_VERB_EXTRA:
        return True
    return any(token.startswith(stem) for stem in _PLAY_VERB_STEMS)


def has_play_intent(text: str) -> bool:
    """Heuristik: enthält der Text ein Play-Verb?"""
    return any(_is_play_verb(t) for t in _tokenize(text))


def has_magic_word(text: str, word: str = "bitte") -> bool:
    """True, wenn ``word`` als eigenständiges Token im Transkript steht.

    Case-insensitive, robust gegen Punctuation. Für den Zauberwort-Modus:
    ohne "bitte" wird nicht abgespielt, sondern der Frage-Prompt kommt.
    """
    return word.lower() in _tokenize(text)


def extract_query(text: str) -> str:
    """Stripe Kommando-Stopwords und gib das verbleibende Entity-Fragment.

    Reihenfolge bleibt erhalten — wichtig für mehrteilige Namen ("Das
    Dschungelbuch" liefert ``dschungelbuch`` zurück, weil "das" Filler ist).
    """
    tokens = _tokenize(text)
    keep = []
    for t in tokens:
        if _is_play_verb(t):
            continue
        if t in _STOPWORDS:
            continue
        keep.append(t)
    return " ".join(keep).strip()


def _ratio(query: str, name: str) -> float:
    """Ähnlichkeit zwischen ``query`` und ``name`` (0..1).

    SequenceMatcher.ratio() bestraft kurze Queries gegen lange Namen unfair
    ("bibi" vs "Bibi Blocksberg" → 0.42, obwohl die Query exakt enthalten
    ist). Deshalb: bei Substring-Treffer liefern wir mindestens 0.85 zurück
    (höher wenn die Query fast den ganzen Namen abdeckt). Sonst fallback auf
    difflib für richtige Fuzzy-Logik (Buchstabendreher etc.).
    """
    q = query.lower().strip()
    n = name.lower().strip()
    if not q or not n:
        return 0.0
    if q in n:
        return max(0.85, len(q) / len(n))
    return SequenceMatcher(None, q, n).ratio()


def parse_play_command(
    text: str,
    catalog: Sequence[Candidate],
    threshold: float = 0.55,
) -> PlayCommand | None:
    """Erkennt 'spiele/play X' und matched X gegen ``catalog``.

    Rückgabe:
        - ``PlayCommand`` wenn Play-Verb erkannt und Match-Score ≥ threshold
        - ``None`` sonst (kein Verb, leere Query, oder Match zu schwach)

    Threshold ist bewusst niedrig (0.55) — Kinder verschlucken Endungen
    und der Katalog ist meist klein genug, dass False-Positives selten sind.
    Höher anziehen, wenn Falsch-Triggers stören.
    """
    if not has_play_intent(text):
        return None
    query = extract_query(text)
    if not query:
        return None

    best: tuple[float, Candidate] | None = None
    for cand in catalog:
        # Bester Score über Haupt-Name + alle Aliase. Aliase sind gleichwertig
        # zum Namen — wer "eiskönigin" sagt, soll genauso treffen wie "frozen".
        score = _ratio(query, cand.name)
        for alias in cand.aliases:
            score = max(score, _ratio(query, alias))
        if best is None or score > best[0]:
            best = (score, cand)
    if best is None or best[0] < threshold:
        return None
    return PlayCommand(target=best[1], score=best[0], raw_text=text, query=query)
