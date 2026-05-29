#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────
# Kakabox WLAN-Onboarding — Installer
#
# WAS DAS SCRIPT MACHT (komplettes Box-Provisioning, idempotent):
#   1. Installiert comitup, espeak-ng, mpv, Build-Tools & Python-venv-Deps
#   2. Konfiguriert comitup für Kakabox-Branding (eigene SSID, Callback, mDNS)
#   3. Generiert Audio-Ansagen (deutsch, via espeak-ng) für Hotspot/Connected
#   4. Lädt Piper-TTS-Stimmen + Whisper-ASR-Modell (ggml-tiny) nach /usr/share/kakabox
#   5. Installiert das Audio-HAT-Overlay (googlevoicehat-kakabox) + patcht config.txt
#   6. Legt den Python-venv an und installiert die Requirements
#   7. Installiert systemd-Unit kakabox.service + backend.conf-Drop-in
#   8. Ersetzt comitup-Templates mit Kakabox-Branding und startet comitup neu
#
# USER/PFAD-AGNOSTISCH: Box-User und Installationspfad werden automatisch aus
# dem Clone-Verzeichnis bzw. dem sudo-Aufrufer abgeleitet (RUN_USER/DEVICE_DIR).
# Die Templates kakabox.service/sudoers-kakabox/kakabox-comitup-callback tragen
# __RUN_USER__/__RUN_GROUP__/__DEVICE_DIR__-Tokens, die render() beim Install ersetzt.
#
# WICHTIG — VOR DEM AUSFÜHREN LESEN:
#   • Beim ersten Start nach Installation öffnet die Box einen offenen Hotspot
#     "Kakabox-XXXX". Sie ist dann nicht mehr im normalen Heim-WLAN.
#   • Das bricht eine bestehende SSH-Session vom Pi → vorher sicherstellen,
#     dass du physisch an der Pi sitzt oder Ethernet-Backup hast.
#   • Wenn schon ein WLAN-Profil eingerichtet ist (z. B. dein Heim-WLAN),
#     verwendet comitup das beim Boot weiterhin — Hotspot kommt nur, wenn KEIN
#     gespeichertes WLAN erreichbar ist.
#
# RESET (zurück in Hotspot-Modus):
#   • Roter Knopf 10 Sekunden gedrückt halten — main.py erkennt das via
#     gpiozero hold_time und triggert /usr/local/bin/kakabox-wifi-nuke
#     (sudoers-Drop-in erlaubt riffi NOPASSWD nur für genau diesen Pfad).
#   • Helper löscht alle 802-11-wireless-Profile und rebootet → Hotspot.
#
# AUSFÜHREN:
#   sudo bash device/setup/install-wifi-onboarding.sh
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo ausführen: sudo bash $0"
    exit 1
fi

# Nicht-interaktiver Modus (für bootstrap.sh / unbeaufsichtigte Erst-Provisionierung):
#   --yes / -y oder KAKABOX_ASSUME_YES=1  → keine Rückfrage vor dem comitup-Restart
#   KAKABOX_NO_COMITUP_RESTART=1          → comitup nur enablen, NICHT live neu starten
#       (kappt sonst die SSH-Session; beim Reboot startet comitup ohnehin frisch)
ASSUME_YES="${KAKABOX_ASSUME_YES:-0}"
NO_COMITUP_RESTART="${KAKABOX_NO_COMITUP_RESTART:-0}"
for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=1 ;;
        --no-comitup-restart) NO_COMITUP_RESTART=1 ;;
    esac
done

# Pfade — Skript-Verzeichnis robust ermitteln
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Box-User + Gruppe ableiten: bevorzugt der sudo-Aufrufer, sonst der Eigentümer
# des Clone-Verzeichnisses. So läuft das Provisioning unter beliebigem User/Pfad,
# ohne dass irgendeine Datei von Hand angepasst werden muss.
RUN_USER="${SUDO_USER:-}"
if [[ -z "$RUN_USER" || "$RUN_USER" == "root" ]]; then
    RUN_USER="$(stat -c %U "$DEVICE_DIR")"
fi
RUN_GROUP="$(id -gn "$RUN_USER")"
echo "ℹ Box-User: $RUN_USER  •  Gruppe: $RUN_GROUP  •  Pfad: $DEVICE_DIR"

