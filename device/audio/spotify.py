"""Spotify-Anbindung über den lokalen go-librespot-Daemon.

Die Box streamt Spotify nicht selbst — das macht go-librespot
(https://github.com/devgianlu/go-librespot) als eigener systemd-Service
(``go-librespot.service``), der bei Spotify als Connect-Gerät "kakabox"
auftritt. Dieses Modul ist der dünne Client für dessen REST-API auf
localhost. Vorteil gegenüber der offiziellen Spotify Web API: keine
Developer-App, kein Cloud-Roundtrip — Play/Pause ist ein lokaler
HTTP-Call mit einstelliger Millisekunden-Latenz.

Bedienmodell (Spotify-Chip, siehe config "spotify"):
- Chip aufgelegt  → ``turn_on()``: liegt ein Wiedergabe-Kontext im Player
  (pausiert nach Chip-Runter) → resume. Ist der Player leer (frisch
  gebootet, nie benutzt) → konfigurierte Default-Playlist ("spotify.uri"),
  falls gesetzt.
- Chip entfernt   → ``pause()``.
Musik auswählen können die Eltern jederzeit über die Spotify-App mit dem
Box-Konto — die Box erscheint dort als Gerät "kakabox"; der Chip bleibt
der An/Aus-Schalter.

Setup/Betrieb des Daemons: siehe device/setup/spotify/README.md.
"""

import logging
import re
import threading
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:3678"

# Lokaler Daemon: connect darf praktisch nicht dauern; read etwas großzügiger,
# weil /player/play das Laden des Kontexts abwartet.
_TIMEOUT = (0.5, 3.0)

# https://open.spotify.com/playlist/<id>?si=… → ("playlist", "<id>")
_OPEN_URL_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]+/)?"
    r"(playlist|album|track|artist|show|episode)/([A-Za-z0-9]+)"
)
_URI_RE = re.compile(
    r"^spotify:(?:playlist|album|track|artist|show|episode):[A-Za-z0-9]+$"
)


def normalize_spotify_uri(value: Optional[str]) -> Optional[str]:
    """Akzeptiert spotify:-URIs UND open.spotify.com-Links (wie man sie aus
    der App per "Teilen" kopiert) und liefert immer eine spotify:-URI.
    Unbrauchbare Werte → None (mit Log), damit ein Tippfehler in der config
    die Chip-Funktion nicht komplett lahmlegt, sondern nur die Default-
    Playlist fehlt."""
    if not value:
        return None
    value = value.strip()
    if _URI_RE.match(value):
        return value
    m = _OPEN_URL_RE.search(value)
    if m:
        return f"spotify:{m.group(1)}:{m.group(2)}"
    logger.warning("Spotify: config-URI nicht erkannt: %r", value)
    return None


