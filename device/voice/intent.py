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
from functools import lru_cache
from typing import Sequence

# Optionale Matching-Verstärker (ASR-Plan 2026-07-07, Stufe 1.7). Beide sind
# pure-Python-freundlich installierbar; fehlen sie, fällt das Matching still
# auf das bewährte difflib-Verhalten zurück — die Box bricht nie deswegen.
try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore
except ImportError:  # pragma: no cover - Umgebung ohne rapidfuzz
    _rf_fuzz = None
try:
    import cologne_phonetics as _cologne  # type: ignore
except ImportError:  # pragma: no cover - Umgebung ohne cologne-phonetics
    _cologne = None

# Verben, die Wiedergabe anstoßen. Stamm-Match ("spiel*") deckt Konjugationen ab.
_PLAY_VERB_STEMS = ("spiel", "abspiel", "play")
_PLAY_VERB_EXTRA = {"starte", "starten", "leg"}

# Wörter, die "spiel irgendwas" bedeuten → lösen den Random-Modus aus statt
# eines Catalog-Matches. Greift NUR, wenn die ganze Query daraus besteht (siehe
# is_random_request) — sonst bliebe "spiele etwas von DIKKA" kein Artist-Match.
_RANDOM_WORDS = {
    "irgendwas", "irgendetwas", "etwas", "irgendein", "irgendeine",
    "irgendeinen", "irgendeins", "was", "random", "zufällig", "zufall",
    "egal", "alles", "querbeet",
}

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
    # Abstand zum zweitbesten Kandidaten (1.0 = konkurrenzlos). Kleine Margin
    # = mehrdeutiger Treffer — Grundlage für den späteren Rückfrage-Flow
    # ("Meinst du X?"). Wird vorerst nur geloggt, nicht gegatet.
    margin: float = 1.0


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if t]


def _is_play_verb(token: str) -> bool:
    if token in _PLAY_VERB_EXTRA:
        return True
    return any(token.startswith(stem) for stem in _PLAY_VERB_STEMS)


def has_play_intent(text: str) -> bool:
    """Heuristik: enthält der Text ein Play-Verb?"""
    return any(_is_play_verb(t) for t in _tokenize(text))


def is_random_request(text: str) -> bool:
    """True, wenn der Text 'spiel irgendwas' meint → Random-Modus.

    Bedingung: ein Play-Verb ist da UND die verbleibende Query (nach Strippen
    von Verb + Stopwords) besteht *ausschließlich* aus Random-Wörtern. So bleibt
    "spiele etwas von DIKKA" ein Artist-Match (Query enthält "dikka"), während
    "spiele irgendwas" / "spiele mir was" den Random-Modus auslöst.

    Eine leere Query (z.B. "spiel bitte") zählt bewusst NICHT als Random — das
    kann auch ein abgeschnittenes/vernuscheltes Kommando sein, und der bestehende
    Pfad liefert dafür schon den Error-Ton.
    """
    if not has_play_intent(text):
        return False
    tokens = [
        t for t in _tokenize(text)
        if not _is_play_verb(t) and t not in _STOPWORDS
    ]
    return bool(tokens) and all(t in _RANDOM_WORDS for t in tokens)