# render <quelle> <ziel> <mode> — kopiert eine Template-Datei und ersetzt dabei
# die __RUN_USER__/__RUN_GROUP__/__DEVICE_DIR__-Tokens durch die echten Werte.
render() {
    local src="$1" dest="$2" mode="$3"
    sed -e "s|__DEVICE_DIR__|$DEVICE_DIR|g" \
        -e "s|__RUN_USER__|$RUN_USER|g" \
        -e "s|__RUN_GROUP__|$RUN_GROUP|g" \
        "$src" > "$dest"
    chmod "$mode" "$dest"
}

PROMPTS_DIR=/usr/share/kakabox/prompts
TTS_DIR=/usr/share/kakabox/tts
TTS_CACHE_DIR=/var/lib/kakabox/tts-cache
VOICE_DIR=/usr/share/kakabox/voice
# Whisper.cpp-ASR-Modell (klein & schnell genug für Kinderstimmen auf Pi 5;
# base war mit ~12 s/Befehl zu langsam). config.json zeigt auf ggml-tiny.bin.
WHISPER_MODEL=ggml-tiny.bin
WHISPER_MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_MODEL"
PIPER_VOICES_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE"
# Beide Stimmen ausliefern — die Box schaltet per Backend-Setting tts_voice um
# (männlich=thorsten, weiblich=kerstin). Format: "<modellname> <hf-unterpfad>".
TTS_MODELS_TO_FETCH=(
    "de_DE-thorsten-medium thorsten/medium"
    "de_DE-kerstin-low kerstin/low"
)
COMITUP_TEMPLATES_DIR=/usr/share/comitup/web/templates
COMITUP_CONF=/etc/comitup.conf
CALLBACK_PATH=/usr/local/bin/kakabox-comitup-callback
SYSTEMD_DIR=/etc/systemd/system

step() { echo -e "\n\033[1;36m==> $*\033[0m"; }

# ──────────────────────────────────────────────────────────────────────────
# 1. Pakete installieren
# ──────────────────────────────────────────────────────────────────────────
step "1/8 Pakete installieren"
DEBIAN_FRONTEND=noninteractive apt-get update
# comitup/nftables: WLAN-Onboarding. espeak-ng: Fallback-TTS. mpv: Audio-Player
# (Musik + Prompts, vom Callback und main.py aufgerufen). python3-gpiozero/-lgpio:
# GPIO über System-Site-Packages. python3-venv/-dev + build-essential + cmake:
# venv-Bau + native Wheels (RPi.GPIO kompiliert, pywhispercpp baut whisper.cpp).
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    comitup espeak-ng mpv nftables \
    python3-gpiozero python3-lgpio \
    python3-venv python3-dev build-essential cmake

# ──────────────────────────────────────────────────────────────────────────
# 2. comitup-Konfiguration
# ──────────────────────────────────────────────────────────────────────────
step "2/8 /etc/comitup.conf installieren"
install -m 0644 "$SCRIPT_DIR/comitup.conf" "$COMITUP_CONF"

# WLAN-Power-Save abschalten — sonst kappt der Pi-Treiber bei mittlerem
# Signal regelmäßig die Verbindung und comitup fällt auf Hotspot zurück.
step "WLAN-Power-Save deaktivieren (/etc/NetworkManager/conf.d/wifi-powersave.conf)"
install -m 0644 "$SCRIPT_DIR/wifi-powersave-off.conf" \
    /etc/NetworkManager/conf.d/wifi-powersave.conf

step "Callback-Script installieren ($CALLBACK_PATH)"
# render: ersetzt __DEVICE_DIR__ im CONFIG-Pfad durch das echte Clone-Verzeichnis.
render "$SCRIPT_DIR/kakabox-comitup-callback" "$CALLBACK_PATH" 0755

step "Hardware-Helper installieren (poweroff + wifi-clear + cpu-governor)"
install -m 0755 "$SCRIPT_DIR/kakabox-poweroff"     /usr/local/bin/kakabox-poweroff
install -m 0755 "$SCRIPT_DIR/kakabox-wifi-clear"   /usr/local/bin/kakabox-wifi-clear
install -m 0755 "$SCRIPT_DIR/kakabox-cpu-governor" /usr/local/bin/kakabox-cpu-governor
# Alten wifi-nuke entfernen, falls aus früherer Installation noch da
rm -f /usr/local/bin/kakabox-wifi-nuke
# render: ersetzt __RUN_USER__ durch den echten Box-User.
render "$SCRIPT_DIR/sudoers-kakabox" /etc/sudoers.d/kakabox 0440
# Validate sudoers (sicher gegen kaputte Datei)
visudo -cf /etc/sudoers.d/kakabox >/dev/null || {
    echo "⚠ sudoers-Datei invalid — entferne sie wieder, um System nicht zu blocken"
    rm -f /etc/sudoers.d/kakabox
    exit 1
}