class SpotifyController:
    """Dünner Client für die go-librespot REST-API (localhost).

    Alle Methoden sind fehler-tolerant: Daemon nicht erreichbar / HTTP-Fehler
    → False/None + Warning im Log, nie eine Exception zum Aufrufer. Die Box
    muss ohne laufenden Spotify-Daemon uneingeschränkt funktionieren.
    """

    def __init__(self, api_url: str = DEFAULT_API_URL,
                 default_uri: Optional[str] = None):
        self._base = (api_url or DEFAULT_API_URL).rstrip("/")
        self._default_uri = normalize_spotify_uri(default_uri)
        self._session = requests.Session()

        # Coalescing-Volume-Sender: der Encoder feuert beim Drehen viele Ticks
        # pro Sekunde; jeder HTTP-Call inline würde den Encoder-Pfad stauen
        # (gleiches Problem wie früher amixer, siehe main._adjust_volume).
        # Daher: nur Zielwert merken, ein Worker-Thread schickt jeweils den
        # NEUESTEN Wert — Zwischenwerte werden übersprungen.
        self._vol_lock = threading.Lock()
        self._vol_pending: Optional[int] = None
        self._vol_event = threading.Event()
        self._vol_thread = threading.Thread(
            target=self._volume_worker, daemon=True, name="spotify-volume"
        )
        self._vol_thread.start()

    # ------------------------------------------------------------------
    # HTTP-Helfer
    # ------------------------------------------------------------------

    def _get(self, path: str) -> Optional[dict]:
        try:
            r = self._session.get(self._base + path, timeout=_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning("Spotify: GET %s fehlgeschlagen: %s", path, e)
            return None

    def _post(self, path: str, payload: Optional[dict] = None,
              read_timeout: Optional[float] = None) -> bool:
        try:
            timeout = (_TIMEOUT[0], read_timeout or _TIMEOUT[1])
            r = self._session.post(self._base + path, json=payload,
                                   timeout=timeout)
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("Spotify: POST %s fehlgeschlagen: %s", path, e)
            return False

    # ------------------------------------------------------------------
    # Player-Steuerung
    # ------------------------------------------------------------------

    def status(self) -> Optional[dict]:
        """GET /status — None wenn der Daemon nicht erreichbar ist."""
        return self._get("/status")

    def turn_on(self, volume: Optional[int] = None) -> bool:
        """Chip aufgelegt → Musik an.

        Reihenfolge: Lautstärke angleichen (Box-Regler ist die Wahrheit),
        dann resume — oder Default-Playlist, wenn der Player leer ist.
        """
        st = self.status()
        if st is None:
            return False
        if not st.get("username"):
            logger.warning("Spotify: Daemon läuft, aber kein Konto verknüpft "
                           "(OAuth-Login fehlt) — siehe setup/spotify/README.md.")
            return False
        if volume is not None:
            self.set_volume_async(volume)

        # "stopped" = kein Kontext geladen (frischer Daemon). "paused" mit
        # geladenem Track = der Normalfall nach Chip-Runter → resume.
        if st.get("stopped") or not st.get("track"):
            if self._default_uri:
                logger.info("Spotify: kein Kontext — starte Default %s",
                            self._default_uri)
                # /player/play antwortet erst, wenn der Kontext geladen ist —
                # bei langsamem WLAN oder (Fehlerfall) beim Durchskippen
                # unspielbarer Tracks dauert das deutlich länger als der
                # Standard-Read-Timeout. Aufrufer läuft eh im Worker-Thread.
                return self._post("/player/play", {"uri": self._default_uri},
                                  read_timeout=20.0)
            logger.info("Spotify: kein Kontext geladen und keine Default-URI "
                        "konfiguriert (config spotify.uri). Einmal in der "
                        "Spotify-App Musik auf 'kakabox' starten.")
            return False
        return self._post("/player/resume")

    def pause(self) -> bool:
        return self._post("/player/pause")

    def stop(self) -> bool:
        return self._post("/player/stop")

    def next_track(self) -> bool:
        return self._post("/player/next")

    def prev_track(self) -> bool:
        return self._post("/player/prev")

    def is_playing(self) -> bool:
        """True nur wenn nachweislich Wiedergabe läuft (für Standby-Check)."""
        st = self.status()
        if st is None:
            return False
        return bool(st.get("track")) and not st.get("paused") \
            and not st.get("stopped")

    # ------------------------------------------------------------------
    # Lautstärke
    # ------------------------------------------------------------------

    def set_volume_async(self, percent: int) -> None:
        """Nicht-blockierend: merkt den Zielwert, Worker schickt den neuesten.

        Skala: der Daemon ist mit volume_steps=100 konfiguriert (config.yml),
        damit Box-Prozent == Daemon-Steps gilt und hier nichts umgerechnet
        werden muss.
        """
        with self._vol_lock:
            self._vol_pending = max(0, min(100, int(percent)))
        self._vol_event.set()

    def _volume_worker(self) -> None:
        while True:
            self._vol_event.wait()
            self._vol_event.clear()
            with self._vol_lock:
                target = self._vol_pending
                self._vol_pending = None
            if target is None:
                continue
            self._post("/player/volume", {"volume": target})
