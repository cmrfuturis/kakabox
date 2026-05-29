"""Catalog-Builder für den Voice-Matcher.

Liest den vom Audio-Sync geschriebenen ``voice_catalog.json`` (Liste der
Backend-Songs mit Titel + Aliasen) und produziert eine Liste von
``Candidate``-Objekten:

  - **Pro Song** ein ``kind="track"``-Candidate. Wenn der Titel dem Schema
    ``<ARTIST> - <TRACK>`` folgt, wird der reine Track-Teil zusätzlich als
    Alias geführt, damit "spiele Peace" auch ohne Artist trifft.

  - **Pro eindeutigem Artist** ein ``kind="artist"``-Candidate, der bei
    Match die Wiedergabe ALLER Songs dieses Artists triggert. So funktioniert
    "spiele DIKKA" automatisch als Künstler-Playlist.

  - **Pro Genre** ein ``kind="genre"``-Candidate, der ALLE Songs dieses Genres
    sammelt — "spiele ein Urlaubslied" spielt die ganze Urlaub-Kategorie
    (gemischt, siehe ``_play_voice_target``). Aliase decken die typischen
    Sprechweisen ab ("Urlaubslied", "Urlaubssong", "Urlaubsmusik").

Reine Funktion ohne I/O-Abhängigkeit zum Player — die Voice-Aktivierung im
Main-Loop bekommt von ``Candidate.content_ids`` die abzuspielenden IDs und
baut daraus eine Playlist.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from voice.intent import Candidate

logger = logging.getLogger(__name__)

# "DIKKA - Superkind" → ("DIKKA", "Superkind"). Auch "Bibi & Tina - Lied" greift.
# Optionaler Whitespace um den Bindestrich; der Match ist NICHT-greedy, damit
# Titel mit mehrfachen "-" (selten, aber möglich) am ersten Trenner splitten.
_ARTIST_TITLE_RE = re.compile(r"^\s*(.+?)\s+-\s+(.+?)\s*$")


def _parse_artist(title: str) -> tuple[str | None, str]:
    """Splittet '<Artist> - <Track>' → (artist, track_only). Sonst (None, title)."""
    m = _ARTIST_TITLE_RE.match(title or "")
    if not m:
        return None, (title or "").strip()
    return m.group(1).strip(), m.group(2).strip()


# Sprechweisen für Genre-Aufrufe: "Urlaub" → auch "Urlaubslied(er)", "Urlaubssong(s)",
# "Urlaubsmusik". Mit Fugen-s, weil Kinder genau so reden ("ein Urlaubslied").
# Der Genre-Name selbst bleibt der Haupt-Treffer; die Fuzzy-Logik fängt den Rest.
_GENRE_ALIAS_SUFFIXES = ("slied", "slieder", "ssong", "ssongs", "smusik")


def _genre_aliases(genre: str) -> tuple[str, ...]:
    """Erzeugt Aufruf-Aliase für ein Genre. Leeres/zu kurzes Genre → keine."""
    g = genre.strip()
    if len(g) < 3:
        return ()
    return tuple(f"{g}{suffix}" for suffix in _GENRE_ALIAS_SUFFIXES)


def build_catalog_from_songs(songs: list[dict]) -> list[Candidate]:
    """Erzeugt Track- und Artist-Candidates aus einer Liste von Song-Dicts.

    ``songs`` ist die Liste aus ``voice_catalog.json["songs"]``. Format pro
    Eintrag: ``{"content_id": int, "title": str, "aliases": list[str]}``.

    Reihenfolge der Rückgabe:
      1. Alle Track-Candidates in Catalog-Reihenfolge (damit der Matcher bei
         Ties den ersten passenden Song nimmt — meist der relevanteste).
      2. Alle Artist-Candidates alphabetisch.

    Ein Song ohne Artist-Prefix erzeugt keinen Artist-Candidate, läuft aber
    normal als Track durch (z.B. "Bestellt", "Captain Schlumpf").
    """
    tracks: list[Candidate] = []
    artist_to_ids: dict[str, list[int]] = {}
    genre_to_ids: dict[str, list[int]] = {}

    for song in songs:
        cid = song.get("content_id")
        title = song.get("title") or ""
        if not cid or not title:
            continue
        cid = int(cid)

        artist, track_only = _parse_artist(title)
        user_aliases = tuple(
            str(a).strip()
            for a in (song.get("aliases") or [])
            if str(a).strip()
        )
        # Track-Alias automatisch: "DIKKA - Superkind" → Alias "Superkind"
        # erlaubt "spiele superkind" ohne den Artist auszusprechen.
        auto_aliases: tuple[str, ...] = ()
        if artist and track_only and track_only.lower() != title.lower():
            auto_aliases = (track_only,)

        tracks.append(Candidate(
            id=str(cid),
            name=title,
            kind="track",
            aliases=auto_aliases + user_aliases,
            content_ids=(cid,),
        ))

        if artist:
            artist_to_ids.setdefault(artist, []).append(cid)

        genre = (song.get("genre") or "").strip()
        if genre:
            genre_to_ids.setdefault(genre, []).append(cid)

    artists = [
        Candidate(
            id=f"artist:{artist}",
            name=artist,
            kind="artist",
            content_ids=tuple(ids),
        )
        for artist, ids in sorted(artist_to_ids.items(), key=lambda kv: kv[0].lower())
    ]

    genres = [
        Candidate(
            id=f"genre:{genre}",
            name=genre,
            kind="genre",
            aliases=_genre_aliases(genre),
            content_ids=tuple(ids),
        )
        for genre, ids in sorted(genre_to_ids.items(), key=lambda kv: kv[0].lower())
    ]

    return tracks + artists + genres


def _load_songs(path: Path | str) -> list[dict]:
    """Liest die ``songs``-Liste aus ``voice_catalog.json`` (best-effort).

    Fehlende oder kaputte Datei → leere Liste (kein Crash).
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("voice_catalog.json nicht lesbar: %s", e)
        return []
    return data.get("songs") or []


def build_catalog_from_file(path: Path | str) -> list[Candidate]:
    """Liest ``voice_catalog.json`` und delegiert an ``build_catalog_from_songs``.

    Fehlende oder kaputte Datei → leere Liste (best-effort, kein Crash).
    """
    return build_catalog_from_songs(_load_songs(path))


def build_title_map_from_file(path: Path | str) -> dict[int, str]:
    """``content_id → echter Einzeltitel`` aus ``voice_catalog.json``.

    Wird gebraucht, damit per-Sprache gestartete Artist-/Genre-Playlists (die
    nur ``content_ids`` sammeln) ihren Tracks den richtigen Titel geben statt
    des Kategorie-/Künstlernamens — und damit "Wie heißt dieses Lied?" den
    echten Titel ansagen kann. Gleiche Quelle wie der Catalog, also konsistent.
    """
    out: dict[int, str] = {}
    for s in _load_songs(path):
        cid = s.get("content_id")
        title = s.get("title")
        if cid and title:
            out[int(cid)] = title
    return out