# ──────────────────────────────────────────────────────────────────────────
# 3. Captive-Portal-Branding
# ──────────────────────────────────────────────────────────────────────────
if [[ -d "$COMITUP_TEMPLATES_DIR" ]]; then
    step "3/8 Captive-Portal-Templates ersetzen"
    # Backup nur einmal — beim ersten Lauf
    if [[ ! -d "${COMITUP_TEMPLATES_DIR}.orig" ]]; then
        cp -r "$COMITUP_TEMPLATES_DIR" "${COMITUP_TEMPLATES_DIR}.orig"
    fi
    install -m 0644 "$SCRIPT_DIR/branding/templates/index.html" "$COMITUP_TEMPLATES_DIR/index.html"
    install -m 0644 "$SCRIPT_DIR/branding/templates/confirm.html" "$COMITUP_TEMPLATES_DIR/confirm.html"
    install -m 0644 "$SCRIPT_DIR/branding/templates/connect.html" "$COMITUP_TEMPLATES_DIR/connect.html"

    # Fredoka-Font für Captive-Portal lokal ablegen — der Hotspot hat kein
    # Internet, deshalb wird der Font einmalig beim Install heruntergeladen.
    # Ablage unter css/, weil comitupweb.py nur /css /js /img als statische
    # Routen exposiert (siehe send_from_directory in comitupweb.py). Variable
    # Font, latin subset, Gewichte 300-700 in einer Datei (~30KB).
    FREDOKA_DEST="$COMITUP_TEMPLATES_DIR/css/Fredoka.woff2"
    FREDOKA_URL="https://cdn.jsdelivr.net/npm/@fontsource-variable/fredoka/files/fredoka-latin-wght-normal.woff2"
    if curl --fail --silent --show-error --location --retry 2 --max-time 20 \
            -o "$FREDOKA_DEST" "$FREDOKA_URL"; then
        chmod 0644 "$FREDOKA_DEST"
        echo "  ✓ Fredoka.woff2 ($(stat -c%s "$FREDOKA_DEST") Bytes) abgelegt"
    else
        echo "  ⚠ Fredoka konnte nicht geladen werden — Templates fallen auf system-ui zurück"
        rm -f "$FREDOKA_DEST"
    fi
else
    echo "⚠ Templates-Dir $COMITUP_TEMPLATES_DIR nicht gefunden — Branding übersprungen"
fi

# ──────────────────────────────────────────────────────────────────────────
# 4. Audio-Ansagen
#    Bevorzugt eigene WAV-Dateien aus branding/audio/ (von Hand eingesprochen).
#    Fehlt eine Datei dort, generiert espeak-ng eine TTS-Variante als Fallback,
#    damit die Box auch ohne Custom-Audio funktioniert.
# ──────────────────────────────────────────────────────────────────────────
step "4/8 Audio-Ansagen + Sprachmodelle installieren"
mkdir -p "$PROMPTS_DIR"

AUDIO_SRC="$SCRIPT_DIR/branding/audio"

# Mapping: Quelldatei (im Repo) → Zielname (von main.py + comitup-callback erwartet)
#   espeak-Fallback-Args: -v <lang> -s <speed> -p <pitch> "<text>"
install_prompt() {
    local src_name="$1" dest_name="$2" lang="$3" speed="$4" pitch="$5" text="$6"
    local src="$AUDIO_SRC/$src_name"
    local dest="$PROMPTS_DIR/$dest_name"
    if [[ -f "$src" ]]; then
        install -m 0644 "$src" "$dest"
        echo "  ✓ $dest_name (aus branding/audio/$src_name)"
    else
        espeak-ng -v "$lang" -s "$speed" -p "$pitch" -w "$dest" "$text"
        echo "  ⚠ $dest_name (espeak-Fallback — branding/audio/$src_name fehlt)"
    fi
}

