"""Wiedergabe-Historie ans Backend melden.

Eine Box meldet abgeschlossene Wiedergaben über ``/api/box/play-session``.
Die Webapp listet die letzten 20 Einträge in der Box-Detail-View.

Lebenszyklus:
    1. ``start(content_id, kaka_id, source, used_zauberwort=...)`` öffnet
       eine Session mit jetzigem Zeitstempel.
    2. ``end(end_reason, position_seconds=...)`` schließt sie ab und legt
       sie in eine Queue.
    3. Ein Worker-Thread sendet die Queue an das Backend; Retries bei
       Transport/5xx, Drop bei 4xx (Validation/Foreign-Household —
       sinnlos zu wiederholen).
    4. Idempotenz: jede Session bekommt eine ``client_uuid``. Backend
       erkennt Wiederholungen und antwortet 200 mit ``duplicate: true``.

Offline-Resilienz: die Queue wird in eine JSON-Datei gespiegelt, damit
Reboots während Offline-Phase keine Sessions verlieren. Die aktive
``_current``-Session wird NICHT persistiert — ein Reboot mitten in der
Wiedergabe verwirft den Eintrag (es gibt keinen sauberen end_reason,
und doppelt eingespielte halbe Sessions wären verwirrend in der UI).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("kakabox.play_sessions")

# Max. Anzahl Sessions, die wir bei Offline-Phase puffern. Über diesem Wert
# fallen die ältesten Einträge raus — Anzeige zeigt eh nur die letzten 20,
# Bibliotheks-/Familien-Statistiken sind aktuell nicht geplant. Größer
# als 200 würde im Worst-Case (lange Offline-Phase + viele kurze Tracks)
# unnötig RAM/Disk fressen.
MAX_QUEUE_SIZE = 200

# Worker-Schlaf zwischen Flushes wenn die Queue leer ist.
WORKER_IDLE_SLEEP = 5.0
# Wartezeit nach einem Fehler, bevor wir die Queue noch mal probieren.
WORKER_BACKOFF_SLEEP = 30.0


def _now_iso() -> str:
    """ISO-8601 in UTC mit Z-Suffix — das Backend parsed das mit Carbon."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PlaySessionReporter:
    """Tracked die aktuell laufende Wiedergabe und schickt Endsessions weg.

    Thread-safe: ``start``/``end`` werden aus dem Main-/Voice-/NFC-Thread
    gerufen, der Sender-Thread aus einem eigenen Daemon-Worker.
    """

    def __init__(
        self,
        send_fn: Callable[[dict[str, Any]], bool],
        queue_path: Optional[Path] = None,
        max_queue_size: int = MAX_QUEUE_SIZE,
    ) -> None:
        self._send_fn = send_fn
        self._queue_path = queue_path
        self._max_queue_size = max_queue_size

        self._lock = threading.Lock()
        self._current: Optional[dict[str, Any]] = None
        self._queue: list[dict[str, Any]] = self._load_queue()

        self._stop_event = threading.Event()
        self._worker_wake = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API (vom main-Loop aufgerufen)
    # ------------------------------------------------------------------

    def start(
        self,
        content_id: int,
        kaka_id: Optional[int],
        source: str,
        used_zauberwort: Optional[bool] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Öffnet eine neue Session. Eine vorher offene wird sicherheitshalber
        mit end_reason='error' verworfen — main.py sollte ``end`` selbst
        rufen, das hier ist nur Safety-Net.
        """
        if content_id is None:
            return
        with self._lock:
            if self._current is not None:
                logger.warning(
                    "PlaySession start() obwohl schon eine läuft — alte verwerfen "
                    "(content_id=%s).", self._current.get("content_id"),
                )
            self._current = {
                "client_uuid": str(uuid.uuid4()),
                "content_id": int(content_id),
                "kaka_id": int(kaka_id) if kaka_id else None,
                "source": source,
                "used_zauberwort": used_zauberwort,
                "metadata": metadata,
                "_started_at_monotonic": time.monotonic(),
                "started_at": _now_iso(),
            }

    def end(
        self,
        end_reason: str,
        position_seconds: Optional[float] = None,
    ) -> None:
        """Schließt die aktuelle Session ab und queued sie zum Senden.

        ``position_seconds`` ist die im Track erreichte Sekunde. Wenn None,
        rechnen wir es aus Wall-Clock-Differenz aus — gut genug für die
        UI-Anzeige (Buttons skippen oft mitten im Track).
        """
        with self._lock:
            session = self._current
            self._current = None
        if session is None:
            return

        started_mono = session.pop("_started_at_monotonic", time.monotonic())
        elapsed = max(0.0, time.monotonic() - started_mono)
        if position_seconds is not None and position_seconds >= 0:
            duration = int(position_seconds)
        else:
            duration = int(elapsed)
        # 1-Sekunden-Sessions als "kaka_removed sofort wieder runter" sind
        # die häufigsten Spam-Sessions. Wir behalten sie trotzdem — die UI
        # filtert nicht und Eltern wollen evtl. sehen, dass das Kind den
        # Chip nur kurz aufgelegt hat.

        payload = {
            "client_uuid": session["client_uuid"],
            "content_id": session["content_id"],
            "kaka_id": session.get("kaka_id"),
            "source": session["source"],
            "started_at": session["started_at"],
            "ended_at": _now_iso(),
            "duration_seconds": duration,
            "end_reason": end_reason,
        }
        if session.get("used_zauberwort") is not None:
            payload["used_zauberwort"] = bool(session["used_zauberwort"])
        if session.get("metadata"):
            payload["metadata"] = session["metadata"]

        with self._lock:
            self._queue.append(payload)
            # Überlauf am ältesten Ende kappen.
            if len(self._queue) > self._max_queue_size:
                drop = len(self._queue) - self._max_queue_size
                logger.warning(
                    "PlaySession-Queue overflow — %d älteste Einträge verworfen.",
                    drop,
                )
                self._queue = self._queue[drop:]
            self._persist_queue_locked()

        self._worker_wake.set()

    def cancel(self) -> None:
        """Verwirft die offene Session ohne sie zu reporten — z.B. wenn
        sofort eine neue Wiedergabe übernimmt und main.py keinen eindeutigen
        end_reason hat. Selten gebraucht, vorhanden für Special-Cases.
        """
        with self._lock:
            self._current = None

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def start_worker(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="play-session-reporter",
        )
        self._worker_thread.start()

    def stop_worker(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        self._worker_wake.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=timeout)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            sent_any = self._flush_once()
            if sent_any:
                # Sofort die nächste Session probieren.
                continue
            with self._lock:
                empty = not self._queue
            sleep = WORKER_IDLE_SLEEP if empty else WORKER_BACKOFF_SLEEP
            self._worker_wake.wait(timeout=sleep)
            self._worker_wake.clear()

    def _flush_once(self) -> bool:
        """Versucht den ältesten Eintrag zu senden. Gibt True zurück wenn
        einer erfolgreich ging — der Worker kann dann sofort weitermachen.
        """
        with self._lock:
            if not self._queue:
                return False
            payload = self._queue[0]

        try:
            ok = self._send_fn(payload)
        except Exception as e:
            logger.warning("PlaySessionReporter.send_fn warf %s — Retry später.", e)
            ok = False

        if not ok:
            return False

        with self._lock:
            # In der Zwischenzeit könnte ein anderer Eintrag dazugekommen
            # sein — wir entfernen den, dessen UUID wir gerade gesendet
            # haben, nicht stumpf den ersten.
            self._queue = [s for s in self._queue if s.get("client_uuid") != payload.get("client_uuid")]
            self._persist_queue_locked()
        return True

    # ------------------------------------------------------------------
    # Persistenz
    # ------------------------------------------------------------------

    def _load_queue(self) -> list[dict[str, Any]]:
        if not self._queue_path or not self._queue_path.exists():
            return []
        try:
            data = json.loads(self._queue_path.read_text())
        except (ValueError, OSError) as e:
            logger.warning("PlaySession-Queue %s nicht lesbar: %s", self._queue_path, e)
            return []
        if not isinstance(data, list):
            return []
        # Defensive: nur Dicts behalten.
        return [item for item in data if isinstance(item, dict)]

    def _persist_queue_locked(self) -> None:
        """Schreibt die Queue. Caller MUSS self._lock halten."""
        if not self._queue_path:
            return
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._queue_path.with_suffix(self._queue_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._queue, ensure_ascii=False))
            tmp.replace(self._queue_path)
        except OSError as e:
            # Disk voll oder read-only — Queue läuft halt nur in-memory weiter.
            logger.warning("PlaySession-Queue konnte nicht gesichert werden: %s", e)
