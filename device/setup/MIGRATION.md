# Neue Kakabox aufsetzen (Migration auf eine zweite Pi)

So bringst du eine **separate, eigenständige** Kakabox auf eine neue Raspberry Pi 5
— parallel zur bestehenden Box, mit **eigener Identität**. Kein `dd`-Image-Klon:
der würde Serial/Token/`hardware_serial` duplizieren, und beide Boxen würden sich
beim Backend gegenseitig die Anmeldung wegnehmen.

Das `install-wifi-onboarding.sh` ist seit der User-/Pfad-Parametrisierung
vollständig: es zieht alle System-Pakete, Modelle, das Audio-HAT-Overlay, den
venv und die Services. Du musst nichts mehr von Hand zusammensuchen.

---

## Was wovon kopiert wird

| Kategorie | Quelle | Auf der neuen Box |
|---|---|---|
| App-Code + Setup | `git clone` | identisch |
| System-Setup (Pakete, venv, Overlay, Modelle, Services) | `install-wifi-onboarding.sh` | reproduziert |
| **Identität** (`box_identity.json`) | **NEU registrieren** | eigene Serial + eigener Token |
| Box-Settings (`config.json`) | aus `config.example.json` | frisch, dann anpassen |
| NFC-Zuordnung (`tag_cache.json` / `config.json["tags"]`) | leer | Max' eigene Tags |
| Audio (`audio_cache/`, `voice_catalog.json`) | **nicht kopieren** | synct selbst vom Backend |

---

## Schritte

### 1. Raspberry Pi OS
- Frisches **Raspberry Pi OS (64-bit, Trixie/Bookworm)** auf SD/NVMe flashen.
- Beim Imager: Hostname z. B. `kakabox-max`, denselben Linux-User wie geplant
  anlegen (kann ein anderer User als auf der bestehenden Box sein — das Skript
  leitet User & Pfad automatisch ab).
- SSH/WLAN für die Ersteinrichtung aktivieren, einmal booten, `sudo apt update && sudo apt full-upgrade`.

### 2. Repo klonen
```bash
git clone <repo-url> ~/kakabox       # Pfad frei wählbar
cd ~/kakabox
```

### 3. Box im kakaland-Backend registrieren (→ Identität)
- In der **kakaland-Webapp** eine neue Box anlegen → du bekommst eine
  `serial_number` + `activation_code`.
- `device/box_identity.json` anlegen (NICHT von der alten Box kopieren):
  ```json
  {
    "serial_number": "KB-2026-XXXXXX",
    "activation_code": "<code-aus-webapp>",
    "model": "Raspberry Pi 5 Model B",
    "registered_at": "pending"
  }
  ```
  Den `api_token` **nicht** eintragen — die Box holt ihn beim ersten
  `/api/box/connect` automatisch und schreibt ihn selbst in die Datei
  (siehe `network/backend.py::connect`).

### 4. Start-Config anlegen
```bash
cp device/config.example.json device/config.json
# volume / max_volume / tts_voice nach Geschmack anpassen; "tags" bleibt leer
```

### 5. Provisioning ausführen
```bash
sudo bash device/setup/install-wifi-onboarding.sh
```
Das Skript:
1. installiert Pakete (comitup, mpv, espeak-ng, Build-Tools, venv-Deps)
2. richtet comitup + Captive-Portal-Branding ein
3. legt Audio-Ansagen + Piper-TTS-Stimmen + Whisper-`ggml-tiny` ab
4. installiert das **Audio-HAT-Overlay** und patcht `config.txt`
5. baut den **venv** und installiert die Requirements
6. installiert `kakabox.service` (+ `backend.conf` → `https://kakaland.de`)
7. startet comitup neu (kappt ggf. die SSH-Session — am Gerät sitzen!)

> ⚠ **Reboot:** Wenn das Audio-HAT-Overlay neu in `config.txt` eingetragen wurde,
> meldet das Skript das und du musst **einmal `sudo reboot`** — sonst fehlt die
> ALSA-Karte `sndrpigooglevoi` und die Box hat keinen Ton.

### 6. WLAN + Aktivierung
- Box bootet → falls kein WLAN bekannt: Hotspot **`Kakabox-XXXX`** → per Phone
  verbinden, Captive-Portal, Heim-WLAN auswählen.
- Sobald online: `kakabox.service` ruft `/api/box/connect` mit serial+code, holt
  den `api_token`, schreibt ihn in `box_identity.json`, und startet Heartbeat +
  Audio-Sync. Der `audio_cache` füllt sich von selbst.

### 7. NFC-Tags für Max
- Tags neu zuordnen (eigene Tags, eigene Alben) — `tag_cache.json` /
  `config.json["tags"]` startet leer und wird über das normale Tag-Anlern-/
  Backend-Verfahren befüllt.

---

## Checks nach dem Boot
```bash
aplay -l | grep -i googlevoi          # Audio-Karte da?
systemctl status kakabox.service      # läuft?
journalctl -u kakabox.service -f      # "Connected to backend as box id=..."?
~/kakabox/device/.venv/bin/python -c "import vosk, pywhispercpp, piper"  # Voice-Deps ok?
```

## Häufige Stolpersteine
- **Kein Ton** → Reboot nach Overlay-Eintrag vergessen, oder Overlay nicht in
  `config.txt` (Skript-Schritt 5/8 prüfen). `aplay -l` muss `sndrpigooglevoi` zeigen.
- **HTTP 401 beim connect** → `serial_number`/`activation_code` stimmen nicht mit
  der Webapp-Registrierung überein, oder Box dort noch nicht angelegt.
- **`pywhispercpp`-Build schlägt fehl** → `cmake` + `build-essential` fehlen
  (installiert das Skript eigentlich); Voice ist best-effort, Box läuft trotzdem.
