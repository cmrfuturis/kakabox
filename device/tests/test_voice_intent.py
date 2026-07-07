"""Tests für voice.intent.parse_play_command — kein I/O, kein ASR.

Unit-tests sollen sicherstellen dass:
  - Klare Play-Befehle gegen den Catalog gematched werden
  - Höflichkeits-/Füllwörter den Match nicht stören
  - Leichte Aussprachefehler ("kinder verschlucken Endungen") tolerant sind
  - Texte ohne Play-Verb None liefern
  - Texte mit Play-Verb aber ohne sinnvolle Entity None liefern
"""
import pytest

from voice.intent import (
    Candidate,
    has_play_intent,
    is_random_request,
    is_song_name_question,
    parse_play_command,
)


CATALOG = [
    Candidate(id="bibi-blocksberg", name="Bibi Blocksberg", kind="album"),
    Candidate(id="bambi", name="Bambi", kind="album"),
    Candidate(id="dschungelbuch", name="Das Dschungelbuch", kind="album"),
    Candidate(id="benjamin", name="Benjamin Blümchen", kind="album"),
    Candidate(id="artist:dikka", name="DIKKA", kind="artist"),
]


def test_simple_play():
    cmd = parse_play_command("spiele Bambi", CATALOG)
    assert cmd is not None
    assert cmd.target.id == "bambi"


def test_courtesy_words_stripped():
    cmd = parse_play_command("spiele bitte mal das Album Bambi", CATALOG)
    assert cmd is not None
    assert cmd.target.id == "bambi"


def test_full_sentence():
    cmd = parse_play_command("spielst du mir bitte das dschungelbuch", CATALOG)
    assert cmd is not None
    assert cmd.target.id == "dschungelbuch"


def test_partial_name_with_typo():
    # Kinder verschlucken die Endung: "Bibi Blocksbe" statt "Bibi Blocksberg"
    cmd = parse_play_command("spiele bibi blocksbe", CATALOG)
    assert cmd is not None
    assert cmd.target.id == "bibi-blocksberg"


def test_kakabox_wakeword_stripped():
    cmd = parse_play_command("kakabox spiel bambi", CATALOG)
    assert cmd is not None
    assert cmd.target.id == "bambi"


def test_no_play_verb_returns_none():
    assert parse_play_command("ich mag bambi", CATALOG) is None
    assert parse_play_command("hallo box", CATALOG) is None


def test_empty_after_stripping_returns_none():
    # Nur Verb + Höflichkeit, keine Entity
    assert parse_play_command("spiel bitte", CATALOG) is None


def test_unmatched_entity_returns_none():
    cmd = parse_play_command("spiele asdfqwerzxc", CATALOG)
    assert cmd is None


def test_punctuation_doesnt_break():
    cmd = parse_play_command("Spielst du bitte 'Bambi'?", CATALOG)
    assert cmd is not None
    assert cmd.target.id == "bambi"


def test_score_is_higher_for_better_match():
    exact = parse_play_command("spiele Bambi", CATALOG)
    fuzzy = parse_play_command("spiele bambit", CATALOG)
    assert exact is not None and fuzzy is not None
    assert exact.score > fuzzy.score


def test_short_query_against_long_name():
    """Substring-Boost: 'bibi' soll 'Bibi Blocksberg' matchen, auch ohne
    den Rest auszusprechen — Kinder kürzen oft ab."""
    cmd = parse_play_command("spiel mir bibi", CATALOG)
    assert cmd is not None
    assert cmd.target.id == "bibi-blocksberg"


@pytest.mark.parametrize("phrase", [
    "spiele bitte",
    "Spielst du Bibi?",
    "starte das Album",
    "leg los",
    "abspielen!",
    "play bambi",
])
def test_has_play_intent_positive(phrase):
    assert has_play_intent(phrase)


