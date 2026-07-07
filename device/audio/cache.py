"""Lokaler Audio-Cache für vom Backend bezogene MP3-Dateien.

- Layout: ``<cache_dir>/<content_id>.mp3``
- Hash-Verifikation: Vor dem Markieren als "vorhanden" wird sha256 geprüft.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

logger = logging.getLogger("kakabox.cache")

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "audio_cache"


class AudioCache:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self.dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._download_locks_guard = threading.Lock()
        self._download_locks: dict[int, threading.Lock] = {}

    @contextmanager
    def download_guard(self, content_id: int) -> Iterator[None]:
        """Serialisiert Downloads für dieselbe content_id.

        Der Audio-Sync-Loop (main.py) und die Playlist-Prefetch-Threads können
        denselben Content unabhängig voneinander als "fehlt lokal" erkennen und
        beide einen Download starten. Ohne Koordination schreiben beide auf
        denselben deterministischen ``.part``-Tempfile (siehe
        ``Backend.download_audio``) — die Datei wird dadurch korrupt, der
        SHA-256-Check danach schlägt fehl und der Song landet in der Backoff-
        Sperre. Dieser Context-Manager stellt sicher, dass pro content_id
        immer nur ein Download gleichzeitig läuft; Aufrufer sollten nach dem
        Erwerb erneut ``is_cached()`` prüfen, da der wartende zweite Aufrufer
        die Datei oft schon fertig geladen vorfindet.
        """
        with self._download_locks_guard:
            lock = self._download_locks.setdefault(content_id, threading.Lock())
        with lock:
            yield

    def path_for(self, content_id: int) -> Path:
        return self.dir / f"{content_id}.mp3"

    def is_cached(self, content_id: int, expected_hash: str | None = None) -> bool:
        path = self.path_for(content_id)
        if not path.exists():
            return False
        if expected_hash and self.compute_hash(path) != expected_hash:
            return False
        return True

    @staticmethod
    def compute_hash(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(64 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def find_by_hash(self, expected_hash: str) -> Path | None:
        """Sucht eine bereits gecachte Datei mit dem gegebenen sha256-Hash.

        Genutzt vom Sync-Loop: bevor ein neuer Download startet, prüfen ob
        derselbe Inhalt schon unter einer anderen ``content_id`` liegt
        (Backend-Dublette: gleicher Hash, andere ID). In dem Fall einfach
        per Hardlink "duplizieren" — kein zweiter Download, kein doppelter
        Speicherplatz.
        """
        if not expected_hash:
            return None
        for f in self.dir.iterdir():
            if not f.is_file() or f.suffix != ".mp3":
                continue
            try:
                if self.compute_hash(f) == expected_hash:
                    return f
            except OSError:
                continue
        return None

    def link_from(self, source: Path, content_id: int) -> bool:
        """Erzeugt path_for(content_id) als Hardlink auf ``source``. Returns
        True bei Erfolg. Falls Hardlink scheitert (z.B. cross-filesystem),
        fallback auf copy."""
        target = self.path_for(content_id)
        if target.exists():
            return True
        try:
            os.link(source, target)
            return True
        except OSError:
            try:
                shutil.copy2(source, target)
                return True
            except OSError as e:
                logger.warning("link_from(%s → %s) failed: %s", source, target, e)
                return False

    def cleanup(self, keep_content_ids: Iterable[int]) -> int:
        """Entfernt Dateien, die nicht mehr in keep_content_ids sind. Returns: Anzahl gelöscht."""
        keep = {int(cid) for cid in keep_content_ids}
        deleted = 0
        for f in self.dir.iterdir():
            if not f.is_file() or f.suffix != ".mp3":
                continue
            try:
                cid = int(f.stem)
            except ValueError:
                continue
            if cid not in keep:
                f.unlink(missing_ok=True)
                deleted += 1
        return deleted

    def storage_stats_mb(self) -> tuple[int, int]:
        """Returns (total_mb, free_mb) auf dem Volume des Cache-Verzeichnisses."""
        usage = shutil.disk_usage(self.dir)
        return usage.total // (1024 * 1024), usage.free // (1024 * 1024)
