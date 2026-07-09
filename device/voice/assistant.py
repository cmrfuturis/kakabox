"""KI-Assistent für Kinder (Claude-basiert, konversativ).

Läuft auf der Kakabox, nutzt den Server für LLM-Inference (Phase 4).
Features: Lied-Info, Rätsel, Geschichten, Schulunterstützung, Gedächtnis.

Offline-Fallback: Bei Server-Fehler spielen einfache lokale Befehle ab.

Modus-Integration:
- Zauberwort-Mode: betrifft NUR Song-Wünsche (intent="play_song") — das Kind
  muss "bitte" gesagt haben, sonst kein Playback. Hat NICHTS mit anderen
  Konversationsarten zu tun (Witze/Geschichten/Fragen sind unberührt,
  User-Klarstellung 2026-07-09) — siehe conversation_loop().
- Nachtmodus (quiet_hours): ruhige Inhalte, leise Antworten
- SafetyFilter: blockiert Vulgär/Rassistisch/Feindselig (deutsche Blacklist)
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Optional

from voice.intent import has_magic_word

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
    """Blockiert unsichere Inhalte für Kinder.

    Zauberwort-Modus gehört NICHT hierher (User-Klarstellung 2026-07-09):
    "bitte" ist nur für Song-Wünsche relevant und wird direkt in
    conversation_loop() vor dem play_song-Intent geprüft — Witze/Geschichten/
    Fragen sind von Zauberwort-Modus komplett unberührt.
    """

    @staticmethod
    def is_safe(text: str, box_config: dict) -> bool:
        """Prüft ob Text für Kinder sicher ist (Vulgär/Rassistisch/Feindselig,
        gruselige Inhalte während Nachtmodus)."""
        text_lower = text.lower()

        # Vulgär / Rassistisch / Feindselig
        for word in FORBIDDEN_WORDS:
            if word in text_lower:
                logger.warning(f"SafetyFilter: blocked forbidden word '{word}'")
                return False

        # Nachtmodus: keine gruselig/Action-Inhalte
        if box_config.get("quiet_hours"):
            for topic in SCARY_TOPICS:
                if topic in text_lower:
                    logger.warning(f"SafetyFilter: quiet hours, scary topic '{topic}' blocked")
                    return False

        return True


def _looks_like_self_echo(spoken_text: str, heard_text: str) -> bool:
    """Grobe Heuristik gegen Mikro-Feedback während Barge-in-Lauschen: ohne
    Hardware-Echo-Unterdrückung hört das Mikro manchmal die eigene TTS-Stimme
    der Box mit. Ist der als "Kind hat geredet" erkannte Text fast identisch
    mit dem, was die Box GERADE SELBST gesagt hat, ist das mit hoher
    Wahrscheinlichkeit die eigene Stimme und kein echter Barge-in — Whisper
    transkribiert synthetische TTS-Sprache meist sehr sauber (kein Hintergrund-
    rauschen), daher reicht ein einfacher Ähnlichkeits-/Containment-Check.

    Live beobachtet: ohne diesen Check konnte sich die Box in einer
    Rückkopplungsschleife selbst "zuhören" — eine geblockte Antwort
    ("Davon kann ich dir nicht erzählen.") wurde vom eigenen Mikro
    aufgenommen, erneut als Kind-Eingabe verarbeitet, wieder geblockt,
    wieder gehört, usw.
    """
    def norm(s: str) -> str:
        return " ".join(s.lower().split())

    a, b = norm(spoken_text), norm(heard_text)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() > 0.7


# Unicode-Bereiche gängiger Emoji (Emoticons, Symbole, Transport, Flaggen,
# Dingbats, Variation-Selector). Piper (TTS) kann Emoji nicht sprechen und
# verbalisiert sie stattdessen als Beschreibung ("😄" → "Gesicht mit
# lachenden Augen") — live beobachtet: das wurde vom eigenen Mikro
# aufgenommen (kein Echo-Cancelling) und wich vom Quelltext zu stark ab, um
# vom Selbst-Echo-Filter (_looks_like_self_echo) erkannt zu werden. Also VOR
# der TTS-Synthese entfernen, statt nur nachträglich zu filtern.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # Symbols & Pictographs, Emoticons, Transport, Supplemental
    "\U00002600-\U000027BF"  # Misc Symbols, Dingbats
    "\U0001F1E6-\U0001F1FF"  # Regional Indicators (Flaggen)
    "\U0000FE0F"              # Variation Selector-16
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    """Entfernt Emoji, damit die TTS nie deren Namen vorliest."""
    return _EMOJI_RE.sub("", text)


class VoiceAssistant:
    """Konversations-Assistent für Kinder (mit Memory + Server-LLM + Safety)."""

    # "5 Sekunden nichts sagen → Abbruch" (User-Wunsch): gilt für die Stille
    # BEVOR überhaupt Sprache erkannt wurde — z.B. wenn das Kind nach der
    # KI-Antwort gar nicht mehr reagiert. Das ist bewusst länger als die
    # Nachlauf-Stille unten (Kind braucht ggf. einen Moment zum Überlegen).
    SILENCE_TIMEOUT = 5.0
    # Nachlauf-Stille NACH erkannter Sprache, bis der Satz als beendet gilt.
    # Live-Test zeigte: mit SILENCE_TIMEOUT (5s) hier wartete die Box nach
    # jedem Satz spürbar lange, bevor sie überhaupt zu antworten begann ("wie
    # Siri, aber langsam" — User-Feedback). 1.4s matcht VOICE_SILENCE_SECONDS
    # aus main.py, bereits an Kinder-Sprechpausen kalibriert (>1s Denkpausen
    # MITTEN im Satz sind normal, siehe dortiger Kommentar) — dieselbe
    # Schwelle ist auch hier ein sicherer, bereits validierter Wert.
    TURN_SILENCE_SECONDS = 1.4
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
        # Wird von conversation_loop() pro Aufruf gesetzt — harter Stopp
        # (z.B. Blau-Knopf während KI-Modus) bricht laufende Aufnahmen sofort
        # ab, siehe record_until_silence()'s cancel_event-Parameter.
        self._cancel_event: Optional[threading.Event] = None

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
        text = _strip_emoji(text).strip()
        if not text:
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

    def _speak_interruptible(self, text: str) -> tuple[bool, Optional[str], bool]:
        """Wie ``_speak()``, aber lauscht GLEICHZEITIG aufs Mikrofon — redet
        das Kind während der Antwort rein (Barge-in, wie ChatGPT Voice Mode),
        wird die Wiedergabe SOFORT gestoppt statt zu Ende zu laufen.

        Mikro-Aufnahme (arecord) und Wiedergabe (mpv) laufen auf getrennten
        ALSA-Geräten — kein Ressourcen-Konflikt. Das Restrisiko ist rein
        akustisch: ohne Echo-Unterdrückung in der Hardware hört das Mikro auch
        die eigene Stimme der Box mit — dagegen filtert ``_looks_like_self_echo``
        Treffer heraus, die fast wortgleich mit der gerade gesprochenen
        Antwort sind (live beobachtet: ohne diesen Filter konnte sich die Box
        in einer Rückkopplungsschleife selbst zuhören). Nutzt ansonsten
        bewusst dieselbe adaptive VAD-Schwelle wie normales Zuhören (kein
        blind geratener Sonder-Threshold) — falls sich die Box dadurch live zu
        oft selbst unterbricht, muss das empirisch nachjustiert werden
        (Mikro-/Lautsprecher-Abstand variiert).

        Die Aufnahme läuft mit den GLEICHEN Timeouts wie ein normaler Turn
        (SILENCE_TIMEOUT/TURN_SILENCE_SECONDS) — läuft die Antwort ungestört
        durch, geht das Lauschen nahtlos in die normale "warte auf nächsten
        Turn"-Phase über. Der Aufrufer braucht danach KEINE zusätzliche
        Aufnahme mehr zu starten.

        Returns:
            (barged_in, transcript, cancelled) — cancelled=True wenn der
            harte Stopp (self._cancel_event, z.B. Blau-Knopf) ausgelöst wurde;
            barged_in=True wenn währenddessen/danach ECHTE Sprache erkannt
            wurde (Selbst-Echo wird herausgefiltert, siehe oben).
        """
        if not self.speaker or not self.player or not text:
            return False, None, False
        text = _strip_emoji(text).strip()
        if not text:
            return False, None, False
        try:
            wav = self.speaker.synth_to_wav(text)
        except Exception as e:
            logger.warning(f"KI-Modus: TTS-Synthese fehlgeschlagen: {e}")
            return False, None, False
        if wav is None:
            logger.info("KI-Modus: TTS lieferte keine WAV für Antwort.")
            return False, None, False

        if not self.recorder or not self.transcribe_fn:
            # Kein Barge-in möglich → normale, nicht unterbrechbare Wiedergabe.
            try:
                self.player.play_prompt(str(wav), self.volume)
                self.player.wait_until_idle(timeout=15.0)
            except Exception as e:
                logger.warning(f"KI-Modus: TTS-Wiedergabe fehlgeschlagen: {e}")
            return False, None, False

        try:
            self.player.play_prompt(str(wav), self.volume)
        except Exception as e:
            logger.warning(f"KI-Modus: TTS-Wiedergabe fehlgeschlagen: {e}")
            return False, None, False

        try:
            rec = self.recorder.record_until_silence(
                max_seconds=self.MAX_TURN_SECONDS,
                silence_seconds=self.TURN_SILENCE_SECONDS,
                initial_silence_seconds=self.SILENCE_TIMEOUT,
                cancel_event=self._cancel_event,
            )
        except Exception as e:
            logger.warning(f"KI-Modus: Barge-in-Aufnahme fehlgeschlagen: {e}")
            rec = None

        # Falls die Antwort noch läuft (Barge-in/harter Stopp): SOFORT
        # stoppen. Ist sie schon fertig (kein Barge-in, normales Zuhören auf
        # den nächsten Turn), ist stop() ein günstiges No-op.
        try:
            self.player.stop()
        except Exception as e:
            logger.warning(f"KI-Modus: Stop nach Antwort fehlgeschlagen: {e}")

        # Defense-in-depth: falls record_until_silence() TROTZ des Fixes in
        # recorder.py mal mit einer Exception statt cancelled=True rausfällt
        # (rec bleibt dann None), prüfen wir das cancel_event direkt — sonst
        # geht ein harter Stopp als generischer Fehler verloren und der Loop
        # versucht fälschlich normal weiterzumachen (live beobachtet: führte
        # zu einer zweiten Aufnahme mit noch gesetztem cancel_event → erneuter
        # Crash statt sauberem Abbruch).
        if (rec is not None and rec.cancelled) or (
            self._cancel_event is not None and self._cancel_event.is_set()
        ):
            logger.info("KI-Modus: hart abgebrochen (Blau-Knopf während Antwort).")
            return False, None, True

        if not rec or not rec.speech_seen:
            return False, None, False

        try:
            transcript = self.transcribe_fn(rec.path) or ""
        except Exception as e:
            logger.warning(f"KI-Modus: Barge-in ASR-Fehler: {e}")
            return True, None, False

        if transcript and _looks_like_self_echo(text, transcript):
            logger.info(f"KI-Modus: Barge-in ignoriert (vermutlich eigene Stimme gehört): «{transcript}»")
            return False, None, False

        return True, (transcript or None), False

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

    def conversation_loop(
        self, box_config: dict, catalog: list[dict],
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[dict]:
        """Konversations-Modus: Endlosschleife bis Abbruch oder Play-Intent.

        Args:
            box_config: Box-Konfiguration (zauberwort_mode, quiet_hours, etc.)
            catalog: Verfügbare Lieder (für Claude-Kontext) — Liste von
                {"content_id": int, "title": str, ...} (roh aus voice_catalog.json,
                NICHT die Candidate-Objekte des Fuzzy-Matchers).
            cancel_event: wird während jeder Aufnahme geprüft — gesetzt (z.B.
                Blau-Knopf während des KI-Modus), bricht die Aufnahme SOFORT
                ab und der Loop beendet sich als harter Stopp (kein Error-Ton-
                Abbruch-Unterschied für den Aufrufer, main.py behandelt beides
                gleich als "Abbruch").

        Returns:
            {"intent": "play_song", "song_id": ..., "song_title": ...} bei Play-Wunsch,
            {"intent": "offline"} wenn der Server nicht erreichbar ist (Aufrufer
            spielt dann einen eigenen Offline-Hinweis statt des generischen
            Fehlertons), oder None bei Abbruch (Stille/Goodbye/Fehler/harter Stopp).
        """
        # Offline-Gate VOR jeder Aufnahme: ohne Server keine Antwort möglich —
        # das Kind soll nicht erst sprechen und dann nur Stille/Error-Ton hören.
        if not self.backend or not self.backend.is_connected:
            logger.info("KI-Modus: Server offline — Konversation nicht möglich.")
            return {"intent": "offline"}

        logger.info("KI-Modus gestartet (5s Stille-Timeout)")
        self._cancel_event = cancel_event

        # Von _speak_interruptible() erkannter Barge-in: das Kind hat schon
        # während/direkt nach der letzten Antwort weitergeredet — dieser Text
        # ist der nächste Turn, keine neue Aufnahme nötig (ist schon passiert).
        pending_transcript: Optional[str] = None

        while True:
            try:
                if pending_transcript is not None:
                    transcript = pending_transcript
                    pending_transcript = None
                else:
                    # 1. Aufnahme bis Stille. Zwei unterschiedliche Schwellen
                    # (siehe Klassen-Konstanten oben): 5s BEVOR überhaupt
                    # Sprache erkannt wurde (Kind reagiert gar nicht →
                    # Abbruch), aber nur 1.4s NACH erkannter Sprache
                    # (Satzende, Siri-artige Reaktionszeit) — kein Zeitlimit
                    # fürs eigentliche Reden selbst (MAX_TURN_SECONDS).
                    rec = self.recorder.record_until_silence(
                        max_seconds=self.MAX_TURN_SECONDS,
                        silence_seconds=self.TURN_SILENCE_SECONDS,
                        initial_silence_seconds=self.SILENCE_TIMEOUT,
                        cancel_event=cancel_event,
                    )
                    if rec is not None and rec.cancelled:
                        logger.info("KI-Modus: hart abgebrochen (Blau-Knopf).")
                        return None
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

                # 4. Intent auslesen. Zauberwort-Modus betrifft NUR Song-
                # Wünsche (User-Klarstellung 2026-07-09): ohne "bitte" im
                # TRANSKRIPT (nicht im Antworttext!) wird play_song zu answer
                # umgewidmet + eine Erinnerung gesprochen — Witze/Geschichten/
                # Fragen sind davon komplett unberührt, unabhängig ob "bitte"
                # gesagt wurde. Deterministischer Backstop, falls Claude die
                # Regel aus dem System-Prompt trotzdem mal ignoriert.
                intent = result.get("intent", "answer")
                response_text = result.get("response", "")
                if (
                    intent == "play_song"
                    and box_config.get("zauberwort_mode_enabled")
                    and not has_magic_word(transcript)
                ):
                    logger.info("KI-Modus: Zauberwort-Mode aktiv, 'bitte' fehlt — kein Playback.")
                    intent = "answer"
                    response_text = "Du musst noch 'bitte' sagen, wenn du ein Lied hören möchtest!"

                if not self.safety.is_safe(response_text, box_config):
                    logger.warning("KI-Modus: Response blockiert (SafetyFilter)")
                    response_text = "Davon kann ich dir nicht erzählen."

                # 5. Memory (play_song braucht keine gesprochene Antwort —
                # main.py spielt direkt den Song).
                self._add_to_history(transcript, response_text)

                if intent == "play_song":
                    action = result.get("action") or {}
                    song_id = action.get("song_id")
                    song_title = action.get("song_title")
                    logger.info(f"KI-Modus: Play-Intent → {song_title}")
                    return {"intent": "play_song", "song_id": song_id, "song_title": song_title}

                if intent == "goodbye":
                    # Nicht unterbrechbar — Konversation endet hier ohnehin;
                    # ein Barge-in während des Abschieds würde sonst verworfen
                    # (bewusste Vereinfachung, siehe _speak_interruptible-Doku).
                    self._speak(response_text)
                    logger.info("KI-Modus: Goodbye-Intent, beende")
                    return None

                # 6. Antwort abspielen UND gleichzeitig auf Barge-in bzw. den
                # nächsten Turn lauschen (wie ChatGPT Voice Mode) — redet das
                # Kind rein, wird die Wiedergabe sofort gestoppt.
                barged_in, next_transcript, cancelled = self._speak_interruptible(response_text)
                if cancelled:
                    logger.info("KI-Modus: hart abgebrochen (Blau-Knopf während Antwort).")
                    return None
                if barged_in:
                    if not next_transcript:
                        logger.info("KI-Modus: nach Antwort Sprache erkannt, ASR leer — beende")
                        return None
                    pending_transcript = next_transcript
                # sonst: pending_transcript bleibt None → nächste Iteration
                # nimmt normal auf (Antwort lief ungestört durch).

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