@pytest.mark.parametrize("phrase", [
    "ich mag bambi",
    "hallo box",
    "stiel und kanne",  # 'stiel' ≠ 'spiel' — darf nicht triggern
    "",
])
def test_has_play_intent_negative(phrase):
    assert not has_play_intent(phrase)


# --- Random-Wunsch ("spiele irgendwas") --------------------------------------

@pytest.mark.parametrize("phrase", [
    "spiele irgendwas",
    "spiel irgendetwas",
    "spiele mir was",
    "spiel etwas",
    "spiele mir bitte irgendein lied",
    "spiele was random",
    "spiel mir egal was",  # "egal" + "was" beide Random-Wörter
    "spiele querbeet",
])
def test_is_random_request_positive(phrase):
    assert is_random_request(phrase)


@pytest.mark.parametrize("phrase", [
    "spiele etwas von DIKKA",  # Entity dahinter → kein Random, sondern Artist
    "spiele bambi",
    "spiel bitte",             # leere Query zählt NICHT als Random
    "ich mag irgendwas",       # kein Play-Verb
    "spiele irgendein bibi",   # konkretes Entity → kein Random
    "",
])
def test_is_random_request_negative(phrase):
    assert not is_random_request(phrase)


def test_random_words_dont_break_artist_match():
    # "spiele etwas von DIKKA" darf weiterhin DIKKA matchen, nicht Random sein.
    assert not is_random_request("spiele etwas von DIKKA")
    cmd = parse_play_command("spiele etwas von DIKKA", CATALOG)
    assert cmd is not None
    assert cmd.target.id == "artist:dikka"


# --- Frage-Intent "Wie heißt dieses Lied?" -----------------------------------

@pytest.mark.parametrize("phrase", [
    "wie heißt das lied",
    "wie heißt dieses lied",
    "wie heißt der song",
    "wie heißt der titel",
    "wie heißt das hier",
    "wie nennt sich das lied",
    "was ist das für ein lied",
    "was ist das denn für ein lied",
    "was ist das hier für musik",
    "welches lied ist das",
    "welcher song ist das",
    "was läuft da",
    "was läuft hier gerade",
    "was spielt gerade",
    "was spielt da gerade",
    "was höre ich da",
    "was hör ich da gerade",
    "wie heißt das lied das gerade läuft",
    "wie heißt das lied das da spielt",
    "wie heißt es",  # #7: generisches "es" + Name-Verb
])
def test_is_song_name_question_positive(phrase):
    assert is_song_name_question(phrase)


@pytest.mark.parametrize("phrase", [
    "spiele bambi",
    "spiele das dschungelbuch",
    "spiele das lied wo der hund bellt",
    "spiele das nächste lied",
    "spiele irgendwas",
    "spiel mir was",
    "spiele was random",
    "spiele was von dikka",
    "spiele ein urlaubslied",
    "spiele bitte",
    "ich mag bambi",
    "hallo box",
    "mach lauter",
    "stopp",
    "wie geht es dir",
    "wer hat das gemacht",
    "was kostet das",
    "was kostet es",            # #7: generisches "es" OHNE Name-/Lauf-Verb
    # #2: Play-Befehle mit Fragewort/Track-Nomen im Titel bleiben Play-Befehle
    "spiele das lied tage wie diese",
    "spiele bitte das lied wie heißt der wind",
    "",
])
def test_is_song_name_question_negative(phrase):
    assert not is_song_name_question(phrase)


def test_song_question_takes_priority_over_random():
    # "was spielt gerade" matcht (gewollt) AUCH is_random_request — der Flow
    # MUSS die Frage zuerst prüfen. Dieser Test dokumentiert die Kollision,
    # damit die Reihenfolge in main.py nicht versehentlich gedreht wird.
    assert is_song_name_question("was spielt gerade")
    assert is_random_request("was spielt gerade")  # ← deshalb Frage zuerst!


