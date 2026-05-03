"""Tests für audio.playlist.Playlist."""
import hashlib
import threading
from pathlib import Path

import pytest

from audio.cache import AudioCache
from audio.playlist import KakaContent, Playlist


def _make_content(content_id: int, hash_payload: bytes, sort: int = 0) -> KakaContent:
    return KakaContent(
        content_id=content_id,
        title=f"Track {content_id}",
        file_hash=hashlib.sha256(hash_payload).hexdigest(),
        download_url=f"http://test/dl/{content_id}",
        cached_locally=False,
        sort_order=sort,
    )


class FakeBackend:
    """Simuliert das Backend: legt korrekte Dateien an für Hash-Validierung."""
    def __init__(self):
        self.payloads: dict[int, bytes] = {}
        self.calls: list[int] = []

    def set_content(self, content_id: int, payload: bytes):
        self.payloads[content_id] = payload

    def download(self, content_id: int, target: Path) -> bool:
        self.calls.append(content_id)
        if content_id not in self.payloads:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payloads[content_id])
        return True


class FakePlayer:
    def __init__(self):
        self.played: list[Path] = []
        self.stop_called = False

    def play(self, path: Path, title: str):
        self.played.append(Path(path))

    def stop(self):
        self.stop_called = True


@pytest.fixture
def cache(tmp_path: Path) -> AudioCache:
    return AudioCache(cache_dir=tmp_path)


def test_start_plays_first_track_after_download(cache):
    backend = FakeBackend()
    player = FakePlayer()
    backend.set_content(1, b"alpha")

    pl = Playlist(
        contents=[_make_content(1, b"alpha")],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
    )
    assert pl.start() is True
    assert len(player.played) == 1
    assert player.played[0] == cache.path_for(1)


def test_start_uses_cached_file_without_download(cache):
    cache.path_for(5).write_bytes(b"cached-bytes")
    backend = FakeBackend()
    player = FakePlayer()

    pl = Playlist(
        contents=[_make_content(5, b"cached-bytes")],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
    )
    pl.start()

    # Hash matched → keine Backend-Anfrage
    assert backend.calls == []
    assert player.played[0] == cache.path_for(5)


def test_on_track_end_advances_to_next(cache):
    backend = FakeBackend()
    backend.set_content(1, b"a")
    backend.set_content(2, b"b")
    player = FakePlayer()

    pl = Playlist(
        contents=[_make_content(1, b"a", sort=1), _make_content(2, b"b", sort=2)],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
    )
    assert pl.start() is True

    # Warten bis prefetch durch ist (sehr schnell mit der FakeBackend)
    threading.Event().wait(0.05)

    pl.on_track_end()
    assert len(player.played) == 2
    assert player.played[1] == cache.path_for(2)


def test_stop_prevents_further_play(cache):
    backend = FakeBackend()
    backend.set_content(1, b"a")
    backend.set_content(2, b"b")
    player = FakePlayer()

    pl = Playlist(
        contents=[_make_content(1, b"a", sort=1), _make_content(2, b"b", sort=2)],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
    )
    pl.start()
    pl.stop()
    assert player.stop_called is True

    pl.on_track_end()  # darf nichts mehr starten
    # nur der erste Track wurde gespielt
    assert len(player.played) == 1


def test_hash_mismatch_after_download_aborts_play(cache):
    backend = FakeBackend()
    # Backend liefert "evil" — die deklarierte Hash erwartet aber "expected"
    backend.set_content(1, b"evil")

    pl = Playlist(
        contents=[_make_content(1, b"expected")],  # Hash != evil
        cache=cache,
        download_fn=backend.download,
        play_fn=lambda p, t: pytest.fail("play should not be called on hash mismatch"),
        stop_fn=lambda: None,
    )
    assert pl.start() is False
    assert not cache.path_for(1).exists()


def test_empty_playlist_returns_false(cache):
    pl = Playlist(
        contents=[],
        cache=cache,
        download_fn=lambda *_: True,
        play_fn=lambda *_: None,
        stop_fn=lambda: None,
    )
    assert pl.is_empty
    assert pl.start() is False


def test_sort_order_respected(cache):
    backend = FakeBackend()
    backend.set_content(1, b"a")
    backend.set_content(2, b"b")
    player = FakePlayer()

    pl = Playlist(
        # In falscher Reihenfolge übergeben
        contents=[_make_content(2, b"b", sort=2), _make_content(1, b"a", sort=1)],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
    )
    pl.start()
    assert player.played[0] == cache.path_for(1)