# --- Frage-Intent "Wie heißt dieses Lied?" -----------------------------------
# Strukturelle Erkennung (kein Catalog-Match): Die Antwort — der aktuell
# laufende Titel — kennt nur der Main-Loop. Hier wird rein grammatisch
# entschieden. Bewusst getrennt von has_play_intent, damit "spiele das lied
# wo der hund bellt" NICHT als Frage gilt (es fehlt das Fragewort).
_QUESTION_WORDS = {"wie", "was", "welches", "welcher", "welche", "wer", "wieso", "wessen"}
_TRACK_NOUNS = {"lied", "song", "titel", "musik", "stück", "liedchen", "songs", "liedlein"}
_NAME_VERBS = {"heißt", "heisst", "heist", "heißen", "nennt", "nennst", "genannt"}
_PLAYING_VERBS = {
    "läuft", "lauft", "spielt", "kommt", "tönt", "toent", "dröhnt",
    "höre", "hör", "hören", "hörst",
}
# Demonstrativ-Bezug auf den laufenden Track ("das"/"dieses" Lied). "es" ist
# generisch, wird aber nur in Kombination mit einem Name-/Lauf-Verb gewertet
# (Muster A/C) — "wie heißt es" funktioniert, "was kostet es" nicht.
_QUESTION_DEMO = {"das", "dieses", "dieser", "diese", "der", "dem", "den", "es"}
_HERE_NOW = {"da", "gerade", "jetzt", "grad", "hier", "eben", "denn"}

# Gesamtes "Frage-Struktur"-Vokabular. Bleibt nach dem Strippen von Play-Verben
# + Stopwords NUR solches übrig, ist es eine Frage ("was spielt da gerade").
# Taucht ein Wort AUSSERHALB auf, ist es ein echter Titel → Play-Befehl
# ("spiele das lied TAGE WIE DIESE").
_QUESTION_VOCAB = (
    _QUESTION_WORDS | _TRACK_NOUNS | _NAME_VERBS
    | _PLAYING_VERBS | _QUESTION_DEMO | _HERE_NOW | _RANDOM_WORDS
)


def is_song_name_question(text: str) -> bool:
    """True, wenn der Text fragt "wie heißt das gerade laufende Lied?".

    Kein Catalog-Match — die Antwort (der aktuelle Titel) kennt nur main.py.
    Entscheidet rein strukturell: Fragewort (Pflicht-Gate) + Bezug auf den
    laufenden Titel über eines von drei Mustern:

      A) Name-Verb + (Track-Nomen oder Demonstrativ) — "wie heißt das lied"
      B) Track-Nomen + Demonstrativ ohne Name-Verb   — "welches lied ist das"
      C) Lauf-/Hör-Verb + (Hier/Jetzt | Demonstrativ | Track-Nomen)
                                                       — "was läuft da"

    WICHTIG (Reihenfolge im Flow): "was spielt gerade" enthält das Play-Verb
    "spielt" und reduziert sich auf "was" (∈ Random-Wörter) — diese Funktion
    MUSS daher VOR is_random_request/parse_play_command geprüft werden, sonst
    landet die Frage fälschlich im Random-Modus.
    """
    tokset = set(_tokenize(text))
    if not tokset:
        return False
    if not (tokset & _QUESTION_WORDS):  # Fragewort ist Pflicht
        return False
    # Echter Abspielwunsch mit Titel? "spiele das lied tage wie diese" enthält
    # Fragewort + "lied" + Demonstrativ, ist aber ein Play-Befehl. Greift nur bei
    # vorhandenem Play-Verb: bleibt nach dem Strippen ein Wort AUSSERHALB des
    # Frage-Vokabulars übrig (= echter Titel-Token), ist es eine Wiedergabe-
    # Anweisung, keine Frage. "was spielt da gerade" übersteht das (Rest nur
    # was/da ∈ Frage-Vokabular).
    if has_play_intent(text):
        residual = [
            t for t in _tokenize(text)
            if not _is_play_verb(t) and t not in _STOPWORDS
        ]
        if any(t not in _QUESTION_VOCAB for t in residual):
            return False
    has_demo = bool(tokset & _QUESTION_DEMO)
    has_noun = bool(tokset & _TRACK_NOUNS)
    has_name = bool(tokset & _NAME_VERBS)
    has_playing = bool(tokset & _PLAYING_VERBS)
    has_here = bool(tokset & _HERE_NOW)
    if has_name and (has_noun or has_demo):        # Muster A
        return True
    if has_noun and has_demo and not has_name:     # Muster B
        return True
    if has_playing and (has_here or has_demo or has_noun):  # Muster C
        return True
    return False


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