install_prompt "Offline mit Wlan verbinden.wav" setup_active.wav    de 145 50 \
    "Hallo. Ich bin im Einrichtungsmodus. Bitte verbinde dich mit dem WLAN Kakabox und öffne den Browser, um dein WLAN auszuwählen."

install_prompt "Wlan verbunden.wav"             wifi_connected.wav  de 145 50 \
    "WLAN ist verbunden. Die Kakabox ist jetzt einsatzbereit."

install_prompt "ready to rumble.wav"            ready_to_rumble.wav en 140 35 \
    "Ready to rumble!"

install_prompt "tschau kakau.wav"               tschau_kakau.wav    de 145 50 \
    "Tschau Kakau!"

install_prompt "A cheerful welcomin.wav"        listening.wav       de 145 50 \
    "Bitte sprich jetzt."

install_prompt "Wie-heißt-das-Zauberwort.wav"   zauberwort.wav      de 145 50 \
    "Wie heißt das Zauberwort?"

install_prompt "Cartoonish successful.wav"      voice_success.wav   de 140 50 \
    "Habe verstanden!"

install_prompt "Cartoonish error.wav"           voice_error.wav     de 140 50 \
    "Habe nichts verstanden."

# "Weiß ich gerade nicht"-Fallback (wenn nichts läuft / TTS scheitert). Die
# eigentliche Titel-Ansage ("Dieses Lied heißt …") spricht die Box per Piper-TTS
# in der gewählten Stimme — kein fester Trägerphrasen-Prompt mehr nötig.
install_prompt "Weiss-ich-gerade-nicht.wav"     voice_no_title.wav  de 140 50 \
    "Das weiß ich gerade nicht."

# Piper-TTS-Stimmmodelle (beide: männlich thorsten + weiblich kerstin). Werden
# NICHT ins Repo committet (~63 MB je) — hier von HuggingFace ziehen, falls noch
# nicht da. Nicht-fatal: fehlt ein Modell, fällt voice/tts.py auf espeak-ng zurück.
mkdir -p "$TTS_DIR"
install -d -o "$RUN_USER" -g "$RUN_GROUP" -m 0775 "$TTS_CACHE_DIR"
for entry in "${TTS_MODELS_TO_FETCH[@]}"; do
    model_name="${entry%% *}"
    sub_path="${entry##* }"
    if [[ -f "$TTS_DIR/$model_name.onnx" ]]; then
        echo "  ✓ Piper-Modell $model_name bereits vorhanden"
        continue
    fi
    url="$PIPER_VOICES_BASE/$sub_path/$model_name.onnx"
    if curl --fail --silent --show-error --location --retry 2 --max-time 120 \
            -o "$TTS_DIR/$model_name.onnx"      "$url" && \
       curl --fail --silent --show-error --location --retry 2 --max-time 30 \
            -o "$TTS_DIR/$model_name.onnx.json" "$url.json"; then
        chmod 0644 "$TTS_DIR/$model_name.onnx"*
        echo "  ✓ Piper-Modell $model_name geladen ($(stat -c%s "$TTS_DIR/$model_name.onnx") Bytes)"
    else
        rm -f "$TTS_DIR/$model_name.onnx"*
        echo "  ⚠ Piper-Modell $model_name fehlgeschlagen — ggf. espeak-Fallback"
    fi
done

# Whisper-ASR-Modell (ggml-tiny) für die Sprachbefehle. NICHT im Repo (~75 MB).
# Nicht-fatal: fehlt es, wirft voice/asr.py VoiceUnavailable und die Box läuft
# ohne ASR weiter (Musik/NFC unberührt).
mkdir -p "$VOICE_DIR"
if [[ -f "$VOICE_DIR/$WHISPER_MODEL" ]]; then
    echo "  ✓ Whisper-Modell $WHISPER_MODEL bereits vorhanden"
elif curl --fail --silent --show-error --location --retry 2 --max-time 180 \
        -o "$VOICE_DIR/$WHISPER_MODEL" "$WHISPER_MODEL_URL"; then
    chmod 0644 "$VOICE_DIR/$WHISPER_MODEL"
    echo "  ✓ Whisper-Modell $WHISPER_MODEL geladen ($(stat -c%s "$VOICE_DIR/$WHISPER_MODEL") Bytes)"
