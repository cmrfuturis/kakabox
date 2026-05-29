#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────
# Kakabox WLAN-Onboarding — Installer
#
# WAS DAS SCRIPT MACHT:
#   1. Installiert comitup (aus den Trixie-Repos), espeak-ng und Abhängigkeiten
#   2. Konfiguriert comitup für Kakabox-Branding (eigene SSID, Callback, mDNS)
#   3. Generiert Audio-Ansagen (deutsch, via espeak-ng) für Hotspot/Connected
#   4. Installiert State-Callback-Script und ersetzt comitup-Templates mit
#      Kakabox-Branding
#   5. Installiert systemd-Unit kakabox.service (nicht enabled — startet via Callback)
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

# Pfade — Skript-Verzeichnis robust ermitteln
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVICE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROMPTS_DIR=/usr/share/kakabox/prompts
COMITUP_TEMPLATES_DIR=/usr/share/comitup/web/templates
COMITUP_CONF=/etc/comitup.conf
CALLBACK_PATH=/usr/local/bin/kakabox-comitup-callback
SYSTEMD_DIR=/etc/systemd/system

step() { echo -e "\n\033[1;36m==> $*\033[0m"; }

# ──────────────────────────────────────────────────────────────────────────
# 1. Pakete installieren
# ──────────────────────────────────────────────────────────────────────────
step "1/6 Pakete installieren"
DEBIAN_FRONTEND=noninteractive apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    comitup espeak-ng python3-gpiozero python3-lgpio nftables

# ──────────────────────────────────────────────────────────────────────────
# 2. comitup-Konfiguration
# ──────────────────────────────────────────────────────────────────────────
step "2/6 /etc/comitup.conf installieren"
install -m 0644 "$SCRIPT_DIR/comitup.conf" "$COMITUP_CONF"

# WLAN-Power-Save abschalten — sonst kappt der Pi-Treiber bei mittlerem
# Signal regelmäßig die Verbindung und comitup fällt auf Hotspot zurück.
step "WLAN-Power-Save deaktivieren (/etc/NetworkManager/conf.d/wifi-powersave.conf)"
install -m 0644 "$SCRIPT_DIR/wifi-powersave-off.conf" \
    /etc/NetworkManager/conf.d/wifi-powersave.conf

step "Callback-Script installieren ($CALLBACK_PATH)"
install -m 0755 "$SCRIPT_DIR/kakabox-comitup-callback" "$CALLBACK_PATH"

step "Hardware-Helper installieren (poweroff + wifi-clear + cpu-governor)"
install -m 0755 "$SCRIPT_DIR/kakabox-poweroff"     /usr/local/bin/kakabox-poweroff
install -m 0755 "$SCRIPT_DIR/kakabox-wifi-clear"   /usr/local/bin/kakabox-wifi-clear
install -m 0755 "$SCRIPT_DIR/kakabox-cpu-governor" /usr/local/bin/kakabox-cpu-governor
# Alten wifi-nuke entfernen, falls aus früherer Installation noch da
rm -f /usr/local/bin/kakabox-wifi-nuke
install -m 0440 "$SCRIPT_DIR/sudoers-kakabox" /etc/sudoers.d/kakabox
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
    step "3/6 Captive-Portal-Templates ersetzen"
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
step "4/6 Audio-Ansagen installieren"
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

# ──────────────────────────────────────────────────────────────────────────
# 5. systemd-Units
# ──────────────────────────────────────────────────────────────────────────
step "5/6 systemd-Units installieren"
install -m 0644 "$SCRIPT_DIR/kakabox.service" "$SYSTEMD_DIR/kakabox.service"

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
# 6. Comitup neu starten
# ──────────────────────────────────────────────────────────────────────────
step "6/6 comitup-Service neu starten (übernimmt jetzt das Wifi-Mgmt)"
systemctl enable comitup
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
# kakabox.service wird vom comitup-Callback automatisch hochgefahren,
# sobald comitup CONNECTED meldet — wir starten ihn hier nicht manuell.

cat <<'EOF'

✅ Installation abgeschlossen.

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
