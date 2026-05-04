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

step "Callback-Script installieren ($CALLBACK_PATH)"
install -m 0755 "$SCRIPT_DIR/kakabox-comitup-callback" "$CALLBACK_PATH"

step "WiFi-Nuke-Helper installieren (/usr/local/bin/kakabox-wifi-nuke)"
install -m 0755 "$SCRIPT_DIR/kakabox-wifi-nuke" /usr/local/bin/kakabox-wifi-nuke
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
else
    echo "⚠ Templates-Dir $COMITUP_TEMPLATES_DIR nicht gefunden — Branding übersprungen"
fi

# ──────────────────────────────────────────────────────────────────────────
# 4. Audio-Ansagen via espeak-ng
# ──────────────────────────────────────────────────────────────────────────
step "4/6 Audio-Ansagen generieren"
mkdir -p "$PROMPTS_DIR"

espeak-ng -v de -s 145 -p 50 -w "$PROMPTS_DIR/setup_active.wav" \
    "Hallo. Ich bin im Einrichtungsmodus. Bitte verbinde dich mit dem WLAN Kakabox und öffne den Browser, um dein WLAN auszuwählen."

espeak-ng -v de -s 145 -p 50 -w "$PROMPTS_DIR/wifi_connected.wav" \
    "WLAN ist verbunden. Die Kakabox ist jetzt einsatzbereit."

chmod 644 "$PROMPTS_DIR"/*.wav

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

# kakabox.service NICHT enabled — wird via comitup-Callback gestartet, wenn
# WLAN steht. So gibt's keinen "halben" Start ohne Netzwerk.
# Manueller Test trotzdem mit: sudo systemctl start kakabox.service
systemctl disable kakabox.service 2>/dev/null || true

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
