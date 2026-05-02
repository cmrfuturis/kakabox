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
from typing import Any

import requests

logger = logging.getLogger("kakabox.network")

DEFAULT_BACKEND_URL = os.environ.get("KAKABOX_BACKEND", "http://localhost:8000")
DEFAULT_TIMEOUT = 5.0


class BackendError(RuntimeError):
    """Raised for non-recoverable backend responses (auth, 4xx other than 404)."""


class NotConnected(BackendError):
    """Raised when an authenticated call is made before the box has a token."""


class Backend:
    def __init__(
        self,
        identity_path: Path,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._identity_path = Path(identity_path)
        self._base_url = (base_url or DEFAULT_BACKEND_URL).rstrip("/")
        self._timeout = timeout
        self._identity: dict[str, Any] = self._load_identity()

    # ------------------------------------------------------------------
    # Identity persistence
    # ------------------------------------------------------------------

    def _load_identity(self) -> dict[str, Any]:
        if not self._identity_path.exists():
            raise FileNotFoundError(
                f"box_identity.json missing at {self._identity_path}"
            )
        return json.loads(self._identity_path.read_text())

    def _save_identity(self) -> None:
        self._identity_path.write_text(
            json.dumps(self._identity, indent=2, ensure_ascii=False)
        )

    @property
    def token(self) -> str | None:
        return self._identity.get("api_token")

    @property
    def is_connected(self) -> bool:
        return bool(self.token)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_connected(self) -> bool:
        """Connect once if no token is stored yet. Returns True on success."""
        if self.is_connected:
            return True
        return self.connect()

    def connect(self) -> bool:
        """POST /api/box/connect with serial + activation code, store token."""
        serial = self._identity.get("serial_number")
        code = self._identity.get("activation_code")
        if not serial or not code:
            raise BackendError("box_identity.json missing serial_number or activation_code")

        url = f"{self._base_url}/api/box/connect"
        try:
            resp = requests.post(
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
                "registered in webapp. Register the box in the parent app first."
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
        """POST /api/box/heartbeat. Returns True on 2xx."""
        if not self.is_connected:
            return False
        url = f"{self._base_url}/api/box/heartbeat"
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("heartbeat transport error: %s", e)
            return False

        if resp.status_code == 401:
            logger.error("Backend says token invalid — clearing local token.")
            self._identity["api_token"] = None
            self._save_identity()
            return False

        if not resp.ok:
            logger.warning("heartbeat: HTTP %s — %s", resp.status_code, resp.text[:200])
            return False
        return True

    def tag_scan(self, uid: str) -> dict[str, Any] | None:
        """POST /api/box/tag-scan. Returns parsed JSON, or None on transport error."""
        if not self.is_connected:
            raise NotConnected("device has no api_token; call ensure_connected() first")

        url = f"{self._base_url}/api/box/tag-scan"
        try:
            resp = requests.post(
                url,
                json={"tag_uid": uid.upper()},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            logger.warning("tag_scan transport error: %s", e)
            return None

        if resp.status_code == 401:
            logger.error("Backend says token invalid — clearing local token.")
            self._identity["api_token"] = None
            self._save_identity()
            return None

        # 200 (paired/play), 404 (unknown), 403 (foreign household) all return JSON
        try:
            return resp.json()
        except ValueError:
            logger.error("tag_scan: non-JSON response (HTTP %s)", resp.status_code)
            return None
