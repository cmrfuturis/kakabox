"""Tests für audio.cache.AudioCache."""
import hashlib
import threading
import time
from pathlib import Path

import pytest

from audio.cache import AudioCache


@pytest.fixture
def cache(tmp_path: Path) -> AudioCache:
    return AudioCache(cache_dir=tmp_path)


def test_path_for_is_deterministic(cache):
    assert cache.path_for(42) == cache.dir / "42.mp3"
    assert cache.path_for(42) == cache.path_for(42)


def test_is_cached_returns_false_when_missing(cache):
    assert cache.is_cached(99) is False


def test_is_cached_returns_true_when_file_exists(cache):
    p = cache.path_for(1)
    p.write_bytes(b"hello")
    assert cache.is_cached(1) is True


def test_is_cached_validates_hash_when_provided(cache):
    p = cache.path_for(7)
    p.write_bytes(b"abc")
    correct = hashlib.sha256(b"abc").hexdigest()

    assert cache.is_cached(7, expected_hash=correct) is True
    assert cache.is_cached(7, expected_hash="x" * 64) is False


def test_compute_hash_matches_known_value(cache):
    p = cache.path_for(3)
    p.write_bytes(b"deterministic")
    expected = hashlib.sha256(b"deterministic").hexdigest()
    assert cache.compute_hash(p) == expected


def test_cleanup_keeps_only_listed_ids(cache):
    cache.path_for(1).write_bytes(b"keep")
    cache.path_for(2).write_bytes(b"drop")
    cache.path_for(3).write_bytes(b"keep")
    (cache.dir / "garbage.txt").write_bytes(b"ignore")

    deleted = cache.cleanup(keep_content_ids=[1, 3])

    assert deleted == 1
    assert cache.path_for(1).exists()
    assert not cache.path_for(2).exists()
    assert cache.path_for(3).exists()
    assert (cache.dir / "garbage.txt").exists()  # nicht-mp3 wird ignoriert


def test_storage_stats_returns_positive(cache):
    total, free = cache.storage_stats_mb()
    assert total > 0
    assert 0 <= free <= total


def test_download_guard_serializes_same_content_id(cache):
    """Regression für den P0-Fix vom 2026-07-07: der Audio-Sync-Loop und die
    Playlist-Prefetch-Threads dürfen denselben Content nie parallel
    herunterladen (Race auf denselben .part-Tempfile)."""
    order: list[str] = []
    started = threading.Event()

    def first():
        with cache.download_guard(42):
            order.append("first-enter")
            started.set()
            time.sleep(0.1)
            order.append("first-exit")

    def second():
        started.wait()
        with cache.download_guard(42):
            order.append("second-enter")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert order == ["first-enter", "first-exit", "second-enter"]


def test_download_guard_does_not_serialize_different_content_ids(cache):
    """Downloads verschiedener content_ids dürfen sich nicht gegenseitig blockieren."""
    entered: list[int] = []
    barrier = threading.Barrier(2, timeout=2)

    def worker(content_id):
        with cache.download_guard(content_id):
            entered.append(content_id)
            barrier.wait()  # blockiert nur, wenn BEIDE den Lock gleichzeitig halten

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not t1.is_alive() and not t2.is_alive()
    assert sorted(entered) == [1, 2]
