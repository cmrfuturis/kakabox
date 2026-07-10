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
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from itertools import chain
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from voice.intent import has_magic_word

logger = logging.getLogger(__name__)

# Ersatztext, wenn der SafetyFilter eine Antwort (oder einen einzelnen Satz
# im Streaming-Modus) blockiert.
BLOCKED_TEXT = "Davon kann ich dir nicht erzählen."

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

# Wortanfangs-Grenze statt rohem Substring (Review-Finding: "arsch" matchte
# in "Marsch", "geist" in "begeistert" — harmlose Antworten wurden blockiert).
# \b + Wort erlaubt weiterhin Flexionen ("ficken", "gruselig"), verlangt aber
# eine Wortgrenze VOR dem Treffer.
_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(sorted(FORBIDDEN_WORDS, key=len, reverse=True)) + r")",
    re.IGNORECASE,
)
_SCARY_RE = re.compile(
    r"\b(" + "|".join(sorted(SCARY_TOPICS, key=len, reverse=True)) + r")",
    re.IGNORECASE,
)


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
        # Vulgär / Rassistisch / Feindselig
        m = _FORBIDDEN_RE.search(text)
        if m:
            logger.warning(f"SafetyFilter: blocked forbidden word '{m.group(1)}'")
            return False

        # Nachtmodus: keine gruselig/Action-Inhalte. quiet_hours_active ist
        # der von main.py AUSGEWERTETE Ist-Zustand (bool) — der alte Key
        # quiet_hours enthielt den kompletten ZEITPLAN und war truthy, sobald
        # Eltern irgendein Fenster konfiguriert hatten (Review-Finding: die KI
        # lief dann 24/7 im Nachtmodus, auch mittags). Fallback auf den alten
        # Key bewusst NICHT — lieber kein Nachtmodus-Block als Dauer-Block.
        if box_config.get("quiet_hours_active"):
            m = _SCARY_RE.search(text)
            if m:
                logger.warning(f"SafetyFilter: quiet hours, scary topic '{m.group(1)}' blocked")
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
    # Containment nur bei ausreichend langem Gehörten (Review-Finding: das
    # Kind ruft "Bitte" in die Zauberwort-Erinnerung "Du musst noch 'bitte'
    # sagen …" — 'bitte' ist Substring der Antwort und wurde als Selbst-Echo
    # verworfen, obwohl es die ECHTE Kind-Antwort war. Kurze Ein-Wort-Antworten
    # ("Ja", "Nein", "Bitte") sind praktisch nie ein brauchbares Echo-Fragment).
    if min(len(a), len(b)) >= 15 and (a in b or b in a):
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


# Satzgrenze fürs Streaming-TTS: nach [.!?…] + Whitespace, aber nur wenn danach
# ein Großbuchstabe/Zahl/öffnendes Anführungszeichen folgt — verhindert Splits
# mitten in Abkürzungen ("z.B. der Hund" bleibt zusammen, weil "der" klein
# beginnt). Zeilenumbrüche sind IMMER eine Grenze (Claude nutzt sie zwischen
# Absätzen).
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÄÖÜ0-9„»\"'])|\n+")
# Latenz-/Speicher-Bound: kommt sehr lange kein Satzende, wird am letzten
# Leerzeichen getrennt, damit die Wiedergabe nicht beliebig auf das Satzende
# warten muss.
_SENTENCE_MAX_BUFFER = 300


def _iter_sentences(chunks: Iterator[str]) -> Iterator[str]:
    """Schneidet einen Text-Chunk-Strom (beliebige Stückelung, z.B. LLM-Token)
    an Satzgrenzen und liefert vollständige Sätze, sobald sie komplett sind —
    Herzstück des Streaming-Sprechens: Satz 1 kann schon abgespielt werden,
    während der Server Satz 2 noch generiert."""
    buf = ""
    for piece in chunks:
        if not piece:
            continue
        buf += piece
        while True:
            m = _SENTENCE_BOUNDARY_RE.search(buf)
            if m:
                sent, buf = buf[: m.start()], buf[m.end():]
                sent = sent.strip()
                if sent:
                    yield sent
                continue
            if len(buf) > _SENTENCE_MAX_BUFFER:
                cut = buf.rfind(" ", 0, _SENTENCE_MAX_BUFFER)
                if cut > 0:
                    sent, buf = buf[:cut].strip(), buf[cut + 1:]
                    if sent:
                        yield sent
                    continue
            break
    tail = buf.strip()
    if tail:
        yield tail


