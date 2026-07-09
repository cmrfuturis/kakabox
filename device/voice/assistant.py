"""KI-Assistent für Kinder (Claude-basiert, konversativ).

Läuft auf der Kakabox, nutzt den Server für LLM-Inference (Phase 4).
Features: Lied-Info, Rätsel, Geschichten, Schulunterstützung, Gedächtnis.

Offline-Fallback: Bei Server-Fehler spielen einfache lokale Befehle ab.
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class VoiceAssistant:
    """Konversations-Assistent für Kinder (mit Memory + Server-LLM)."""

    def __init__(self, backend: Any, player: Any, tts_path: Optional[Path] = None):
        """
        Args:
            backend: Network-Backend für Server-Kommunikation
            player: Audio-Player (für TTS-Ausgabe)
            tts_path: Pfad zu TTS-Audio (optional)
        """
        self.backend = backend
        self.player = player
        self.tts_path = tts_path

        # Konversations-Memory (max 10 Turns, älteste werden gelöscht)
        self.history: list[dict[str, str]] = []
        self.max_history = 10

        # Zuletzt gespieltes Lied (für Kontext)
        self.now_playing: Optional[dict[str, str]] = None

        # Kinder-Alter (wird vom Config gespeichert, default 7)
        self.child_age: int = 7

    def set_now_playing(self, title: str, artist: str) -> None:
        """Speichert das aktuell spielende Lied (für Kontext)."""
        self.now_playing = {"title": title, "artist": artist}

    def understand(self, transcript: str) -> Optional[str]:
        """
        Sendet Transkript an den Server-Assistenten, speichert Kontext,
        und gibt die Antwort des Kindes zurück (zur TTS).

        Returns:
            Sprach-Antwort vom Assistenten, oder None bei Fehler/offline.
        """
        if not self.backend.is_connected:
            logger.info("Assistant offline — fallback lokal")
            return None

        # Baue Kontext für den Server
        context = {
            "child_age": self.child_age,
            "conversation_history": self.history[-5:],  # letzte 5 Turns
            "now_playing": self.now_playing,
        }

        # Frage den Server
        try:
            timeout = 8  # kurz halten, damit Kind nicht zu lange wartet
            response = self.backend._session.post(
                f"{self.backend._base_url}/api/box/assistant",
                json={
                    "transcript": transcript,
                    **context,
                },
                headers=self.backend._auth_headers(),
                timeout=timeout,
            )
        except Exception as e:
            logger.warning(f"Assistant request failed: {e}")
            return None

        if response.status_code == 503:
            # Server hat Assistenten deaktiviert oder ist überlastet
            logger.debug("Assistant disabled or unavailable (503)")
            return None

        if not response.ok:
            logger.warning(f"Assistant HTTP {response.status_code}")
            return None

        try:
            data = response.json()
            if data.get("status") != "ok":
                return None

            response_text = data.get("response", "")
            if response_text:
                # Speichere Turn im Memory
                self._add_to_history(transcript, response_text)
                return response_text

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Assistant response parse error: {e}")

        return None

    def _add_to_history(self, user_text: str, bot_response: str) -> None:
        """Speichert einen Konversations-Turn im Memory."""
        self.history.append({
            "timestamp": datetime.now().isoformat(),
            "transcript": user_text,
            "response": bot_response,
        })
        # Alte Turns löschen, wenn Memory voll
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history :]

    def clear_history(self) -> None:
        """Löscht die Konversations-History."""
        self.history.clear()
        self.now_playing = None

    def get_debug_info(self) -> dict[str, Any]:
        """Gibt Debug-Infos (Memory, Status)."""
        return {
            "enabled": self.backend.is_connected,
            "child_age": self.child_age,
            "history_turns": len(self.history),
            "now_playing": self.now_playing,
            "last_turn": self.history[-1] if self.history else None,
        }
