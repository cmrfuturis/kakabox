"""KI-Assistent für Kinder (Claude-basiert, konversativ).

Läuft auf der Kakabox, nutzt den Server für LLM-Inference (Phase 4).
Features: Lied-Info, Rätsel, Geschichten, Schulunterstützung, Gedächtnis.

Offline-Fallback: Bei Server-Fehler spielen einfache lokale Befehle ab.

Modus-Integration:
- Zauberwort-Mode: nur play_song/pause/next/volume erlaubt
- Nachtmodus (quiet_hours): ruhige Inhalte, leise Antworten
- SafetyFilter: blockiert Vulgär/Rassistisch/Feindselig (deutsche Blacklist)
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Deutsche Blacklist: Wörter/Phrasen, die Kinder nicht hören sollen
FORBIDDEN_WORDS = {
    # Vulgär (Deutsch)
    "scheiße", "scheiss", "verdammt", "verdamm", "arschloch", "arsch", "fick", "nutte",
    # Rassistisch / Diskriminierend
    "neger", "zigeuner", "behindert", "retard",
    # Feindselig / Gewalt
    "töten", "umbringen", "erschießen", "erstechen", "vergewaltigen", "verprügeln",
    # Selbstschaden
    "suizid", "selbstmord", "ritzem", "hungern",
}

SCARY_TOPICS = {
    "horrorfilm", "grusel", "angst", "angststörung", "panikattacke",
    "monster", "geist", "zombie", "vampir", "werwolf",
}


class SafetyFilter:
    """Blockiert unsichere Inhalte für Kinder."""

    @staticmethod
    def is_safe(text: str, box_config: dict) -> bool:
        """Prüft ob Text für Kinder sicher ist."""
        text_lower = text.lower()

        # Vulgär / Rassistisch / Feindselig
        for word in FORBIDDEN_WORDS:
            if word in text_lower:
                logger.warning(f"SafetyFilter: blocked forbidden word '{word}'")
                return False

        # Zauberwort-Mode: nur Musik-Befehle
        if box_config.get("zauberwort_mode_enabled"):
            allowed_intents = {"play_song", "pause", "next", "volume", "goodbye"}
            intent = SafetyFilter._detect_intent(text_lower)
            if intent not in allowed_intents:
                logger.warning(f"SafetyFilter: zauberwort mode, intent '{intent}' not allowed")
                return False

        # Nachtmodus: keine gruselig/Action-Inhalte
        if box_config.get("quiet_hours"):
            for topic in SCARY_TOPICS:
                if topic in text_lower:
                    logger.warning(f"SafetyFilter: quiet hours, scary topic '{topic}' blocked")
                    return False

        return True

    @staticmethod
    def _detect_intent(text_lower: str) -> str:
        """Einfache Intent-Heuristik (wird durch Claude ersetzt, aber Fallback)."""
        if any(word in text_lower for word in ["spiel", "play"]):
            return "play_song"
        elif any(word in text_lower for word in ["pausier", "pause", "stopp", "stop"]):
            return "pause"
        elif any(word in text_lower for word in ["nächst", "next", "vor"]):
            return "next"
        elif any(word in text_lower for word in ["lauter", "leiser", "volume", "laut"]):
            return "volume"
        elif any(word in text_lower for word in ["aus", "goodbye", "bye", "schlaf", "nacht"]):
            return "goodbye"
        return "answer"


class VoiceAssistant:
    """Konversations-Assistent für Kinder (mit Memory + Server-LLM + Safety)."""

    SILENCE_TIMEOUT = 5.0  # Sekunden Stille bevor Abbruch

    def __init__(self, backend: Any, player: Any, recorder: Any, tts_path: Optional[Path] = None):
        """
        Args:
            backend: Network-Backend für Server-Kommunikation
            player: Audio-Player (für TTS-Ausgabe + Musik)
            recorder: Voice-Recorder für conversation_loop (Aufnahme + Stille-Detect)
            tts_path: Pfad zu TTS-Audio (optional)
        """
        self.backend = backend
        self.player = player
        self.recorder = recorder
        self.tts_path = tts_path

        # Konversations-Memory (max 10 Turns, älteste werden gelöscht)
        self.history: list[dict[str, str]] = []
        self.max_history = 10

        # Zuletzt gespieltes Lied (für Kontext)
        self.now_playing: Optional[dict[str, str]] = None

        # Kinder-Alter (wird vom Config gespeichert, default 7)
        self.child_age: int = 7

        # Safety
        self.safety = SafetyFilter()

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

    def conversation_loop(self, box_config: dict, catalog: list[dict]) -> Optional[dict]:
        """Konversations-Modus: Endlosschleife bis Abbruch oder Play-Intent.

        Args:
            box_config: Box-Konfiguration (zauberwort_mode, quiet_hours, etc.)
            catalog: Verfügbare Lieder (für Claude-Kontext)

        Returns:
            Optional[dict] bei Intent="play_song":
                {"intent": "play_song", "song_id": ..., "song_title": ...}
            oder None bei Abbruch (Stille/Goodbye)
        """
        logger.info("KI-Modus gestartet (5s Stille-Timeout)")

        while True:
            try:
                # 1. Aufnahme bis Stille (SILENCE_TIMEOUT = 5s)
                rec = self.recorder.record_until_silence(
                    timeout_secs=self.SILENCE_TIMEOUT,
                    silence_threshold=0.1,
                )
                if not rec or not rec.speech_seen:
                    # Stille ohne Sprache → Abbruch
                    logger.info("KI-Modus: Stille erkannt, beende")
                    return None

                transcript = rec.transcript or ""
                if not transcript:
                    # Leeres Transkript (ASR Fehler) → Abbruch
                    logger.warning("KI-Modus: ASR leer, beende")
                    return None

                # 2. Claude verstehen
                result = self.understand(
                    transcript,
                    context={
                        "box_config": box_config,
                        "catalog": catalog,
                        "child_age": self.child_age,
                    },
                )
                if not result:
                    logger.warning("KI-Modus: understand() gab None zurück")
                    return None

                # 3. Safety-Check
                response_text = result.get("response", "")
                if not self.safety.is_safe(response_text, box_config):
                    logger.warning("KI-Modus: Response blockiert (SafetyFilter)")
                    response_text = "Davon kann ich dir nicht erzählen."

                # 4. TTS sprechen
                try:
                    if self.tts_path and self.player:
                        # TODO: TTS generieren + abspielen
                        # Für jetzt: nur logg
                        logger.info(f"KI: {response_text}")
                except Exception as e:
                    logger.warning(f"KI-Modus: TTS Fehler: {e}")
                    # Trotzdem weiter

                # 5. Memory + Intent-Check
                self._add_to_history(transcript, response_text)
                intent = result.get("intent", "answer")

                if intent == "play_song":
                    # Lied spielen → beende Loop
                    song_id = result.get("action", {}).get("song_id")
                    song_title = result.get("action", {}).get("song_title")
                    logger.info(f"KI-Modus: Play-Intent → {song_title}")
                    return {"intent": "play_song", "song_id": song_id, "song_title": song_title}

                elif intent == "goodbye":
                    logger.info("KI-Modus: Goodbye-Intent, beende")
                    return None

                # sonst: loop → warte auf nächste Eingabe

            except Exception as e:
                logger.exception(f"KI-Modus: Fehler in Loop: {e}")
                return None

    def get_debug_info(self) -> dict[str, Any]:
        """Gibt Debug-Infos (Memory, Status)."""
        return {
            "enabled": self.backend.is_connected,
            "child_age": self.child_age,
            "history_turns": len(self.history),
            "now_playing": self.now_playing,
            "last_turn": self.history[-1] if self.history else None,
        }