# --- Matching-Upgrades (ASR-Plan 2026-07-07, Stufe 1.6/1.7) -------------------

def test_ratio_substring_floor_requires_min_length():
    # Der live reproduzierte False-Positive: "ja" (2 Zeichen) bekam den
    # 0.85-Substring-Floor gegen "DIKKA - Na ja hier". Jetzt: <4 Zeichen
    # → kein Floor, nur ehrliches difflib-Ratio.
    from voice.intent import _ratio
    assert _ratio("ja", "DIKKA - Na ja hier") < 0.5


def test_ratio_substring_floor_requires_word_boundary():
    from voice.intent import _ratio
    # "onst" steckt in "Monster", aber nicht an einer Wortgrenze → kein Floor.
    assert _ratio("onst", "Monsterparty") < 0.85
    # "bibi" steht an einer Wortgrenze → Floor bleibt (der dokumentierte
    # Grund für die Substring-Regel).
    assert _ratio("bibi", "Bibi Blocksberg") >= 0.85


def test_phonetic_match_catches_kids_pronunciation():
    # "Diga" und "DIKKA" sind orthographisch fern, phonetisch identisch
    # (Kölner Code 24) — genau die Kinder-Fehlerklasse aus dem ASR-Plan.
    cat = [Candidate(id="1", name="DIKKA", kind="artist", content_ids=(1,))]
    cmd = parse_play_command("spiele diga", cat)
    assert cmd is not None
    assert cmd.target.id == "1"


def test_token_order_does_not_matter():
    cat = [Candidate(id="1", name="Bibi & Tina - Mädchen gegen Jungs",
                     kind="track", content_ids=(1,))]
    cmd = parse_play_command("spiele mädchen gegen jungs von bibi und tina", cat)
    assert cmd is not None
    assert cmd.target.id == "1"


def test_margin_is_small_for_ambiguous_match():
    cat = [
        Candidate(id="1", name="DIKKA - Na ja hier", kind="track", content_ids=(1,)),
        Candidate(id="2", name="DIKKA - Superkind", kind="track", content_ids=(2,)),
    ]
    cmd = parse_play_command("spiele dikka", cat)
    assert cmd is not None
    assert cmd.margin < 0.1  # beide Kandidaten treffen fast gleich gut


def test_margin_is_large_for_unambiguous_match():
    cat = [
        Candidate(id="1", name="DIKKA - Superkind", kind="track",
                  aliases=("Superkind",), content_ids=(1,)),
        Candidate(id="2", name="Rolf Zuckowski - In der Weihnachtsbäckerei",
                  kind="track", content_ids=(2,)),
    ]
    cmd = parse_play_command("spiele superkind", cat)
    assert cmd is not None
    assert cmd.target.id == "1"
    assert cmd.margin > 0.3


def test_short_title_recovered_in_bare_mode():
    # Regression (Review 2026-07-07): 3-Zeichen-Titel wie "Zug" fielen mit
    # der len>=4-Schwelle durch. Jetzt >=3 → Wortgrenzen-Substring trifft.
    cat = [Candidate(id="7", name="Der Zug hat keine Bremsen", kind="track", content_ids=(7,))]
    cmd = parse_play_command("zug", cat, threshold=0.70, require_play_verb=False)
    assert cmd is not None
    assert cmd.target.id == "7"


def test_phonetic_alone_needs_verb_not_bare_title():
    # Regression (Review 2026-07-07): ein reiner Klang-Treffer ohne "spiele"
    # davor darf den strengen Bare-Title-Threshold NICHT allein reißen.
    cat = [Candidate(id="1", name="DIKKA", kind="artist", content_ids=(1,))]
    # Mit Verb: Phonetik trägt.
    assert parse_play_command("spiele diga", cat) is not None
    # Bare-Title (kein Verb): nur Phonetik reicht nicht.
    assert parse_play_command("diga", cat, threshold=0.70, require_play_verb=False) is None