else
    rm -f "$VOICE_DIR/$WHISPER_MODEL"
    echo "  ⚠ Whisper-Modell $WHISPER_MODEL fehlgeschlagen — ASR vorerst inaktiv"
fi

# ──────────────────────────────────────────────────────────────────────────
# 5. Audio-HAT — Device-Tree-Overlay (googlevoicehat-kakabox)
#    Aktiviert die I2S-Soundkarte (MAX98357A) als ALSA-Karte "sndrpigooglevoi",
#    auf die main.py + der comitup-Callback fest verdrahtet sind. Ohne dieses
#    Overlay hat die Box KEINEN Ton. Vorgebautes Binär-Overlay liegt im Repo
#    (kein .dts-Quelltext). Greift erst nach einem Reboot.
# ──────────────────────────────────────────────────────────────────────────
step "5/8 Audio-HAT-Overlay installieren + config.txt patchen"
if [[ -d /boot/firmware/overlays ]]; then
    BOOT_DIR=/boot/firmware
else
    BOOT_DIR=/boot
fi
CONFIG_TXT="$BOOT_DIR/config.txt"
install -m 0755 "$SCRIPT_DIR/googlevoicehat-kakabox.dtbo" \
    "$BOOT_DIR/overlays/googlevoicehat-kakabox.dtbo"
echo "  ✓ Overlay → $BOOT_DIR/overlays/googlevoicehat-kakabox.dtbo"

if grep -q 'kakabox audio-hat' "$CONFIG_TXT" 2>/dev/null; then
    echo "  ✓ config.txt enthält den Kakabox-Audio-Block bereits"
else
    cat >> "$CONFIG_TXT" <<'EOF'

# >>> kakabox audio-hat >>>
# I2S/SPI/I2C aktivieren + googlevoicehat-Overlay für die MAX98357A-Soundkarte.
[all]
dtparam=i2c_arm=on
dtparam=i2s=on
dtparam=spi=on
dtparam=audio=on
dtoverlay=googlevoicehat-kakabox
# <<< kakabox audio-hat <<<
EOF
    echo "  ✓ Kakabox-Audio-Block an $CONFIG_TXT angehängt (Reboot nötig)"
    NEED_REBOOT=1
fi

# ──────────────────────────────────────────────────────────────────────────
# 6. Python-venv + Requirements
#    venv gehört dem Box-User (NICHT root), mit --system-site-packages, damit
#    python3-gpiozero/-lgpio (RP1-GPIO auf Pi 5) sichtbar sind. Voice-Deps sind
#    optional/best-effort — pywhispercpp baut whisper.cpp aus den Sources.
# ──────────────────────────────────────────────────────────────────────────
step "6/8 Python-venv anlegen + Requirements installieren"
VENV="$DEVICE_DIR/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
    sudo -u "$RUN_USER" python3 -m venv --system-site-packages "$VENV"
    echo "  ✓ venv angelegt: $VENV"
else
    echo "  ✓ venv vorhanden: $VENV"
fi
sudo -u "$RUN_USER" "$VENV/bin/pip" install --upgrade pip >/dev/null
# Kern-Requirements (Box läuft nicht ohne) — fatal bei Fehler.
sudo -u "$RUN_USER" "$VENV/bin/pip" install -r "$DEVICE_DIR/requirements.txt"
# Voice-Requirements (ASR/TTS) — best-effort, Box läuft auch ohne.
if sudo -u "$RUN_USER" "$VENV/bin/pip" install -r "$DEVICE_DIR/requirements-voice.txt"; then
    echo "  ✓ Voice-Requirements installiert"
else
    echo "  ⚠ Voice-Requirements fehlgeschlagen — Box läuft, ASR/TTS evtl. inaktiv"
fi

# ──────────────────────────────────────────────────────────────────────────
# 7. systemd-Units
# ──────────────────────────────────────────────────────────────────────────
step "7/8 systemd-Units installieren"
# render: ersetzt __RUN_USER__/__RUN_GROUP__/__DEVICE_DIR__ in der Unit.
render "$SCRIPT_DIR/kakabox.service" "$SYSTEMD_DIR/kakabox.service" 0644

# backend.conf-Drop-in: zeigt die Box auf das Produktiv-Backend. Separat von der
# Unit, damit ein Wechsel (z.B. Staging) ohne Unit-Änderung möglich ist.
mkdir -p "$SYSTEMD_DIR/kakabox.service.d"
cat > "$SYSTEMD_DIR/kakabox.service.d/backend.conf" <<'EOF'
[Service]
Environment=KAKABOX_BACKEND=https://kakaland.de
EOF
echo "  ✓ backend.conf → KAKABOX_BACKEND=https://kakaland.de"