# Ab welcher Query-Länge der Substring-Floor / die Fuzzy-/Phonetik-Verfahren
# greifen. 2-Zeichen-Fragmente ("ja", "es", "da") matchen sonst als Wort-
# Teilmenge unzähliger Titel — der live reproduzierte False-Positive
# ("Ja." → "DIKKA – Na ja hier"). 3 Zeichen erlaubt kurze echte Titel ("Zug",
# "Bus"), schließt die typischen Füllwörter aber aus.
_MIN_MATCH_LEN = 3
# Obergrenze für Fuzzy-/Phonetik-Treffer im Bare-Title-Modus (kein "spiele"
# davor). Ein reiner Klang-/Token-Treffer soll den lockeren "spiele …"-
# Threshold (0.55) schaffen, aber NICHT allein den strengen Bare-Title-
# Threshold (0.70) — dort muss die Orthographie (_ratio) mitziehen. Ohne
# diese Kappe startete "gegen" (Token in "Mädchen gegen Jungs") ein Lied.
_BARE_TITLE_BOOST_CAP = 0.69


def _ratio(query: str, name: str) -> float:
    """Ähnlichkeit zwischen ``query`` und ``name`` (0..1).

    SequenceMatcher.ratio() bestraft kurze Queries gegen lange Namen unfair
    ("bibi" vs "Bibi Blocksberg" → 0.42, obwohl die Query exakt enthalten
    ist). Deshalb: bei Substring-Treffer mindestens 0.85 — aber NUR wenn die
    Query ≥ _MIN_MATCH_LEN Zeichen hat UND an einer Wortgrenze im Namen steht.
    "bibi" → "Bibi & Tina - …" bleibt Volltreffer, "ja" (2 Zeichen) nicht.
    Sonst fallback auf difflib für richtige Fuzzy-Logik (Buchstabendreher).
    """
    q = query.lower().strip()
    n = name.lower().strip()
    if not q or not n:
        return 0.0
    if len(q) >= _MIN_MATCH_LEN and re.search(
        rf"(?<![\wäöüß]){re.escape(q)}(?![\wäöüß])", n
    ):
        return max(0.85, len(q) / len(n))
    return SequenceMatcher(None, q, n).ratio()


@lru_cache(maxsize=2048)
def _phonetic_codes(text: str) -> tuple[str, ...]:
    """Kölner-Phonetik-Codes der Wörter in ``text`` (leere Codes gefiltert).

    Gecacht (ASR-Plan 1.7): die ~160 Katalog-Namen+Aliase werden bei JEDEM
    Voice-Befehl gescort — ohne Cache kostet das ~14 ms/Befehl reine
    Neukodierung auf dem Pi 5 (gemessen). Die Codes sind statisch, der Cache
    deckt Katalog- UND Query-Seite ab.
    """
    if _cologne is None:
        return ()
    try:
        return tuple(code for _, code in _cologne.encode(text) if code)
    except Exception:  # pragma: no cover - defensiv gegen Sonderzeichen-Edgecases
        return ()


