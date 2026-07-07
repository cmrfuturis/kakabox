"""HTTP client to talk to the Kakabox webapp backend.

Persists the API token in ``box_identity.json`` so the device only needs to
connect once. The plain token is stored locally; the server stores its
SHA-256 hash.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterable

import requests

logger = logging.getLogger("kakabox.network")

DEFAULT_BACKEND_URL = os.environ.get("KAKABOX_BACKEND", "http://localhost:8000")
DEFAULT_TIMEOUT = 5.0
# Tag-Scan ist im User-kritischen Pfad (Wartezeit zwischen Auflegen und Ton).
# Bei Cache-Hit läuft die Wiedergabe schon im Hintergrund weiter, daher kann
# der synchrone Pfad einen kürzeren Timeout vertragen — fällt sonst auf den
# lokalen Cache zurück, statt 5 s zu blockieren.
DEFAULT_TAG_SCAN_TIMEOUT = 2.0
DEFAULT_DOWNLOAD_TIMEOUT = 60.0  # MP3-Downloads dürfen länger dauern


class BackendError(RuntimeError):
    """Raised for non-recoverable backend responses (auth, 4xx other than 404)."""


class NotConnected(BackendError):
    """Raised when an authenticated call is made before the box has a token."""


def _validate_url(url: str) -> str:
    """Lehnt unsichere Backend-URLs ab.

    Plain HTTP würde Bearer-Token, Tag-Scans und MP3-Manifeste durchs Heim-WLAN
    klartext schicken — und damit auch die Hash-Verifikation kompromittieren
    (MITM kann Manifest + Datei kohärent austauschen). Erlaubt sind nur:

    - ``https://*`` für Produktion
    - ``http://localhost`` / ``http://127.0.0.1`` für lokales Dev (Pi spricht
      mit Webapp auf demselben Host — kein Netz dazwischen).

    Bei unzulässiger URL → ``BackendError``. Wird in ``main.py`` per try/except
    gefangen → Box läuft offline weiter, statt unsicher rauszutelefonieren.
    """
    url = url.rstrip("/")
    if url.startswith("https://"):
        return url
    if url.startswith(("http://localhost", "http://127.0.0.1")):
        return url
    raise BackendError(
        f"Refusing insecure backend URL '{url}'. "
        "Use https:// for production or http://localhost for local dev."
    )


class Backend:
    def __init__(
        self,
        identity_path: Path,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._identity_path = Path(identity_path)
        self._base_url = _validate_url(base_url or DEFAULT_BACKEND_URL)
        self._timeout = timeout
        self._identity: dict[str, Any] = self._load_identity()
        # Persistente HTTP-Session: hält die TCP/TLS-Verbindung am Leben,
        # sodass tag_scan/heartbeat/manifest ohne erneuten Handshake laufen.
        # Auf einem Pi über WLAN spart das pro Call ~100–500 ms.
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Identity persistence
    # ------------------------------------------------------------------

    def _load_identity(self) -> dict[str, Any]:
        if not self._identity_path.exists():
            raise FileNotFoundError(
                f"box_identity.json missing at {self._identity_path}"
            )
        try:
            return json.loads(self._identity_path.read_text())
        except json.JSONDecodeError:
            # Kann durch einen Spannungseinbruch mitten in _save_identity()
            # entstehen (vor dem Atomic-Write-Fix unten passiert, alte Backups
            # können noch betroffen sein). Ohne Identität kann die Box sich
            # nicht am Backend anmelden — .bak ist der einzige Recovery-Pfad,
            # da es keine sinnvollen Default-Werte für Serial/Token gibt.
            backup_path = Path(str(self._identity_path) + ".bak")
            if backup_path.is_file():
                logger.error(
                    "box_identity.json ist beschädigt — falle auf %s zurück.",
                    backup_path,
                )
                return json.loads(backup_path.read_text())
            raise

    def _save_identity(self) -> None:
        data = json.dumps(self._identity, indent=2, ensure_ascii=False)
        # Atomar: tmp-Datei + os.replace, damit ein Spannungseinbruch mitten im
        # Schreiben nie eine halbe/kaputte box_identity.json hinterlässt.
        tmp_path = Path(str(self._identity_path) + ".tmp")
        tmp_path.write_text(data)
        tmp_path.replace(self._identity_path)
        # Zusätzliches .bak als zweite Verteidigungslinie (siehe _load_identity).
        Path(str(self._identity_path) + ".bak").write_text(data)

    @property
    def token(self) -> str | None:
        return self._identity.get("api_token")

    @property
    def is_connected(self) -> bool:
        return bool(self.token)

    @property
    def base_url(self) -> str:
        return self._base_url

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ------------------------------------------------------------------
    # Connection / heartbeat
    # ------------------------------------------------------------------

    def ensure_connected(self) -> bool:
        if self.is_connected:
            return True
        return self.connect()

    def connect(self) -> bool:
        serial = self._identity.get("serial_number")
        code = self._identity.get("activation_code")
        if not serial or not code:
            raise BackendError("box_identity.json missing serial_number or activation_code")

        url = f"{self._base_url}/api/box/connect"
        try:
            resp = self._session.post(
                url,
                json={"serial_number": serial, "activation_code": code},
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("Backend connect failed (network): %s", e)
            return False

        if resp.status_code == 401:
            logger.error(
                "Backend rejected connect: serial/activation invalid or box not yet "
                "registered in webapp."
            )
            return False

        if not resp.ok:
            logger.error("Backend connect failed: HTTP %s — %s", resp.status_code, resp.text)
            return False

        data = resp.json()
        self._identity["api_token"] = data["token"]
        self._identity["registered_at"] = self._identity.get("registered_at") or "connected"
        self._save_identity()
        logger.info("Connected to backend as box id=%s", data.get("box", {}).get("id"))
        return True

    def heartbeat(self, payload: dict[str, Any]) -> bool:
        if not self.is_connected:
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/api/box/heartbeat",
                json=payload,
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("heartbeat transport error: %s", e)
            return False

        if resp.status_code == 401:
            self._clear_token("token invalid (heartbeat)")
            return False

        if not resp.ok:
            logger.warning("heartbeat: HTTP %s", resp.status_code)
            return False
        return True

    # ------------------------------------------------------------------
    # Tag-Scan
    # ------------------------------------------------------------------

    def tag_scan(self, uid: str, timeout: float | None = None) -> dict[str, Any] | None:
        """Return parsed JSON, or None on transport error."""
        if not self.is_connected:
            raise NotConnected("device has no api_token; call ensure_connected() first")

        try:
            resp = self._session.post(
                f"{self._base_url}/api/box/tag-scan",
                json={"tag_uid": uid.upper()},
                headers=self._auth_headers(),
                timeout=timeout if timeout is not None else DEFAULT_TAG_SCAN_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.warning("tag_scan transport error: %s", e)
            return None

        if resp.status_code == 401:
            self._clear_token("token invalid (tag_scan)")
            return None

        # Alle 200/403/404 antworten mit JSON-Body
        try:
            return resp.json()
        except ValueError:
            logger.error("tag_scan: non-JSON response (HTTP %s)", resp.status_code)
            return None

    # ------------------------------------------------------------------
    # Audio-Sync
    # ------------------------------------------------------------------

    def audio_manifest(self) -> dict[str, Any] | None:
        """Liste der Audios, die diese Box haben sollte."""
        if not self.is_connected:
            return None
        try:
            resp = self._session.get(
                f"{self._base_url}/api/box/audio-manifest",
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("audio_manifest transport error: %s", e)
            return None

        if resp.status_code == 401:
            self._clear_token("token invalid (audio_manifest)")
            return None
        if not resp.ok:
            logger.warning("audio_manifest: HTTP %s", resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def download_audio(self, content_id: int, target_path: Path) -> bool:
        """Lädt eine MP3-Datei in target_path. Schreibt atomisch über .part-Tempfile."""
        if not self.is_connected:
            return False

        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_suffix(target_path.suffix + ".part")

        try:
            with self._session.get(
                f"{self._base_url}/api/box/audio/{content_id}/download",
                headers=self._auth_headers(),
                timeout=DEFAULT_DOWNLOAD_TIMEOUT,
                stream=True,
            ) as resp:
                if resp.status_code == 401:
                    self._clear_token("token invalid (download_audio)")
                    return False
                if not resp.ok:
                    # DEBUG, weil der Sync-Loop diese Failures gesammelt in einer
                    # Summary-Zeile loggt (verhindert Log-Spam bei kaputten
                    # Backend-Storage-IDs, die jeden Sync-Zyklus 404 liefern).
                    logger.debug("download_audio %s: HTTP %s", content_id, resp.status_code)
                    return False
                with tmp_path.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
        except requests.RequestException as e:
            logger.warning("download_audio %s transport error: %s", content_id, e)
            tmp_path.unlink(missing_ok=True)
            return False

        # Atomisches Rename
        tmp_path.replace(target_path)
        return True

    def report_audio_cached(self, content_id: int, file_hash: str) -> bool:
        """Bestätigt dem Backend, dass diese Box den Content jetzt lokal hat."""
        if not self.is_connected:
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/api/box/audio/{content_id}/cached",
                json={"file_hash": file_hash},
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("report_audio_cached transport error: %s", e)
            return False
        return resp.ok

    def upload_voice_command(self, wav_path: Path, meta: dict[str, Any]) -> bool:
        """Lädt einen aufgenommenen Sprachbefehl (WAV + Metadaten) zur Ansicht/
        zum Abhören in die Webapp hoch. Best-effort: bei fehlendem Netz/Token
        einfach False — der Voice-Flow darf davon nie abhängen.

        ``meta``-Felder: transcript, action, matched_name, matched_kind,
        matched_content_id, score, duration_seconds, recorded_at.
        """
        if not self.is_connected:
            return False
        # None-Werte rauswerfen — der Server validiert 'nullable', aber ein
        # multipart-Feld mit literalem "None" wäre ein String, kein Null.
        data = {k: v for k, v in meta.items() if v is not None}
        try:
            wav_path = Path(wav_path)
            if wav_path.is_file():
                with wav_path.open("rb") as fh:
                    resp = self._session.post(
                        f"{self._base_url}/api/box/voice-command",
                        data=data,
                        files={"audio": ("command.wav", fh, "audio/wav")},
                        headers=self._auth_headers(),
                        timeout=DEFAULT_DOWNLOAD_TIMEOUT,
                    )
            else:
                # Kein Audio (z.B. VAD sah keine Sprache) — nur Metadaten.
                resp = self._session.post(
                    f"{self._base_url}/api/box/voice-command",
                    data=data,
                    headers=self._auth_headers(),
                    timeout=self._timeout,
                )
        except requests.RequestException as e:
            logger.warning("upload_voice_command transport error: %s", e)
            return False
        if not resp.ok:
            logger.debug("upload_voice_command: HTTP %s", resp.status_code)
        return resp.ok

    def report_storage(self, total_mb: int, free_mb: int) -> bool:
        if not self.is_connected:
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/api/box/storage-status",
                json={"total_mb": total_mb, "free_mb": free_mb},
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("report_storage transport error: %s", e)
            return False
        return resp.ok

    # ------------------------------------------------------------------
    # Play-Sessions (Wiedergabe-Historie für die Webapp)
    # ------------------------------------------------------------------

    def play_session(self, payload: dict[str, Any]) -> bool:
        """Meldet eine abgeschlossene Wiedergabe-Session ans Backend.

        Erwartete Felder: client_uuid (für Idempotenz bei Retries),
        content_id, kaka_id (nullable), source, started_at, ended_at,
        duration_seconds, end_reason, optional used_zauberwort + metadata.
        """
        if not self.is_connected:
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/api/box/play-session",
                json=payload,
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("play_session transport error: %s", e)
            return False
        if resp.status_code == 401:
            self._clear_token("token invalid (play_session)")
            return False
        if not resp.ok:
            # 4xx (validation/foreign household) loggen wir auf WARN — der
            # Reporter darf nicht ewig retryen, wenn das Backend den Eintrag
            # ablehnt. 5xx wird vom Reporter selbst erneut versucht.
            level = logging.WARNING if resp.status_code < 500 else logging.INFO
            logger.log(level, "play_session: HTTP %s — %s", resp.status_code, resp.text[:200])
            return False
        return True

    # ------------------------------------------------------------------
    # Commands (Pull-Modell)
    # ------------------------------------------------------------------

    def fetch_commands(self) -> Iterable[dict[str, Any]]:
        if not self.is_connected:
            return []
        try:
            resp = self._session.get(
                f"{self._base_url}/api/box/commands",
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("fetch_commands transport error: %s", e)
            return []
        if not resp.ok:
            return []
        try:
            return resp.json().get("commands", [])
        except ValueError:
            return []

    def acknowledge_command(self, command_id: int) -> bool:
        if not self.is_connected:
            return False
        try:
            resp = self._session.post(
                f"{self._base_url}/api/box/commands/{command_id}/ack",
                headers=self._auth_headers(),
                timeout=self._timeout,
            )
        except requests.RequestException:
            return False
        return resp.ok

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _clear_token(self, reason: str) -> None:
        logger.error("Clearing local token: %s", reason)
        self._identity["api_token"] = None
        self._save_identity()