def _split_meta_and_text(chunks: Iterator[str]) -> tuple[Optional[dict], Iterator[str]]:
    """Trennt das Streaming-Antwortformat des Servers in (Meta, Text-Strom).

    Format: erste nicht-leere Zeile = kompaktes JSON ({"intent": ..., "action":
    ..., "confidence": ...}), danach der reine Sprechtext. Markdown-Fence-Zeilen
    (```/```json) vor der Meta werden übersprungen — Claude wickelt Antworten
    gelegentlich trotz Anweisung in Codeblocks (live beobachtet beim
    Nicht-Streaming-Format).

    Ist die erste inhaltliche Zeile KEIN JSON, gibt es keine Meta (None) und
    die Zeile gehört zum Sprechtext — der Aufrufer behandelt die ganze Antwort
    dann als intent="answer".
    """
    it = iter(chunks)
    buf = ""
    meta: Optional[dict] = None
    while True:
        nl = buf.find("\n")
        if nl == -1:
            piece = next(it, None)
            if piece is None:
                line, buf, exhausted = buf, "", True
            else:
                buf += piece
                continue
        else:
            line, buf, exhausted = buf[:nl], buf[nl + 1:], False
        candidate = line.strip()
        if candidate == "" or candidate.startswith("```"):
            if exhausted:
                break
            continue  # Leer-/Fence-Zeilen vor der Meta überspringen
        try:
            parsed = json.loads(candidate)
            meta = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            meta = None
        if meta is None:
            # Zeile ist kein Meta-JSON → sie ist Teil des Sprechtexts.
            buf = line + ("\n" + buf if not exhausted else "")
        break
    text_chunks = chain([buf] if buf else [], it)
    return meta, text_chunks


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

    def _open_stream(self, transcript: str, box_config: dict, catalog: list[dict]):
        """POST /api/box/assistant mit stream=true — bevorzugter Pfad: die
        Antwort kommt als Text-Strom und kann satzweise gesprochen werden,
        BEVOR sie komplett generiert ist (Siri-artige Reaktionszeit).

        Abwärtskompatibel: ein alter Server ohne Streaming-Support ignoriert
        das unbekannte stream-Feld in der Validierung und antwortet klassisch
        mit dem vollen JSON — erkennbar am Content-Type application/json.

        Returns:
            ("stream", meta, text_chunks, response): Server streamt. ``meta``
                ist das Intent-JSON der ersten Zeile, ``text_chunks`` der
                Sprechtext-Strom, ``response`` das offene requests-Response
                (zum Abbrechen per .close()).
            ("legacy", result_dict, None, None): klassische Voll-JSON-Antwort.
            None: Fehler (Netz/HTTP/Status) — Aufrufer entscheidet über
                Offline-Meldung anhand backend.is_connected.
        """
        if not self.backend or not self.backend.is_connected:
            return None

        # Server validiert transcript mit max:500 — ein 60s-Redeturn kann
        # länger transkribieren und würde sonst mit 422 den ganzen Turn
        # abbrechen (Review-Finding). Vorne kappen wäre falsch (der Anfang
        # trägt meist den Intent), also hinten.
        transcript = transcript[:500]

        context = {
            "child_age": self.child_age,
            "conversation_history": self.history[-5:],
            "box_config": box_config,
            "catalog": catalog,
            "stream": True,
        }
        if self.now_playing:
            context["now_playing"] = self.now_playing

        try:
            # (connect, read): read = max. Lücke ZWISCHEN zwei Chunks, nicht
            # Gesamtdauer — der Stream darf insgesamt länger laufen.
            response = self.backend._session.post(
                f"{self.backend._base_url}/api/box/assistant",
                json={"transcript": transcript, **context},
                headers=self.backend._auth_headers(),
                timeout=(3.05, 30),
                stream=True,
            )
        except Exception as e:
            logger.warning(f"Assistant stream request failed: {e}")
            return None

        if response.status_code == 503:
            logger.warning(f"Assistant disabled or unavailable (503): {response.text[:200]}")
            response.close()
            return None
        if not response.ok:
            logger.warning(f"Assistant HTTP {response.status_code}: {response.text[:200]}")
            response.close()
            return None

        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            # Alter Server ohne Streaming — klassische Voll-JSON-Antwort.
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Assistant response parse error: {e}")
                return None
            finally:
                response.close()
            if data.get("status") != "ok":
                logger.warning(f"Assistant response status != ok: {data}")
                return None
            return ("legacy", data, None, None)

        response.encoding = "utf-8"
        try:
            chunks = response.iter_content(chunk_size=None, decode_unicode=True)
            meta, text_chunks = _split_meta_and_text(chunks)
        except Exception as e:
            logger.warning(f"Assistant stream read failed: {e}")
            response.close()
            return None
        if meta is None:
            # Keine Meta-Zeile — gesamte Antwort als einfachen Antworttext
            # behandeln (defensiv gegen Format-Ausreißer von Claude).
            logger.warning("Assistant stream ohne Meta-Zeile — behandle alles als Antworttext.")
            meta = {"intent": "answer", "confidence": 0.0}
        return ("stream", meta, text_chunks, response)

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

    def _speak_sentences_blocking(self, sentences: Iterator[str]) -> str:
        """Spricht alle Sätze nacheinander, NICHT unterbrechbar (für goodbye).
        Gibt den tatsächlich gesprochenen Gesamttext zurück."""
        spoken: list[str] = []
        for sent in sentences:
            s = _strip_emoji(sent).strip().strip("`").strip()
            if not s:
                continue
            spoken.append(s)
            self._speak(s)
        return " ".join(spoken)

    def _speak_sentences_interruptible(
        self, sentences: Iterator[str], box_config: dict,
        close_stream: Optional[Callable[[], None]] = None,
    ) -> dict:
        """Spricht Sätze aus ``sentences`` (ggf. live vom Server-Stream) und
        lauscht GLEICHZEITIG aufs Mikrofon — redet das Kind rein (Barge-in,
        wie ChatGPT Voice Mode), stoppt die Wiedergabe SOFORT.

        Architektur: ein Speaker-Thread konsumiert die Sätze (blockiert dabei
        ggf. auf dem Netzwerk-Stream), synthetisiert den NÄCHSTEN Satz, während
        der vorige noch spielt (Pipeline: Netz ∥ TTS ∥ Wiedergabe), und prüft
        jeden Satz einzeln gegen den SafetyFilter. Der aufrufende Thread
        lauscht parallel in wiederholten Aufnahme-Fenstern: solange die
        Wiedergabe läuft, beendet ein sprachloses Fenster NICHTS (behebt den
        Vorgänger-Bug, bei dem eine Antwort > ~5s nach dem ersten stillen
        Fenster per player.stop() abgeschnitten wurde, wenn das Mikro die
        Box-Stimme nicht hörte). Erst ein volles Stille-Fenster, das NACH dem
        Wiedergabe-Ende begann, beendet die Konversation.

        Selbst-Echo (Mikro hört die eigene TTS-Stimme, keine Hardware-AEC)
        wird über _looks_like_self_echo gegen ALLES bisher Gesprochene
        gefiltert und ignoriert — die Wiedergabe läuft dann weiter.

        Returns dict:
            outcome: "silence"          — Stille nach Antwortende → Konversation beenden
                     "barge_in"         — echtes Reinreden; transcript = nächster Turn
                     "cancelled"        — harter Stopp (Blau-Knopf)
                     "done_no_listener" — gesprochen ohne Barge-in-Fähigkeit
                                          (kein Recorder/ASR); Aufrufer nimmt
                                          danach normal den nächsten Turn auf
            transcript: bei barge_in der erkannte Text (None wenn ASR leer)
            full_text: alles tatsächlich Gesprochene (für History/Echo-Check)
        """
        spoken_parts: list[str] = []

        def _close_stream_quiet() -> None:
            if close_stream:
                try:
                    close_stream()
                except Exception:
                    pass

        if not self.speaker or not self.player:
            # Keine Sprachausgabe möglich — Stream nicht ewig offen halten.
            _close_stream_quiet()
            return {"outcome": "done_no_listener", "transcript": None, "full_text": ""}

        if not self.recorder or not self.transcribe_fn:
            # Kein Barge-in möglich → blockierend sprechen; der Aufrufer nimmt
            # danach ganz normal den nächsten Turn auf.
            full = self._speak_sentences_blocking(sentences)
            _close_stream_quiet()
            return {"outcome": "done_no_listener", "transcript": None, "full_text": full}

        stop_event = threading.Event()
        playback_done = threading.Event()

        def _speaker_thread() -> None:
            playing = False
            try:
                for sent in sentences:
                    if stop_event.is_set():
                        break
                    s = _strip_emoji(sent).strip().strip("`").strip()
                    if not s:
                        continue
                    stop_after = False
                    if not self.safety.is_safe(s, box_config):
                        logger.warning("KI-Modus: Satz blockiert (SafetyFilter) — Rest der Antwort verworfen.")
                        s = BLOCKED_TEXT
                        stop_after = True
                    try:
                        # Synthese läuft, WÄHREND der vorige Satz noch spielt.
                        wav = self.speaker.synth_to_wav(s)
                    except Exception as e:
                        logger.warning(f"KI-Modus: TTS-Synthese fehlgeschlagen: {e}")
                        continue
                    if wav is None:
                        continue
                    if playing:
                        self.player.wait_until_idle(timeout=30.0)
                    if stop_event.is_set():
                        break
                    spoken_parts.append(s)
                    self.player.play_prompt(str(wav), self.volume)
                    playing = True
                    if stop_after:
                        break
                if playing and not stop_event.is_set():
                    self.player.wait_until_idle(timeout=30.0)
            except Exception as e:
                # Auch der Abbruch-Fall landet hier: close_stream() lässt das
                # blockierende Netzwerk-Read im Satz-Iterator eine Exception
                # werfen — gewollt, kein Fehler.
                logger.info(f"KI-Modus: Streaming-Wiedergabe beendet: {e}")
            finally:
                playback_done.set()

        speaker_thread = threading.Thread(
            target=_speaker_thread, daemon=True, name="ki-tts-stream",
        )
        speaker_thread.start()

        def _shutdown_playback() -> None:
            stop_event.set()
            _close_stream_quiet()
            try:
                self.player.stop()
            except Exception as e:
                logger.warning(f"KI-Modus: Stop nach Antwort fehlgeschlagen: {e}")
            speaker_thread.join(timeout=5.0)

        while True:
            # Merken, ob dieses Lauschfenster erst NACH dem Wiedergabe-Ende
            # begann — nur dann darf ein sprachloses Fenster die Konversation
            # beenden (sonst würde eine lange Antwort das 5s-Fenster sprengen).
            window_after_playback = playback_done.is_set()
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

            if (rec is not None and rec.cancelled) or (
                self._cancel_event is not None and self._cancel_event.is_set()
            ):
                _shutdown_playback()
                return {"outcome": "cancelled", "transcript": None,
                        "full_text": " ".join(spoken_parts)}

            if rec is not None and rec.speech_seen:
                try:
                    transcript = self.transcribe_fn(rec.path) or ""
                except Exception as e:
                    logger.warning(f"KI-Modus: Barge-in ASR-Fehler: {e}")
                    transcript = ""
                if transcript and _looks_like_self_echo(" ".join(spoken_parts), transcript):
                    logger.info(f"KI-Modus: Barge-in ignoriert (vermutlich eigene Stimme): «{transcript}»")
                    continue  # weiterlauschen, Wiedergabe läuft ungestört weiter
                _shutdown_playback()
                return {"outcome": "barge_in", "transcript": transcript or None,
                        "full_text": " ".join(spoken_parts)}

            # Kein Speech in diesem Fenster:
            if window_after_playback:
                # Volles Stille-Fenster NACH Antwortende → Konversation zu Ende.
                _shutdown_playback()  # nur Aufräumen, Wiedergabe ist längst durch
                return {"outcome": "silence", "transcript": None,
                        "full_text": " ".join(spoken_parts)}
            if rec is None:
                # Aufnahmefehler, Wiedergabe läuft noch — nicht heiß loopen.
                time.sleep(0.5)
            # sonst: Fenster überlappte die Wiedergabe → einfach weiterlauschen.

    def _add_to_history(self, user_text: str, bot_response: str) -> None:
        """Speichert einen Konversations-Turn im Memory.

        Turns mit leerem Transkript ODER leerer Antwort werden verworfen
        (Review-Finding: beim Streaming kann full_text leer sein, wenn ein
        Barge-in/Stopp VOR dem ersten gesprochenen Satz kam — ein leerer
        assistant-Turn in der History würde alle Folge-Requests an die
        Anthropic-API mit 400 "all messages must have non-empty content"
        scheitern lassen).
        """
        if not (user_text or "").strip() or not (bot_response or "").strip():
            return
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

        # Von _speak_sentences_interruptible() erkannter Barge-in: das Kind hat schon
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

                # Harter Stopp konnte während der ASR gedrückt worden sein —
                # cancel_event wird sonst nur INNERHALB der Aufnahme geprüft
                # (Review-Finding: Blau-Druck im mehrsekündigen "Denk-Fenster"
                # ASR→Claude wurde verschluckt und der Song startete trotzdem).
                if cancel_event is not None and cancel_event.is_set():
                    logger.info("KI-Modus: hart abgebrochen (Blau-Knopf nach ASR).")
                    return None

                # 3. Claude fragen — bevorzugt STREAMEND: die Antwort wird
                # satzweise gesprochen, sobald der erste Satz generiert ist,
                # statt auf das komplette Ergebnis zu warten. Ein alter Server
                # ohne Streaming antwortet klassisch mit Voll-JSON ("legacy").
                opened = self._open_stream(transcript, box_config, catalog)

                # Zweites Denk-Fenster: der Claude-Request selbst (bis zu
                # mehrere Sekunden) — auch hier darf ein zwischenzeitlicher
                # Blau-Druck nicht verloren gehen.
                if cancel_event is not None and cancel_event.is_set():
                    logger.info("KI-Modus: hart abgebrochen (Blau-Knopf während Claude-Anfrage).")
                    if opened is not None and opened[3] is not None:
                        try:
                            opened[3].close()
                        except Exception:
                            pass
                    return None

                if opened is None:
                    # "offline" nur, wenn WIRKLICH keine Verbindung mehr besteht
                    # (User-Wunsch) — ein serverseitiger Fehler (503/500/kaputtes
                    # JSON) bei bestehender Verbindung ist kein Offline-Zustand
                    # und soll nicht fälschlich als solcher angesagt werden.
                    if not self.backend or not self.backend.is_connected:
                        logger.warning("KI-Modus: Verbindung während Konversation verloren.")
                        return {"intent": "offline"}
                    logger.warning("KI-Modus: Server-Fehler (Verbindung besteht) — Abbruch.")
                    return None

                kind, meta, text_chunks, stream_response = opened
                close_stream: Optional[Callable[[], None]] = (
                    stream_response.close if stream_response is not None else None
                )
                # Beim Legacy-Pfad liegt der komplette Antworttext schon vor;
                # beim Stream-Pfad kommt er erst noch (text_chunks).
                legacy_text: Optional[str] = (
                    meta.get("response", "") if kind == "legacy" else None
                )

                # 4. Intent + Zauberwort-Gate. Zauberwort-Modus betrifft NUR
                # Song-Wünsche (User-Klarstellung 2026-07-09): ohne "bitte" im
                # TRANSKRIPT (nicht im Antworttext!) wird play_song zu answer
                # umgewidmet + eine Erinnerung gesprochen — Witze/Geschichten/
                # Fragen sind davon komplett unberührt. Deterministischer
                # Backstop, falls Claude die System-Prompt-Regel ignoriert.
                # "bitte" aus dem UNMITTELBAR vorigen Kind-Turn zählt mit
                # (Review-Finding: "Kannst du bitte ein Lied spielen?" →
                # Rückfrage "Welches?" → "Das rote Pferd" wurde geblockt,
                # obwohl das Kind einen Turn vorher "bitte" gesagt hatte).
                intent = meta.get("intent", "answer")
                said_bitte = has_magic_word(transcript) or (
                    bool(self.history)
                    and has_magic_word(self.history[-1].get("transcript", ""))
                )
                if (
                    intent == "play_song"
                    and box_config.get("zauberwort_mode_enabled")
                    and not said_bitte
                ):
                    logger.info("KI-Modus: Zauberwort-Mode aktiv, 'bitte' fehlt — kein Playback.")
                    intent = "answer"
                    if close_stream is not None:
                        # Rest der Stream-Antwort verwerfen — gesprochen wird
                        # nur die Erinnerung.
                        try:
                            close_stream()
                        except Exception:
                            pass
                        close_stream = None
                        text_chunks = None
                    legacy_text = "Du musst noch 'bitte' sagen, wenn du ein Lied hören möchtest!"

                if intent == "play_song":
                    if close_stream is not None:
                        # Die Bestätigung ("Ich spiele …") spricht main.py —
                        # der restliche Stream-Text wird nicht gebraucht.
                        try:
                            close_stream()
                        except Exception:
                            pass
                    action = meta.get("action") or {}
                    song_id = action.get("song_id")
                    song_title = action.get("song_title")
                    logger.info(f"KI-Modus: Play-Intent → {song_title}")
                    self._add_to_history(
                        transcript,
                        legacy_text or (f"Ich spiele {song_title}." if song_title else ""),
                    )
                    return {"intent": "play_song", "song_id": song_id, "song_title": song_title}

                # 5. Sprech-Quelle vereinheitlichen: Legacy-/Ersatztexte werden
                # über denselben Satz-Iterator gesprochen wie der Live-Stream.
                if legacy_text is not None:
                    if not self.safety.is_safe(legacy_text, box_config):
                        logger.warning("KI-Modus: Response blockiert (SafetyFilter)")
                        legacy_text = BLOCKED_TEXT
                    sentences = _iter_sentences(iter([legacy_text]))
                else:
                    # Stream-Pfad: SafetyFilter läuft pro Satz im Speaker-Thread.
                    sentences = _iter_sentences(text_chunks)

                if intent == "goodbye":
                    # Nicht unterbrechbar — Konversation endet hier ohnehin.
                    full = self._speak_sentences_blocking(sentences)
                    if close_stream is not None:
                        try:
                            close_stream()
                        except Exception:
                            pass
                    self._add_to_history(transcript, full or (legacy_text or ""))
                    logger.info("KI-Modus: Goodbye-Intent, beende")
                    return None

                # 6. Antwort abspielen UND gleichzeitig auf Barge-in bzw. den
                # nächsten Turn lauschen (wie ChatGPT Voice Mode) — redet das
                # Kind rein, wird die Wiedergabe sofort gestoppt. Das Lauschen
                # deckt auch die Nach-Antwort-Phase ab ("silence" = 5s Stille
                # nach Antwortende → Konversation vorbei).
                outcome = self._speak_sentences_interruptible(
                    sentences, box_config, close_stream=close_stream,
                )
                self._add_to_history(
                    transcript, outcome["full_text"] or (legacy_text or ""),
                )

                if outcome["outcome"] == "cancelled":
                    logger.info("KI-Modus: hart abgebrochen (Blau-Knopf während Antwort).")
                    return None
                if outcome["outcome"] == "silence":
                    logger.info("KI-Modus: Stille nach Antwort, beende")
                    return None
                if outcome["outcome"] == "barge_in":
                    if not outcome["transcript"]:
                        logger.info("KI-Modus: nach Antwort Sprache erkannt, ASR leer — beende")
                        return None
                    pending_transcript = outcome["transcript"]
                # "done_no_listener": kein Barge-in-fähiges Setup — nächste
                # Iteration nimmt ganz normal den nächsten Turn auf.

            except Exception as e:
                logger.exception(f"KI-Modus: Fehler in Loop: {e}")
                return None
