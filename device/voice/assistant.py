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
from typing import Any, Callable, Optional

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

    SILENCE_TIMEOUT = 5.0  # Sekunden Stille (Start ODER Nachlauf) bevor Abbruch
    # Harte Obergrenze pro Aufnahme-Turn — User-Wunsch ist "kein Zeitlimit",
    # aber MicRecorder.record_until_silence() verlangt ein max_seconds als
    # Sicherheitsnetz (falls VAD dauerhaft Sprache erkennt, z.B. Radio im
    # Hintergrund). 60s ist für ein Kind-Satz-Turn faktisch unbegrenzt.
    MAX_TURN_SECONDS = 60.0

    def __init__(
        self, backend: Any, player: Any, recorder: Any = None, speaker: Any = None,
        volume: int = 70, transcribe_fn: Optional[Callable[[Any], str]] = None,
    ):
        """
        Args:
            backend: Network-Backend für Server-Kommunikation
            player: Audio-Player (für TTS-Ausgabe + Musik)
            recorder: Voice-Recorder für conversation_loop (Aufnahme + Stille-Detect).
                Optional — nur ``understand()`` (Legacy-Einzelfrage-Pfad) braucht
                keinen Recorder; ``conversation_loop()`` verlangt einen.
            speaker: TTS-Engine mit ``synth_to_wav(text) -> Optional[Path]`` (z.B.
                voice.tts.TitleSpeaker) — dieselbe Stimme wie die Titel-Ansage.
                Optional: fehlt sie (Piper/espeak nicht verfügbar), bleibt die
                Antwort stumm geloggt statt gesprochen.
            volume: Lautstärke für TTS-Wiedergabe (0-100, wie Player.play_prompt)
            transcribe_fn: Callable(wav_path) -> str — ASR-Transkription (z.B.
                main.py's ``_transcribe_command``, die Server-Hybrid + lokalen
                Fallback kapselt). ``record_until_silence()`` liefert selbst
                KEIN Transkript, nur die Aufnahme + VAD-Flag.
        """
        self.backend = backend
        self.player = player
        self.recorder = recorder
        self.speaker = speaker
        self.volume = volume
        self.transcribe_fn = transcribe_fn

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
        if not self.backend or not self.backend.is_connected:
            logger.info("Assistant offline — fallback lokal")
            return None

        # Baue Kontext für den Server. now_playing nur bei tatsächlich laufendem
        # Lied mitschicken (siehe ask() für die Begründung — sonst 422 an
        # Laravels "sometimes|array"-Validierung, weil ein explizites
        # "now_playing": null als vorhanden, aber ungültig gilt).
        context = {
            "child_age": self.child_age,
            "conversation_history": self.history[-5:],  # letzte 5 Turns
        }
        if self.now_playing:
            context["now_playing"] = self.now_playing

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

    def ask(self, transcript: str, box_config: dict, catalog: list[dict]) -> Optional[dict]:
        """Wie ``understand()``, aber für den Konversations-Modus: schickt
        zusätzlich ``box_config`` (Zauberwort/Nachtmodus) und ``catalog``
        (verfügbare Lieder) mit und gibt das VOLLE strukturierte Ergebnis
        zurück (``intent``, ``response``, ``action``, ``confidence``) statt
        nur den Antworttext — ``conversation_loop()`` braucht den Intent fürs
        Routing (play_song/goodbye/...).

        Speichert NICHT selbst in die History — der Aufrufer entscheidet das
        (im Konversations-Modus erst NACH dem Safety-Check).

        Returns:
            Das rohe Server-Dict, oder None bei Offline/Fehler.
        """
        if not self.backend or not self.backend.is_connected:
            logger.info("Assistant offline — kein Server erreichbar")
            return None

        context = {
            "child_age": self.child_age,
            "conversation_history": self.history[-5:],
            "box_config": box_config,
            "catalog": catalog,
        }
        # now_playing nur mitschicken, wenn tatsächlich etwas läuft — Laravels
        # "sometimes"-Validierung prüft nur, ob der Key im Payload EXISTIERT,
        # nicht ob sein Wert null ist. Ein explizites "now_playing": null hätte
        # bei JEDER Anfrage ohne laufendes Lied die array-Regel verletzt (422).
        if self.now_playing:
            context["now_playing"] = self.now_playing

        try:
            timeout = 8
            response = self.backend._session.post(
                f"{self.backend._base_url}/api/box/assistant",
                json={"transcript": transcript, **context},
                headers=self.backend._auth_headers(),
                timeout=timeout,
            )
        except Exception as e:
            logger.warning(f"Assistant request failed: {e}")
            return None

        if response.status_code == 503:
            # WARNING statt debug (User-Wunsch): dieser Fall ist unsichtbar auf
            # INFO-Log-Level geblieben und hat einen Server-seitigen Bug
            # (Assistant per Config deaktiviert) tagelang wie eine echte
            # Verbindungsstörung aussehen lassen.
            logger.warning(f"Assistant disabled or unavailable (503): {response.text[:200]}")
            return None
        if not response.ok:
            logger.warning(f"Assistant HTTP {response.status_code}: {response.text[:200]}")
            return None

        try:
            data = response.json()
            if data.get("status") != "ok":
                logger.warning(f"Assistant response status != ok: {data}")
                return None
            return data
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Assistant response parse error: {e}")
            return None

    def _speak(self, text: str) -> None:
        """Synthetisiert ``text`` per TTS und spielt ihn ab — best-effort.

        Ohne Speaker (Piper/espeak beide weg) oder ohne Player bleibt die
        Antwort stumm; der Turn läuft trotzdem weiter (Memory + Intent-Routing
        funktionieren auch ohne Sprachausgabe).
        """
        if not self.speaker or not self.player or not text:
            return
        try:
            wav = self.speaker.synth_to_wav(text)
            if wav is None:
                logger.info("KI-Modus: TTS lieferte keine WAV für Antwort.")
                return
            self.player.play_prompt(str(wav), self.volume)
            self.player.wait_until_idle(timeout=15.0)
        except Exception as e:
            logger.warning(f"KI-Modus: TTS/Wiedergabe-Fehler: {e}")

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
            catalog: Verfügbare Lieder (für Claude-Kontext) — Liste von
                {"content_id": int, "title": str, ...} (roh aus voice_catalog.json,
                NICHT die Candidate-Objekte des Fuzzy-Matchers).

        Returns:
            {"intent": "play_song", "song_id": ..., "song_title": ...} bei Play-Wunsch,
            {"intent": "offline"} wenn der Server nicht erreichbar ist (Aufrufer
            spielt dann einen eigenen Offline-Hinweis statt des generischen
            Fehlertons), oder None bei Abbruch (Stille/Goodbye/Fehler).
        """
        # Offline-Gate VOR jeder Aufnahme: ohne Server keine Antwort möglich —
        # das Kind soll nicht erst sprechen und dann nur Stille/Error-Ton hören.
        if not self.backend or not self.backend.is_connected:
            logger.info("KI-Modus: Server offline — Konversation nicht möglich.")
            return {"intent": "offline"}

        logger.info("KI-Modus gestartet (5s Stille-Timeout)")

        while True:
            try:
                # 1. Aufnahme bis Stille (SILENCE_TIMEOUT = 5s, sowohl als
                # initiale als auch als Nachlauf-Stille — kein Zeitlimit fürs
                # eigentliche Reden, siehe MAX_TURN_SECONDS als Sicherheitsnetz).
                rec = self.recorder.record_until_silence(
                    max_seconds=self.MAX_TURN_SECONDS,
                    silence_seconds=self.SILENCE_TIMEOUT,
                    initial_silence_seconds=self.SILENCE_TIMEOUT,
                )
                if not rec or not rec.speech_seen:
                    # Stille ohne Sprache → Abbruch
                    logger.info("KI-Modus: Stille erkannt, beende")
                    return None

                # 2. Transkription (record_until_silence liefert nur die
                # Aufnahme + VAD-Flag, kein Transkript).
                if not self.transcribe_fn:
                    logger.warning("KI-Modus: kein transcribe_fn gesetzt, beende")
                    return None
                try:
                    transcript = self.transcribe_fn(rec.path) or ""
                except Exception as e:
                    logger.warning(f"KI-Modus: ASR-Fehler: {e}")
                    return None
                if not transcript:
                    # Leeres Transkript (ASR Fehler) → Abbruch
                    logger.warning("KI-Modus: ASR leer, beende")
                    return None
                logger.info(f"KI-Modus transkribiert: «{transcript}»")

                # 3. Claude verstehen (inkl. box_config + catalog)
                result = self.ask(transcript, box_config, catalog)
                if not result:
                    # "offline" nur, wenn WIRKLICH keine Verbindung mehr besteht
                    # (User-Wunsch) — ein serverseitiger Fehler (503/500/kaputtes
                    # JSON) bei bestehender Verbindung ist kein Offline-Zustand
                    # und soll nicht fälschlich als solcher angesagt werden.
                    if not self.backend or not self.backend.is_connected:
                        logger.warning("KI-Modus: Verbindung während Konversation verloren.")
                        return {"intent": "offline"}
                    logger.warning("KI-Modus: ask() gab None zurück (Server-Fehler, Verbindung besteht) — Abbruch.")
                    return None

                # 4. Safety-Check
                response_text = result.get("response", "")
                if not self.safety.is_safe(response_text, box_config):
                    logger.warning("KI-Modus: Response blockiert (SafetyFilter)")
                    response_text = "Davon kann ich dir nicht erzählen."

                # 5. TTS sprechen
                self._speak(response_text)

                # 6. Memory + Intent-Check
                self._add_to_history(transcript, response_text)
                intent = result.get("intent", "answer")

                if intent == "play_song":
                    # Lied spielen → beende Loop
                    action = result.get("action") or {}
                    song_id = action.get("song_id")
                    song_title = action.get("song_title")
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
            "enabled": bool(self.backend and self.backend.is_connected),
            "child_age": self.child_age,
            "history_turns": len(self.history),
            "now_playing": self.now_playing,
            "last_turn": self.history[-1] if self.history else None,
        }
