"""Tests für voice.catalog — Track-, Artist- und Genre-Candidates.

Stellt sicher, dass:
  - Pro Song ein Track-Candidate entsteht (mit Auto-Alias bei "Artist - Titel")
  - Gleiche Artists zu einem Artist-Candidate zusammengefasst werden
  - Gleiche Genres zu einem Genre-Candidate zusammengefasst werden
  - Genre-Aufrufe ("spiele ein Urlaubslied") gegen den Genre-Candidate matchen
"""
import json

import pytest

from voice.catalog import (
    build_catalog_from_songs,
    build_title_map_from_file,
)
from voice.intent import parse_play_command


SONGS = [
    {"content_id": 1, "title": "DIKKA - Superkind", "aliases": [], "genre": "Rap"},
    {"content_id": 2, "title": "DIKKA - Boom", "aliases": [], "genre": "Rap"},
    {"content_id": 3, "title": "Am Strand", "aliases": ["Strandlied"], "genre": "Urlaub"},
    {"content_id": 4, "title": "Sonne pur", "aliases": [], "genre": "Urlaub"},
    {"content_id": 5, "title": "Stille Nacht", "aliases": [], "genre": "Weihnachten"},
    {"content_id": 6, "title": "Ohne Genre", "aliases": [], "genre": None},
]


def _by_kind(cands, kind):
    return [c for c in cands if c.kind == kind]


def test_tracks_for_every_song():
    cands = build_catalog_from_songs(SONGS)
    tracks = _by_kind(cands, "track")
    assert len(tracks) == len(SONGS)


def test_artist_candidate_aggregates_ids():
    cands = build_catalog_from_songs(SONGS)
    artists = _by_kind(cands, "artist")
    dikka = next(c for c in artists if c.name == "DIKKA")
    assert set(dikka.content_ids) == {1, 2}


def test_genre_candidate_aggregates_ids():
    cands = build_catalog_from_songs(SONGS)
    genres = _by_kind(cands, "genre")
    urlaub = next(c for c in genres if c.name == "Urlaub")
    assert set(urlaub.content_ids) == {3, 4}


def test_genre_candidate_count():
    # Rap, Urlaub, Weihnachten — der Song ohne Genre erzeugt keinen Candidate.
    cands = build_catalog_from_songs(SONGS)
    genres = _by_kind(cands, "genre")
    assert sorted(c.name for c in genres) == ["Rap", "Urlaub", "Weihnachten"]


def test_song_without_genre_has_no_genre_candidate():
    cands = build_catalog_from_songs(SONGS)
    genres = _by_kind(cands, "genre")
    assert all("Ohne Genre" != c.name for c in genres)


def test_genre_aliases_generated():
    cands = build_catalog_from_songs(SONGS)
    urlaub = next(c for c in cands if c.kind == "genre" and c.name == "Urlaub")
    assert "Urlaubslied" in urlaub.aliases
    assert "Urlaubssong" in urlaub.aliases


@pytest.mark.parametrize("phrase,expected_ids", [
    ("spiele ein Urlaubslied", {3, 4}),
    ("spiel mir einen Urlaubssong", {3, 4}),
    ("spiele Urlaub", {3, 4}),
    ("spiele ein Weihnachtslied", {5}),
])
def test_genre_phrase_matches_genre_candidate(phrase, expected_ids):
    cands = build_catalog_from_songs(SONGS)
    cmd = parse_play_command(phrase, cands)
    assert cmd is not None, f"kein Match für «{phrase}»"
    assert cmd.target.kind == "genre"
    assert set(cmd.target.content_ids) == expected_ids


def test_empty_songs_yields_empty_catalog():
    assert build_catalog_from_songs([]) == []


# --- Titel-Map (content_id → echter Titel) -----------------------------------

def test_title_map_from_file(tmp_path):
    p = tmp_path / "voice_catalog.json"
    p.write_text(json.dumps({"songs": SONGS}), encoding="utf-8")
    tmap = build_title_map_from_file(p)
    assert tmap[1] == "DIKKA - Superkind"
    assert tmap[3] == "Am Strand"
    assert tmap[5] == "Stille Nacht"
    # int-Keys, alle 6 Songs (auch der ohne Genre hat einen Titel).
    assert all(isinstance(k, int) for k in tmap)
    assert len(tmap) == len(SONGS)


def test_title_map_missing_file_is_empty(tmp_path):
    assert build_title_map_from_file(tmp_path / "fehlt.json") == {}


def test_title_map_skips_entries_without_id_or_title(tmp_path):
    p = tmp_path / "voice_catalog.json"
    p.write_text(json.dumps({"songs": [
        {"content_id": 1, "title": "Gut"},
        {"content_id": None, "title": "Kein ID"},
        {"content_id": 2, "title": ""},
        {"title": "Kein content_id"},
    ]}), encoding="utf-8")
    assert build_title_map_from_file(p) == {1: "Gut"}
