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
