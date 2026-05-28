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

    # Playlist ruft play_fn(path, title, start_seconds) auf — start_seconds
    # ist neu (Resume-Feature), in Tests aber egal solange die Signatur passt.
    def play(self, path: Path, title: str, start_seconds: float = 0.0):
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


# ----------------------------------------------------------------------
# Wiedergabe-Historie: on_track_start / on_track_end Callbacks
# ----------------------------------------------------------------------

class CallbackRecorder:
    def __init__(self):
        self.events: list[tuple] = []

    def on_start(self, content):
        self.events.append(("start", content.content_id))

    def on_end(self, content, reason: str, position: float):
        self.events.append(("end", content.content_id, reason))


def test_callbacks_fire_on_start_and_completion(cache):
    backend = FakeBackend()
    backend.set_content(1, b"a")
    backend.set_content(2, b"b")
    player = FakePlayer()
    rec = CallbackRecorder()

    pl = Playlist(
        contents=[_make_content(1, b"a", sort=1), _make_content(2, b"b", sort=2)],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
        on_track_start=rec.on_start,
        on_track_end=rec.on_end,
    )
    pl.start()
    threading.Event().wait(0.05)  # prefetch durch
    pl.on_track_end()  # Track 1 natürlich zu Ende → Track 2 startet

    # Erwartet: start(1), end(1, completed), start(2)
    assert rec.events[0] == ("start", 1)
    assert rec.events[1] == ("end", 1, "completed")
    assert rec.events[2] == ("start", 2)


def test_next_emits_skipped_next_then_new_start(cache):
    backend = FakeBackend()
    backend.set_content(1, b"a")
    backend.set_content(2, b"b")
    player = FakePlayer()
    rec = CallbackRecorder()

    pl = Playlist(
        contents=[_make_content(1, b"a", sort=1), _make_content(2, b"b", sort=2)],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
        on_track_start=rec.on_start,
        on_track_end=rec.on_end,
    )
    pl.start()
    threading.Event().wait(0.05)
    pl.next()

    assert ("end", 1, "skipped_next") in rec.events
    # Letzter Event muss ein start(2) sein — Reihenfolge:
    # start(1), end(1, skipped_next), start(2)
    assert rec.events[-1] == ("start", 2)


def test_stop_emits_end_event_with_reason(cache):
    backend = FakeBackend()
    backend.set_content(1, b"a")
    player = FakePlayer()
    rec = CallbackRecorder()

    pl = Playlist(
        contents=[_make_content(1, b"a", sort=1)],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
        on_track_start=rec.on_start,
        on_track_end=rec.on_end,
    )
    pl.start()
    pl.stop(reason="kaka_removed")

    assert ("end", 1, "kaka_removed") in rec.events


def test_callbacks_silent_failure_does_not_break_playlist(cache):
    """Wenn ein Callback eine Exception wirft, soll die Playlist weiterlaufen."""
    backend = FakeBackend()
    backend.set_content(1, b"a")
    player = FakePlayer()

    def boom(*args, **kwargs):
        raise RuntimeError("callback boom")

    pl = Playlist(
        contents=[_make_content(1, b"a", sort=1)],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
        on_track_start=boom,
        on_track_end=boom,
    )
    assert pl.start() is True
    assert player.played[0] == cache.path_for(1)
    # stop() darf auch nicht ausbrechen
    pl.stop()
    assert player.stop_called is True


# ----------------------------------------------------------------------
# update_contents: laufende Playlist live aktualisieren (M2) + robustes
# Advancing zu einem erst später verfügbaren Track (M3).
# Regression: "3. Lied verknüpft, LED zeigt 1/3 2/3, aber 3. Track unerreichbar".
# ----------------------------------------------------------------------

