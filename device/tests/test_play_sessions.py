"""Tests für network.play_sessions.PlaySessionReporter.

Worker-Thread wird in diesen Tests bewusst NICHT gestartet — wir testen die
synchrone Logik (start/end/queue/flush) und rufen _flush_once direkt auf.
Das hält Tests deterministisch und vermeidet Sleeps.
"""
import json
import threading
from pathlib import Path

import pytest

from network.play_sessions import PlaySessionReporter


class CapturingSender:
    """Sammelt alle Payloads und kann konfiguriert werden, beim n-ten Call
    fehlzuschlagen — testet den Retry-Pfad.
    """

    def __init__(self, ok_after: int = 0):
        self.calls: list[dict] = []
        self._ok_after = ok_after

    def __call__(self, payload: dict) -> bool:
        self.calls.append(payload)
        if len(self.calls) <= self._ok_after:
            return False
        return True


@pytest.fixture
def queue_path(tmp_path: Path) -> Path:
    return tmp_path / "play_sessions.json"


def test_start_then_end_queues_complete_payload(queue_path):
    sender = CapturingSender()
    reporter = PlaySessionReporter(send_fn=sender, queue_path=queue_path)

    reporter.start(content_id=42, kaka_id=7, source="kaka")
    reporter.end(end_reason="completed", position_seconds=120.0)

    assert reporter._flush_once() is True
    assert len(sender.calls) == 1
    sent = sender.calls[0]
    assert sent["content_id"] == 42
    assert sent["kaka_id"] == 7
    assert sent["source"] == "kaka"
    assert sent["end_reason"] == "completed"
    assert sent["duration_seconds"] == 120
    assert sent["started_at"].endswith("Z")
    assert sent["ended_at"].endswith("Z")
    # Idempotenz: client_uuid muss gesetzt sein
    assert sent["client_uuid"]


def test_end_without_start_is_noop(queue_path):
    sender = CapturingSender()
    reporter = PlaySessionReporter(send_fn=sender, queue_path=queue_path)
    reporter.end(end_reason="stopped")
    assert reporter._flush_once() is False
    assert sender.calls == []


def test_start_during_open_session_replaces_silently(queue_path):
    """start() während eine Session noch offen ist verwirft die alte —
    main.py sollte das Vermeiden, aber wir wollen den Reporter robust
    halten gegen verpasste end()-Aufrufe."""
    sender = CapturingSender()
    reporter = PlaySessionReporter(send_fn=sender, queue_path=queue_path)
    reporter.start(content_id=1, kaka_id=None, source="manual")
    reporter.start(content_id=2, kaka_id=None, source="manual")
    reporter.end(end_reason="completed", position_seconds=10.0)

    assert reporter._flush_once() is True
    assert sender.calls[0]["content_id"] == 2  # die neuere Session siegt


def test_failed_send_keeps_payload_in_queue_for_retry(queue_path):
    sender = CapturingSender(ok_after=1)  # erster Call fail, zweiter OK
    reporter = PlaySessionReporter(send_fn=sender, queue_path=queue_path)

    reporter.start(content_id=5, kaka_id=None, source="manual")
    reporter.end(end_reason="completed", position_seconds=30.0)

    assert reporter._flush_once() is False
    # Payload muss noch in Queue + auf Disk persistiert sein.
    assert len(reporter._queue) == 1
    saved = json.loads(queue_path.read_text())
    assert len(saved) == 1
    assert saved[0]["content_id"] == 5

    # Zweiter Versuch klappt.
    assert reporter._flush_once() is True
    assert reporter._queue == []
    assert json.loads(queue_path.read_text()) == []


def test_queue_persists_across_reporter_instances(queue_path):
    """Reboot-Simulation: zweite Reporter-Instanz liest die Queue von Disk
    und sendet sie weg."""
    sender = CapturingSender(ok_after=99)  # keiner ok
    reporter_a = PlaySessionReporter(send_fn=sender, queue_path=queue_path)
    reporter_a.start(content_id=11, kaka_id=None, source="voice")
    reporter_a.end(end_reason="stopped", position_seconds=5.0)
    reporter_a._flush_once()  # fail

    # Neue Instanz — wie nach Reboot
    sender_b = CapturingSender()
    reporter_b = PlaySessionReporter(send_fn=sender_b, queue_path=queue_path)
    assert len(reporter_b._queue) == 1
    assert reporter_b._flush_once() is True
    assert sender_b.calls[0]["content_id"] == 11


def test_queue_overflow_drops_oldest(queue_path):
    sender = CapturingSender(ok_after=999)  # alle fail
    reporter = PlaySessionReporter(
        send_fn=sender, queue_path=queue_path, max_queue_size=3,
    )
    for cid in range(5):
        reporter.start(content_id=cid, kaka_id=None, source="manual")
        reporter.end(end_reason="completed", position_seconds=1.0)

    assert len(reporter._queue) == 3
    cids = [s["content_id"] for s in reporter._queue]
    assert cids == [2, 3, 4]  # 0 und 1 sind rausgekippt


def test_voice_source_carries_used_zauberwort(queue_path):
    sender = CapturingSender()
    reporter = PlaySessionReporter(send_fn=sender, queue_path=queue_path)
    reporter.start(content_id=8, kaka_id=None, source="voice", used_zauberwort=True)
    reporter.end(end_reason="completed")
    reporter._flush_once()
    assert sender.calls[0]["used_zauberwort"] is True


def test_used_zauberwort_omitted_when_none(queue_path):
    sender = CapturingSender()
    reporter = PlaySessionReporter(send_fn=sender, queue_path=queue_path)
    reporter.start(content_id=8, kaka_id=4, source="kaka", used_zauberwort=None)
    reporter.end(end_reason="completed")
    reporter._flush_once()
    # Bei source=kaka schickt die Box used_zauberwort gar nicht erst mit,
    # damit das Backend-Schema (nullable) nicht überdeklariert wird.
    assert "used_zauberwort" not in sender.calls[0]


def test_position_fallback_to_wallclock(queue_path):
    """Wenn die Box keine Position liefert (z. B. Player-Bug), fällt
    duration_seconds auf die Wall-Clock-Differenz zurück."""
    sender = CapturingSender()
    reporter = PlaySessionReporter(send_fn=sender, queue_path=queue_path)
    reporter.start(content_id=1, kaka_id=None, source="manual")
    # Kurz schlafen, dann end ohne explizite Position
    threading.Event().wait(0.05)
    reporter.end(end_reason="stopped")
    reporter._flush_once()
    # duration ≥ 0, aber kein riesiger Wert — irgendwas zwischen 0 und ~1s
    assert 0 <= sender.calls[0]["duration_seconds"] <= 2


def test_cancel_drops_open_session_without_reporting(queue_path):
    sender = CapturingSender()
    reporter = PlaySessionReporter(send_fn=sender, queue_path=queue_path)
    reporter.start(content_id=99, kaka_id=None, source="manual")
    reporter.cancel()
    reporter.end(end_reason="stopped")
    # cancel hat _current geleert, daher end() ist no-op
    assert reporter._queue == []
