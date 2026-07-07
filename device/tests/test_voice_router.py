"""Tests für voice/router.py — die aus main.py extrahierte Routing-Kette.

Die REIHENFOLGE der Verzweigungen ist semantisch tragend (Titel-Frage vor
Random vor Match) — diese Tests pinnen sie fest, damit der Eval-Harness
und die Produktion garantiert dieselben Entscheidungen treffen.
"""
from voice.intent import Candidate
from voice.router import is_probable_hallucination, route_transcript

CATALOG = [
    Candidate(id="1", name="DIKKA - Superkind", kind="track",
              aliases=("Superkind",), content_ids=(1,)),
    Candidate(id="2", name="Bibi & Tina - Mädchen gegen Jungs", kind="track",
              content_ids=(2,)),
    Candidate(id="artist:DIKKA", name="DIKKA", kind="artist", content_ids=(1,)),
]


# --- Reihenfolge der Verzweigungen -------------------------------------------

def test_title_question_wins_over_random():
    # "was spielt gerade" enthält das Play-Verb "spielt" und reduziert sich
    # auf "was" (∈ Random-Wörter) — MUSS trotzdem als Frage geroutet werden.
    r = route_transcript("was spielt denn da gerade", CATALOG)
    assert r.action == "title_question"


def test_random_request_routes_to_random():
    r = route_transcript("spiele irgendwas", CATALOG)
    assert r.action == "random"


def test_play_command_routes_to_play_with_command():
    r = route_transcript("spiele superkind", CATALOG)
    assert r.action == "play"
    assert r.command is not None
    assert r.command.target.id == "1"


def test_bare_title_fallback_without_play_verb():
    r = route_transcript("superkind", CATALOG)
    assert r.action == "play"
    assert r.command.target.id == "1"


def test_gibberish_routes_to_no_match():
    r = route_transcript("xylophon quark zebra", CATALOG)
    assert r.action == "no_match"


def test_empty_catalog_routes_to_no_match():
    r = route_transcript("spiele superkind", [])
    assert r.action == "no_match"


# --- Zauberwort-Flag ----------------------------------------------------------

def test_needs_magic_word_set_for_play_without_bitte():
    r = route_transcript("spiele superkind", CATALOG, zauberwort_enabled=True)
    assert r.action == "play"
    assert r.needs_magic_word is True


def test_needs_magic_word_cleared_when_bitte_said():
    r = route_transcript("spiele bitte superkind", CATALOG, zauberwort_enabled=True)
    assert r.action == "play"
    assert r.needs_magic_word is False


def test_title_question_never_needs_magic_word():
    r = route_transcript("wie heißt das lied", CATALOG, zauberwort_enabled=True)
    assert r.action == "title_question"
    assert r.needs_magic_word is False


def test_random_needs_magic_word_too():
    r = route_transcript("spiele irgendwas", CATALOG, zauberwort_enabled=True)
    assert r.action == "random"
    assert r.needs_magic_word is True


# --- Halluzinations-Gate (ASR-Plan 1.5d) --------------------------------------

def test_empty_transcript_is_hallucination():
    assert route_transcript("", CATALOG).action == "hallucination"
    assert route_transcript("   ...  ", CATALOG).action == "hallucination"


def test_known_whisper_hallucination_is_caught():
    r = route_transcript("Untertitelung des ZDF, 2020", CATALOG)
    assert r.action == "hallucination"


def test_real_command_is_not_flagged_as_hallucination():
    assert is_probable_hallucination("spiele superkind") is False


def test_live_reproduced_false_positive_ja_does_not_play():
    # Der im Audit live reproduzierte Fall: "Ja." startete via Substring-Floor
    # das Lied "DIKKA – Na ja hier". Nach dem 1.6-Fix: kein Match mehr.
    catalog = CATALOG + [
        Candidate(id="9", name="DIKKA - Na ja hier", kind="track", content_ids=(9,)),
    ]
    r = route_transcript("Ja.", catalog)
    assert r.action != "play"


def test_command_with_trailing_floskel_is_not_flagged_hallucination():
    # Regression (Review 2026-07-07): eine angehängte Whisper-Floskel darf
    # einen echten Befehl nicht als Halluzination verwerfen — die Phrase muss
    # den Text DOMINIEREN, nicht nur als Teilstring vorkommen. (Ob der Matcher
    # den verwässerten Befehl dann trifft, ist eine separate Frage.)
    assert is_probable_hallucination("spiele superkind bis zum nächsten mal") is False


def test_pure_floskel_is_still_hallucination():
    assert is_probable_hallucination("bis zum nächsten mal") is True
