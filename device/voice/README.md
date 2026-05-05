# Voice-Eingabe

**Status:** Intent-Parser + ASR-Wrapper sind fertig und unabhängig testbar.
Push-to-Talk-Verkabelung an einen Knopf kommt erst, sobald das Mikrofon
physisch angesteckt ist.

## Architektur

```
[Audio]  ─►  ASR  ─►  Text  ─►  Intent-Parser  ─►  Catalog-Match  ─►  Action
            Vosk          (regex+stopwords)        (difflib)            (Player)
```

Bewusst lean gehalten: keine LLM-Pipeline, kein Wake-Word, keine ständige
Mic-Aufnahme. Push-to-Talk auf einen Knopf → 3 s Aufnahme → Vosk → Text →
Stopwords raus → Fuzzy-Match gegen den lokalen Catalog → spielen.

| Modul | Zweck |
|---|---|
| `intent.py` | Pure-Python Intent-Parser (`parse_play_command`). Keine externen Deps. |
| `asr.py` | Vosk-Wrapper. `VoiceUnavailable` wenn Modell oder Paket fehlen. |
| `__main__.py` | CLI für Trockentests: `python -m voice "spiele bambi"` |

## Setup auf der Box (sobald Mikrofon da ist)

### 1. Python-Deps in den venv
```bash
.venv/bin/pip install -r device/requirements-voice.txt
```
Das installiert `vosk` + `numpy`. Auf Pi 5 ca. 2–3 Minuten.

### 2. Deutsches Vosk-Modell (klein, ~50 MB)
```bash
sudo mkdir -p /usr/share/kakabox/voice
cd /tmp
wget https://alphacephei.com/vosk/models/vosk-model-small-de-0.15.zip
sudo unzip vosk-model-small-de-0.15.zip -d /usr/share/kakabox/voice/
```

Für höhere Genauigkeit gibt es `vosk-model-de-0.21` (~1.5 GB) — auf der
Pi 5 mit 8/16 GB RAM kein Problem, aber für unseren Constrained-Vocab-
Anwendungsfall bringt's wenig.

### 3. Mikrofon-Hardware

Empfohlen: **ReSpeaker 2-Mics Pi HAT** oder ein USB-Mic. Beide tauchen als
ALSA-Capture-Device auf. Das ASR erwartet **16 kHz mono 16-bit WAV**.

Aufnahme-Test (Push-to-Talk-Simulation):
```bash
arecord -D plughw:CARD=ReSpeaker -f S16_LE -r 16000 -c 1 -d 4 /tmp/test.wav
.venv/bin/python -m voice --wav /tmp/test.wav
```

### 4. Trockentest ohne Mikrofon
```bash
cd device
.venv/bin/python -m voice "spiele bitte das Dschungelbuch"
```

### 5. Unit-Tests
```bash
cd device
.venv/bin/python -m pytest tests/test_voice_intent.py -v
```

## Erwartete Performance auf Pi 5

| Schritt | typische Latenz |
|---|---|
| Push-to-Talk-Aufnahme (3 s Sprache) | 3 s |
| Vosk small model (transcribe) | ~1–2 s |
| Intent + Match | < 50 ms |
| **Gesamt: Knopf los → Audio startet** | **~4–5 s** |

Beim allerersten Aufruf: zusätzlich ~3 s Modell-Load (einmalig pro
Service-Lifetime, danach im RAM).

## Was noch nicht gemacht ist

- **Knopf-Verdrahtung**: welche Geste startet die Aufnahme? Vorschlag:
  grünen Knopf 1–2 s halten (Encoder-Push wäre auch denkbar). Aktuell
  nicht implementiert — wenn Mic da ist, einbauen wir das in `main.py`
  zusammen mit einem Aufnahme-Loop via `arecord` oder `sounddevice`.
- **Kaka-Catalog vom Backend**: aktuell matched die Voice-Befehle nur
  gegen die lokale Library (`audio.library.scan()`). Wenn die Box auch
  Backend-Kakas kennen soll, ist `_build_album_catalog` im CLI bzw. die
  Catalog-Quelle für die Live-Verkabelung um eine Kaka-Liste zu
  erweitern (Cache der Kaka-Namen + UIDs aus dem letzten Audio-Sync).
- **Wake-Word**: nicht vorhanden — und brauchen wir auch nicht, solange
  Push-to-Talk genutzt wird (Privacy-Vorteil im Kinderzimmer).

## Threshold-Tuning

`parse_play_command(text, catalog, threshold=0.55)` — der Wert ist ein
Mittelwert für „Kind nuschelt etwas, soll trotzdem matchen". Wenn:

- **zu viele Falsch-Triggers**: Threshold auf 0.65 hochziehen.
- **zu viele „nicht verstanden"**: Threshold auf 0.45 runterziehen.
- **gar kein Match wo einer hingehört**: Catalog-Namen prüfen — Vosk-
  Transkription liefert oft Kleinbuchstaben + ohne Sonderzeichen, der
  Catalog-Name könnte aber noch Marketing-Schreibweise haben.
