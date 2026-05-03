"""Spielt Inhalte einer Kaka in Reihenfolge mit progressivem Vorab-Download.

Wenn der Tag aufgelegt wird, bekommt das Device die Liste der Contents inklusive
Download-URL und Hash. Inhalte, die schon lokal liegen, starten sofort; fehlende
werden im Hintergrund geladen, sodass der nächste Track meist fertig ist, wenn
der vorherige zu Ende läuft.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .cache import AudioCache

logger = logging.getLogger("kakabox.playlist")

# Wenn beim Druck auf "Zurück" der aktuelle Track schon länger als so viele
# Sekunden läuft, wird er auf 0:00 gesprungen statt zum vorigen Track.
TRACK_RESTART_THRESHOLD_S = 5.0


@dataclass
class KakaContent:
    """Item aus einer Tag-Scan-Antwort."""
    content_id: int
    title: str
    file_hash: Optional[str]
    download_url: Optional[str]
    cached_locally: bool
    sort_order: int = 0


@dataclass
class PlaylistSnapshot:
    """Zustand zum Speichern für Resume-on-Replace."""
    track_index: int
    position_seconds: float


class Playlist:
    """Sequentielle Wiedergabe einer Kaka mit Hintergrund-Vorabladung.

    Lebenszyklus:
        1. ``start(start_index, start_position)`` lädt den passenden Track,
           startet Wiedergabe und prefetched die restlichen.
        2. Beim Track-Ende ruft der Player ``on_track_end()`` auf — wir gehen
           zum nächsten Track (mit Wraparound am Ende).
        3. ``stop()`` bricht alles ab (Player + Background-Thread).

    Steuerung von außen (Buttons):
        ``next()``         → nächster Track, mit Loop am Ende
        ``previous()``     → vorheriger Track, mit Loop am Anfang;
                             wenn aktueller Track > TRACK_RESTART_THRESHOLD_S
                             läuft → stattdessen Neustart auf 0:00.
        ``toggle_pause()`` → an Player delegieren (nicht hier)
    """

    def __init__(
        self,
        contents: list[KakaContent],
        cache: AudioCache,
        download_fn: Callable[[int, Path], bool],
        play_fn: Callable[..., None],   # play_fn(path, title, start_seconds=0)
        stop_fn: Callable[[], None],
        position_fn: Optional[Callable[[], float]] = None,
        seek_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        # Sortiere nach sort_order, damit die Reihenfolge stabil ist.
        self._contents = sorted(contents, key=lambda c: c.sort_order)
        self._cache = cache
        self._download_fn = download_fn
        self._play_fn = play_fn
        self._stop_fn = stop_fn
        self._position_fn = position_fn  # liefert aktuelle Sekunde im Track
        self._seek_fn = seek_fn          # springt im aktuellen Track

        self._index = -1
        self._stopped = threading.Event()
        self._download_thread: Optional[threading.Thread] = None

    @property
    def is_empty(self) -> bool:
        return not self._contents

    @property
    def current_index(self) -> int:
        return self._index

    @property
    def length(self) -> int:
        return len(self._contents)

    def snapshot(self) -> Optional[PlaylistSnapshot]:
        """Zustand zum späteren Wiederaufnehmen festhalten (Tag wurde abgenommen)."""
        if self._index < 0:
            return None
        pos = 0.0
        if self._position_fn:
            try:
                pos = float(self._position_fn() or 0.0)
            except (TypeError, ValueError):
                pos = 0.0
        return PlaylistSnapshot(track_index=self._index, position_seconds=pos)

    # ------------------------------------------------------------------
    # Wiedergabe-Steuerung
    # ------------------------------------------------------------------

    def start(self, start_index: int = 0, start_position: float = 0.0) -> bool:
        """Block bis Start-Track verfügbar, starte Wiedergabe, prefetche Rest."""
        if self.is_empty:
            logger.warning("Playlist leer — nichts zu spielen.")
            return False

        if not (0 <= start_index < len(self._contents)):
            start_index = 0

        first = self._contents[start_index]
        path = self._ensure_local(first)
        if path is None:
            logger.error("Kann Start-Track '%s' nicht laden.", first.title)
            return False

        self._index = start_index
        self._play(first, path, start_seconds=start_position)

        if len(self._contents) > 1:
            self._download_thread = threading.Thread(
                target=self._prefetch_rest, daemon=True, name="playlist-prefetch"
            )
            self._download_thread.start()

        return True

    def on_track_end(self) -> None:
        """Vom Player aufgerufen, wenn der aktuelle Track durchgelaufen ist.

        Default-Verhalten: zum nächsten Track. Am Ende der Playlist NICHT loopen
        (anders als beim manuellen Knopfdruck) — damit nicht endlos durchspielt
        wird. Soll-Verhalten könnte per Flag konfigurierbar werden.
        """
        if self._stopped.is_set():
            return

        next_index = self._index + 1
        if next_index >= len(self._contents):
            logger.info("Playlist beendet.")
            return

        self._jump_to(next_index)

    # Manuelle Bedienung via Buttons -----------------------------------

    def next(self) -> None:
        """Nächster Track. Am Ende → Wraparound zum ersten."""
        if self._stopped.is_set() or self.is_empty:
            return
        target = (self._index + 1) % len(self._contents)
        self._jump_to(target)

    def previous(self) -> None:
        """Wenn aktueller Track > 5 s läuft → Neustart, sonst voriger Track.
        Am Anfang → Wraparound zum letzten Track.
        """
        if self._stopped.is_set() or self.is_empty:
            return

        # Zuerst prüfen: läuft schon länger als die Schwelle?
        pos = 0.0
        if self._position_fn:
            try:
                pos = float(self._position_fn() or 0.0)
            except (TypeError, ValueError):
                pos = 0.0

        if pos >= TRACK_RESTART_THRESHOLD_S:
            logger.info("Track läuft seit %.1fs — Neustart.", pos)
            if self._seek_fn:
                self._seek_fn(0.0)
            else:
                # Fallback: Track neu starten via _play
                cur = self._contents[self._index]
                path = self._cache.path_for(cur.content_id)
                if path.exists():
                    self._play(cur, path)
            return

        target = (self._index - 1) % len(self._contents)
        self._jump_to(target)

    def stop(self) -> None:
        self._stopped.set()
        self._stop_fn()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _jump_to(self, index: int) -> None:
        if not (0 <= index < len(self._contents)):
            return
        nxt = self._contents[index]
        path = self._ensure_local(nxt, blocking=True)
        if path is None:
            logger.error("Track '%s' nicht verfügbar — überspringe.", nxt.title)
            # Bei nicht-verfügbarem Track einen weiter, max einmal
            if index != self._index:
                self._index = index
                self.next()
            return
        self._index = index
        self._play(nxt, path)

    def _play(self, content: KakaContent, path: Path, start_seconds: float = 0.0) -> None:
        logger.info(
            "Spiele [%d/%d]: %s%s",
            self._index + 1, len(self._contents), content.title,
            f" (ab {start_seconds:.1f}s)" if start_seconds > 0 else "",
        )
        self._play_fn(path, content.title, start_seconds)

    def _ensure_local(self, content: KakaContent, blocking: bool = True) -> Path | None:
        """Stellt sicher, dass der Content lokal vorliegt. Lädt notfalls runter."""
        if self._stopped.is_set():
            return None

        path = self._cache.path_for(content.content_id)
        if self._cache.is_cached(content.content_id, content.file_hash):
            return path

        if not content.download_url:
            logger.warning("Kein download_url für '%s'", content.title)
            return None

        if not blocking:
            return None

        logger.info("Lade '%s' (id=%d) ...", content.title, content.content_id)
        ok = self._download_fn(content.content_id, path)
        if not ok:
            return None
        if content.file_hash and self._cache.compute_hash(path) != content.file_hash:
            logger.error(
                "Hash-Mismatch nach Download für content=%d — Datei verworfen",
                content.content_id,
            )
            path.unlink(missing_ok=True)
            return None
        return path

    def _prefetch_rest(self) -> None:
        """Lädt die restlichen Tracks der Reihe nach im Hintergrund."""
        for content in self._contents:
            if self._stopped.is_set():
                return
            self._ensure_local(content, blocking=True)
