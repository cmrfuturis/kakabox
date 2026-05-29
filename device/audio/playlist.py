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
        on_track_start: Optional[Callable[["KakaContent"], None]] = None,
        on_track_end: Optional[Callable[["KakaContent", str, float], None]] = None,
    ) -> None:
        # Sortiere nach sort_order, damit die Reihenfolge stabil ist.
        self._contents = sorted(contents, key=lambda c: c.sort_order)
        self._cache = cache
        self._download_fn = download_fn
        self._play_fn = play_fn
        self._stop_fn = stop_fn
        self._position_fn = position_fn  # liefert aktuelle Sekunde im Track
        self._seek_fn = seek_fn          # springt im aktuellen Track
        # Lifecycle-Callbacks — werden von der Box für die Wiedergabe-Historie
        # genutzt. Beide optional; bei None ist die Playlist still.
        # on_track_end-Signatur: (content, end_reason, position_seconds)
        # end_reason ∈ {"completed", "skipped_next", "skipped_back", "stopped"}.
        self._on_track_start = on_track_start
        self._on_track_end = on_track_end

        self._index = -1
        self._stopped = threading.Event()
        self._download_thread: Optional[threading.Thread] = None
        # Schützt den atomaren Swap von _contents/_index in update_contents()
        # gegen gleichzeitige Navigation (next/previous/on_track_end laufen aus
        # Button-/Player-Threads, update_contents aus dem Sync-Thread).
        self._update_lock = threading.Lock()

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

    def update_contents(self, new_contents: list[KakaContent]) -> bool:
        """Aktualisiert die Track-Liste einer LAUFENDEN Playlist.

        Wird vom Audio-Sync / Tag-Refresh aufgerufen, wenn sich der Inhalt der
        gerade aktiven Kaka geändert hat (neues Lied verknüpft, eines entfernt)
        oder ein zuvor fehlendes Lied nachgeladen wurde. Der aktuell spielende
        Track bleibt erhalten (über content_id wiedergefunden); neue Tracks
        werden über _ensure_local beim Erreichen erreichbar, entfernte fallen
        raus. Frische download_url/file_hash werden ebenfalls übernommen.

        Gibt True zurück, wenn sich Menge oder Reihenfolge der Tracks geändert
        hat — dann muss der Aufrufer z.B. die LED-Track-Anzeige neu setzen.
        Das gerade laufende Audio wird NICHT unterbrochen.
        """
        if self._stopped.is_set():
            return False

        new_sorted = sorted(new_contents, key=lambda c: c.sort_order)
        with self._update_lock:
            old_ids = [c.content_id for c in self._contents]
            new_ids = [c.content_id for c in new_sorted]
            current = self._current_content()
            self._contents = new_sorted
            # Index auf den weiterhin laufenden Track neu ausrichten.
            if current is not None:
                new_idx = next(
                    (i for i, c in enumerate(new_sorted)
                     if c.content_id == current.content_id),
                    None,
                )
                if new_idx is not None:
                    self._index = new_idx
                else:
                    # Laufender Track wurde entfernt — Index defensiv klemmen,
                    # damit on_track_end nicht ins Leere greift.
                    self._index = min(self._index, len(new_sorted) - 1)
            changed = old_ids != new_ids

        if changed:
            logger.info(
                "Playlist live aktualisiert: %d → %d Tracks.",
                len(old_ids), len(new_ids),
            )
            # Neu hinzugekommene Tracks im Hintergrund vorab laden, damit sie
            # beim Erreichen sofort spielen statt erst beim Jump blockierend.
            if not self.is_empty and not self._stopped.is_set():
                threading.Thread(
                    target=self._prefetch_rest, daemon=True, name="playlist-refetch",
                ).start()
        return changed

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

        # Aktuelle Session sauber als "completed" abschließen, bevor wir den
        # nächsten Track starten — der Reporter braucht das, sonst stapeln
        # sich offene Sessions.
        self._emit_track_end("completed")

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
        self._emit_track_end("skipped_next")
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
            # Restart auf 0:00 zählt als "skipped_back" für die Wiedergabe-
            # Historie: aus User-Sicht ein Reset des aktuellen Titels.
            self._emit_track_end("skipped_back")
            if self._seek_fn:
                self._seek_fn(0.0)
                # Seek startet den Track neu — wir markieren das als neuen
                # Session-Start, damit die Webapp zwei Einträge zeigt.
                cur = self._contents[self._index]
                self._emit_track_start(cur)
            else:
                # Fallback: Track neu starten via _play
                cur = self._contents[self._index]
                path = self._cache.path_for(cur.content_id)
                if path.exists():
                    self._play(cur, path)
            return

        self._emit_track_end("skipped_back")
        target = (self._index - 1) % len(self._contents)
        self._jump_to(target)

    def stop(self, reason: str = "stopped") -> None:
        # Wenn die Playlist während laufender Wiedergabe abgebrochen wird,
        # melden wir der Box ein End-Event — sonst geht die Session in der
        # Webapp-Historie verloren. Caller können den Grund konkretisieren
        # (z. B. "kaka_removed" beim Abnehmen der Figur), Default ist der
        # generische "stopped"-Fall (Box wird heruntergefahren, neuer
        # Wiedergabe-Modus übernimmt etc.).
        self._emit_track_end(reason)
        self._stopped.set()
        self._stop_fn()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _jump_to(self, index: int) -> None:
        # Snapshot der Liste — update_contents() könnte parallel swappen.
        contents = self._contents
        n = len(contents)
        if n == 0 or not (0 <= index < n):
            return
        # Ab `index` den nächsten TATSÄCHLICH verfügbaren Track suchen und bei
        # Bedarf on-demand laden. Maximal einmal rundherum, damit ein fehlendes
        # Lied (z.B. noch nicht fertig gesynct) weder eine Endlosschleife noch
        # ein stummes Zurückspringen auf Track 1 auslöst. _ensure_local lädt
        # blockierend nach — so wird ein neu verknüpftes 3. Lied beim Erreichen
        # geladen statt übersprungen.
        for offset in range(n):
            i = (index + offset) % n
            nxt = contents[i]
            path = self._ensure_local(nxt, blocking=True)
            if path is not None:
                self._index = i
                self._play(nxt, path)
                return
            logger.warning(
                "Track '%s' (#%d) nicht verfügbar — versuche nächsten.",
                nxt.title, i + 1,
            )
        logger.error("Kein Track der Playlist verfügbar — Wiedergabe pausiert.")

    def _play(self, content: KakaContent, path: Path, start_seconds: float = 0.0) -> None:
        logger.info(
            "Spiele [%d/%d]: %s%s",
            self._index + 1, len(self._contents), content.title,
            f" (ab {start_seconds:.1f}s)" if start_seconds > 0 else "",
        )
        self._play_fn(path, content.title, start_seconds)
        self._emit_track_start(content)

    # ------------------------------------------------------------------
    # Lifecycle-Callbacks (no-op wenn keine registriert)
    # ------------------------------------------------------------------

    def _current_position(self) -> float:
        if not self._position_fn:
            return 0.0
        try:
            return float(self._position_fn() or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _current_content(self) -> Optional[KakaContent]:
        if 0 <= self._index < len(self._contents):
            return self._contents[self._index]
        return None

    def current_title(self) -> Optional[str]:
        """Titel des gerade laufenden Tracks (oder None, wenn nichts läuft).

        Public-Getter für die Voice-Frage "Wie heißt dieses Lied?".
        """
        content = self._current_content()
        return content.title if content else None

    def contents_snapshot(self) -> list:
        """Kopie der aktuellen Track-Liste (in Abspiel-Reihenfolge).

        Ermöglicht ein exaktes "an dieser Stelle fortsetzen": dieselbe Liste +
        ``current_index`` rekonstruiert die identische Playlist (z.B. nach der
        Titel-Frage, damit dasselbe Lied weiterläuft statt neu zu mischen).
        """
        return list(self._contents)

    def _emit_track_start(self, content: KakaContent) -> None:
        if self._on_track_start is None:
            return
        try:
            self._on_track_start(content)
        except Exception as e:
            # Callback-Fehler dürfen die Wiedergabe nicht abreißen.
            logger.warning("on_track_start callback warf %s — ignoriert.", e)

    def _emit_track_end(self, end_reason: str) -> None:
        if self._on_track_end is None:
            return
        content = self._current_content()
        if content is None:
            return
        pos = self._current_position()
        try:
            self._on_track_end(content, end_reason, pos)
        except Exception as e:
            logger.warning("on_track_end callback warf %s — ignoriert.", e)

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