def _score_pair(query: str, name: str, q_codes: tuple[str, ...], allow_boost: bool) -> float:
    """Bester Score über drei Verfahren (ASR-Plan Stufe 1.7).

    1. ``_ratio``: difflib + Wortgrenzen-Substring (Basis, immer aktiv).
    2. rapidfuzz ``token_set_ratio``: robust gegen Wortreihenfolge/Zusatzwörter
       ("blocksberg bibi" ↔ "Bibi Blocksberg").
    3. Kölner Phonetik: fängt orthographisch ferne, klanggleiche Kinder-Fehler
       ("Diga" ↔ "DIKKA", beide Code 24).

    ``q_codes`` sind die vorab EINMAL berechneten Query-Phonetik-Codes (nicht
    pro Kandidat neu — der Aufrufer reicht sie durch). ``allow_boost`` schaltet
    Verfahren 2+3 nur im verb-bestätigten "spiele …"-Pfad voll frei; im Bare-
    Title-Modus (kein Verb) werden sie auf ``_BARE_TITLE_BOOST_CAP`` gedeckelt,
    damit ein reiner Klang-/Token-Treffer nicht ohne Orthographie ein Lied
    startet. Verfahren 2+3 greifen erst ab ``_MIN_MATCH_LEN`` Zeichen.
    """
    score = _ratio(query, name)
    if len(query) < _MIN_MATCH_LEN:
        return score

    cap = 1.0 if allow_boost else _BARE_TITLE_BOOST_CAP
    if _rf_fuzz is not None:
        boost = _rf_fuzz.token_set_ratio(query, name) / 100.0 * 0.97
        score = max(score, min(boost, cap))
    if q_codes:
        n_codes = _phonetic_codes(name)
        if n_codes:
            if set(q_codes) <= set(n_codes):
                # Alle Query-Wörter phonetisch im Namen enthalten.
                boost = 0.90
            else:
                boost = SequenceMatcher(
                    None, " ".join(q_codes), " ".join(n_codes)
                ).ratio() * 0.90
            score = max(score, min(boost, cap))
    return score


def parse_play_command(
    text: str,
    catalog: Sequence[Candidate],
    threshold: float = 0.55,
    require_play_verb: bool = True,
) -> PlayCommand | None:
    """Erkennt 'spiele/play X' und matched X gegen ``catalog``.

    Rückgabe:
        - ``PlayCommand`` wenn (Play-Verb erkannt ODER ``require_play_verb=False``)
          und Match-Score ≥ threshold
        - ``None`` sonst (Verb fehlt obwohl gefordert, leere Query, Match zu schwach)

    Threshold ist bewusst niedrig (0.55) — Kinder verschlucken Endungen
    und der Katalog ist meist klein genug, dass False-Positives selten sind.
    Höher anziehen, wenn Falsch-Triggers stören.

    ``require_play_verb=False`` lockert die Verb-Pflicht für den Bare-Title-
    Fallback: Kinder sagen oft nur den Titel ("Der Zug hat keine Bremsen")
    ohne "spiele" davor. Dann steht und fällt die Trefferqualität allein am
    ``threshold`` — der Aufrufer sollte ihn höher setzen, damit zufälliges
    Gerede nicht fälschlich einen Song auslöst.
    """
    if require_play_verb and not has_play_intent(text):
        return None
    query = extract_query(text)
    if not query:
        return None

    # Query-Phonetik EINMAL berechnen (nicht pro Kandidat × Alias neu — das
    # kostete sonst messbar CPU pro Befehl). allow_boost=False im Bare-Title-
    # Modus (kein Verb): dort deckelt _score_pair Fuzzy/Phonetik, damit ein
    # reiner Klang-Treffer nicht ohne Orthographie ein Lied startet.
    q_codes = _phonetic_codes(query) if len(query) >= _MIN_MATCH_LEN else ()
    allow_boost = require_play_verb

    best: tuple[float, Candidate] | None = None
    second_score = 0.0
    for cand in catalog:
        # Bester Score über Haupt-Name + alle Aliase. Aliase sind gleichwertig
        # zum Namen — wer "eiskönigin" sagt, soll genauso treffen wie "frozen".
        score = _score_pair(query, cand.name, q_codes, allow_boost)
        for alias in cand.aliases:
            score = max(score, _score_pair(query, alias, q_codes, allow_boost))
        if best is None or score > best[0]:
            if best is not None:
                second_score = max(second_score, best[0])
            best = (score, cand)
        else:
            second_score = max(second_score, score)
    if best is None or best[0] < threshold:
        return None
    return PlayCommand(
        target=best[1], score=best[0], raw_text=text, query=query,
        margin=best[0] - second_score,
    )