def test_update_contents_adds_track_and_preserves_current(cache):
    backend = FakeBackend()
    backend.set_content(1, b"a")
    backend.set_content(2, b"b")
    backend.set_content(3, b"c")
    player = FakePlayer()

    pl = Playlist(
        contents=[_make_content(1, b"a", sort=1), _make_content(2, b"b", sort=2)],
        cache=cache,
        download_fn=backend.download,
        play_fn=player.play,
        stop_fn=player.stop,
    )
    assert pl.start() is True          # spielt Track 1
    assert pl.length == 2
    assert pl.current_index == 0

    changed = pl.update_contents([
        _make_content(1, b"a", sort=1),
        _make_content(2, b"b", sort=2),
        _make_content(3, b"c", sort=3),
    ])
    assert changed is True
    assert pl.length == 3              # LED-Zähler-Nenner steigt auf 3
    assert pl.current_index == 0       # laufender Track (id=1) bleibt erhalten


def test_update_contents_unchanged_returns_false(cache):
    backend = FakeBackend()
    backend.set_content(1, b"a")
    player = FakePlayer()
    pl = Playlist(
        contents=[_make_content(1, b"a", sort=1)],
        cache=cache, download_fn=backend.download,
        play_fn=player.play, stop_fn=player.stop,
    )
    pl.start()
    assert pl.update_contents([_make_content(1, b"a", sort=1)]) is False


def test_update_contents_remaps_index_when_earlier_track_removed(cache):
    backend = FakeBackend()
    for cid, payload in [(1, b"a"), (2, b"b"), (3, b"c")]:
        backend.set_content(cid, payload)
    player = FakePlayer()
    pl = Playlist(
        contents=[_make_content(1, b"a", 1), _make_content(2, b"b", 2), _make_content(3, b"c", 3)],
        cache=cache, download_fn=backend.download,
        play_fn=player.play, stop_fn=player.stop,
    )
    pl.start()
    threading.Event().wait(0.05)
    pl.on_track_end()                  # → Track 2 (index 1)
    assert pl.current_index == 1
    # Track 1 entfernt → laufender Track (id=2) zeigt auf neuen Index 0
    pl.update_contents([_make_content(2, b"b", 2), _make_content(3, b"c", 3)])
    assert pl.length == 2
    assert pl.current_index == 0


def test_reaches_newly_added_track_on_advance(cache):
    """Kern-Regression: 2 Tracks gecached, 3. erst nach update_contents per
    Download verfügbar — on_track_end muss bis zum 3. Track durchlaufen und ihn
    on-demand laden, statt auf Track 1 zurückzuspringen."""
    cache.path_for(1).write_bytes(b"a")
    cache.path_for(2).write_bytes(b"b")
    backend = FakeBackend()
    backend.set_content(3, b"c")       # 3. nur per Download
    player = FakePlayer()
    pl = Playlist(
        contents=[_make_content(1, b"a", 1), _make_content(2, b"b", 2)],
        cache=cache, download_fn=backend.download,
        play_fn=player.play, stop_fn=player.stop,
    )
    pl.start()
    pl.update_contents([
        _make_content(1, b"a", 1), _make_content(2, b"b", 2), _make_content(3, b"c", 3),
    ])
    threading.Event().wait(0.05)       # prefetch
    pl.on_track_end()                  # → Track 2
    assert player.played[-1] == cache.path_for(2)
    pl.on_track_end()                  # → Track 3 (on-demand geladen)
    assert player.played[-1] == cache.path_for(3)
    assert 3 in backend.calls


def test_jump_skips_unavailable_track_without_wrapping_to_first(cache):
    """M3: ein fehlender, nicht-ladbarer Track darf nicht zum stummen
    Zurückspringen auf Track 1 führen — es geht zum nächsten verfügbaren."""
    cache.path_for(1).write_bytes(b"a")
    cache.path_for(3).write_bytes(b"c")
    backend = FakeBackend()            # Track 2 nirgends → Download schlägt fehl
    player = FakePlayer()
    pl = Playlist(
        contents=[
            _make_content(1, b"a", 1),
            _make_content(2, b"b", 2),
            _make_content(3, b"c", 3),
        ],
        cache=cache, download_fn=backend.download,
        play_fn=player.play, stop_fn=player.stop,
    )
    pl.start()                         # Track 1
    threading.Event().wait(0.05)
    pl.on_track_end()                  # Track 2 fehlt → weiter auf Track 3
    assert player.played[-1] == cache.path_for(3)
    assert pl.current_index == 2