# Alter separater Reset-Watcher wurde durch hold-Logik in main.py + dem
# /usr/local/bin/kakabox-wifi-nuke Helper ersetzt. Vorhandene Installation
# entfernen, falls schon mal ausgerollt.
if [[ -f "$SYSTEMD_DIR/kakabox-reset.service" ]]; then
    systemctl stop kakabox-reset.service 2>/dev/null || true
    systemctl disable kakabox-reset.service 2>/dev/null || true
    rm -f "$SYSTEMD_DIR/kakabox-reset.service"
fi
systemctl daemon-reload

# kakabox.service beim Boot starten — Box muss auch ohne WLAN spielen können
# (lokal gecachte Kakas). Backend-Sync läuft automatisch sobald WLAN da ist
# bzw. beim nächsten Restart.
systemctl enable kakabox.service 2>/dev/null || true

# ──────────────────────────────────────────────────────────────────────────
# 8. Comitup neu starten
# ──────────────────────────────────────────────────────────────────────────
step "8/8 comitup-Service aktivieren (übernimmt das Wifi-Mgmt)"
systemctl enable comitup

if [[ "$NO_COMITUP_RESTART" == "1" ]]; then
    echo "ℹ comitup enabled, aber NICHT live neugestartet (--no-comitup-restart)."
    echo "  Greift beim nächsten Boot — passt zur Erst-Provisionierung mit Reboot."
elif [[ "$ASSUME_YES" == "1" ]]; then
    echo "ℹ comitup wird neugestartet (nicht-interaktiv). Eine offene SSH/VSCode-"
    echo "  Session kann jetzt abreißen — erwartet."
    systemctl restart comitup
else
    echo "ℹ Falls die Box gerade eine SSH/VSCode-Session offen hat, kann sie jetzt"
    echo "  abreißen — das ist erwartet. Kakabox kommt dann via Hotspot oder Heim-WLAN"
    echo "  wieder online."
    read -r -p "  Trotzdem fortfahren? [j/N] " ANS
    if [[ "${ANS,,}" != "j" && "${ANS,,}" != "y" ]]; then
        echo "→ Abgebrochen vor dem comitup-Restart. Files sind installiert,"
        echo "  comitup-Service läuft noch nicht. Manuell: 'sudo systemctl start comitup'"
        exit 0
    fi
    systemctl restart comitup
fi
# kakabox.service wird vom comitup-Callback automatisch hochgefahren,
# sobald comitup CONNECTED meldet — wir starten ihn hier nicht manuell.

if [[ "${NEED_REBOOT:-0}" == "1" ]]; then
    echo
    echo "⚠ Audio-HAT-Overlay wurde neu in config.txt eingetragen → bitte einmal"
    echo "  'sudo reboot'. Ohne Reboot fehlt die ALSA-Karte 'sndrpigooglevoi' und"
    echo "  die Box hat KEINEN Ton."
fi

cat <<'EOF'

✅ Installation abgeschlossen.

  Aktivierung (NUR bei einer NEUEN Box):
   • device/box_identity.json muss existieren mit einer in der kakaland-Webapp
     registrierten serial_number + activation_code. Den api_token holt die Box
     beim ersten /api/box/connect automatisch — nicht von Hand eintragen.
   • Ist noch keine config.json da: 'cp device/config.example.json device/config.json'.

  Bedienung:
   • Falls aktuell kein WLAN-Profil bekannt ist → Box öffnet Hotspot
     "Kakabox-XXXX" (offen). Mit Phone/Laptop verbinden → Captive-Portal
     öffnet sich. WLAN auswählen, Passwort eingeben, fertig.
   • Falls WLAN bekannt → normale Verbindung, kakabox.service wird
     vom Callback automatisch hochgefahren.

  Reset:
   • Roter Knopf 10 Sekunden halten → alle WLAN-Profile gelöscht, Reboot.
     Box kommt im Hotspot-Modus hoch.

  Logs:
   • sudo journalctl -u comitup -f
   • sudo journalctl -u kakabox.service -f
   • sudo tail -f /var/log/kakabox-callback.log

EOF
