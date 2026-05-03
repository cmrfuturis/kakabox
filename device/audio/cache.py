"""Lokaler Audio-Cache für vom Backend bezogene MP3-Dateien.

- Layout: ``<cache_dir>/<content_id>.mp3``
- Hash-Verifikation: Vor dem Markieren als "vorhanden" wird sha256 geprüft.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("kakabox.cache")

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "audio_cache"


class AudioCache:
    def __init__(self, cache_dir: Path | None = None) -> None:
        self.dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)

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
