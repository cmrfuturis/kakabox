"""Tests für voice.intent.parse_play_command — kein I/O, kein ASR.

Unit-tests sollen sicherstellen dass:
  - Klare Play-Befehle gegen den Catalog gematched werden
  - Höflichkeits-/Füllwörter den Match nicht stören
  - Leichte Aussprachefehler ("kinder verschlucken Endungen") tolerant sind
  - Texte ohne Play-Verb None liefern
  - Texte mit Play-Verb aber ohne sinnvolle Entity None liefern
"""
import pytest

from voice.intent import Candidate, has_play_intent, parse_play_command


CATALOG = [
    Candidate(id="bibi-blocksberg", name="Bibi Blocksberg", kind="album"),
    Candidate(id="bambi", name="Bambi", kind="album"),
    Candidate(id="dschungelbuch", name="Das Dschungelbuch", kind="album"),
    Candidate(id="benjamin", name="Benjamin Blümchen", kind="album"),
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
