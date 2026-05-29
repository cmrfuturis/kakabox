#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────
# Kakabox Bootstrap — provisioniert eine NEUE Box von Null.
#
# Voraussetzung: frisch geflashte Raspberry Pi OS Lite (64-bit) mit Internet
# (WLAN/SSH wurden beim Flashen via custom.toml gesetzt). Auf der neuen Pi als
# Box-User (NICHT root) ausführen — das Skript ruft sudo selbst auf:
#
#   git clone <repo-url> ~/kakabox
#   bash ~/kakabox/device/setup/bootstrap.sh --serial KB-2026-XXXXXX --code <code>
#
# Oder in einem Rutsch (Repo wird selbst geklont):
#   curl -fsSL <raw-url>/bootstrap.sh | bash -s -- \
#       --repo <repo-url> --serial KB-2026-XXXXXX --code <code>
#
# Was es macht:
#   1. git sicherstellen + (falls nötig) Repo klonen
#   2. box_identity.json anlegen (serial + activation_code; api_token holt die
#      Box beim ersten /api/box/connect selbst — NICHT hier eintragen)
#   3. config.json aus config.example.json anlegen (falls noch keine da)
#   4. install-wifi-onboarding.sh nicht-interaktiv ausführen (ohne comitup-
#      Live-Restart — der greift beim folgenden Reboot)
#   5. Reboot (aktiviert das Audio-HAT-Overlay)
#
# Optionen:
#   --repo <url>     Git-URL (nötig, wenn --dir noch kein Checkout ist)
#   --dir <pfad>     Ziel-Checkout (default: $HOME/kakabox)
#   --serial <s>     serial_number aus der kakaland-Webapp   (PFLICHT)
#   --code <c>       activation_code aus der kakaland-Webapp (PFLICHT)
#   --model "<str>"  Modellname für box_identity.json (default: auto-erkannt)
#   --no-reboot      am Ende NICHT neustarten (Overlay greift dann beim nächsten
#                    manuellen Reboot)
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="" SERIAL="" CODE="" DIR="$HOME/kakabox" MODEL="" REBOOT=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)      REPO="$2"; shift 2 ;;
        --dir)       DIR="$2"; shift 2 ;;
        --serial)    SERIAL="$2"; shift 2 ;;
        --code)      CODE="$2"; shift 2 ;;
        --model)     MODEL="$2"; shift 2 ;;
        --no-reboot) REBOOT=0; shift ;;
        *) echo "Unbekannte Option: $1"; exit 1 ;;
    esac
done

if [[ $EUID -eq 0 ]]; then
    echo "Bitte als Box-User ausführen (NICHT root) — das Skript ruft sudo selbst auf."
    exit 1
fi
if [[ -z "$SERIAL" || -z "$CODE" ]]; then
    echo "Fehler: --serial und --code sind Pflicht (Werte aus der kakaland-Webapp)."
    exit 1
fi

echo "==> git sicherstellen"
if ! command -v git >/dev/null; then
    sudo apt-get update
    sudo apt-get install -y git
fi

if [[ ! -d "$DIR/.git" ]]; then
    if [[ -z "$REPO" ]]; then
        echo "Fehler: $DIR ist kein Git-Checkout und --repo wurde nicht angegeben."
        exit 1
    fi
    echo "==> Klone $REPO → $DIR"
    git clone "$REPO" "$DIR"
fi

DEV="$DIR/device"
[[ -d "$DEV" ]] || { echo "Fehler: device/ nicht gefunden unter $DEV"; exit 1; }

# 2. Identität — api_token NICHT eintragen, holt die Box beim connect selbst.
if [[ ! -f "$DEV/box_identity.json" ]]; then
    HW="$(awk '/^Serial/{print $3}' /proc/cpuinfo 2>/dev/null | tail -1)"
    if [[ -z "$MODEL" ]]; then
        MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
        [[ -n "$MODEL" ]] || MODEL="Raspberry Pi 5 Model B"
    fi
    cat > "$DEV/box_identity.json" <<JSON
{
  "serial_number": "$SERIAL",
  "activation_code": "$CODE",
  "hardware_serial": "$HW",
  "model": "$MODEL",
  "registered_at": "pending"
}
JSON
    chmod 600 "$DEV/box_identity.json"
    echo "==> box_identity.json angelegt (serial=$SERIAL, hw=$HW)"
else
    echo "==> box_identity.json existiert bereits — unverändert gelassen"
fi

# 3. Start-Config
if [[ ! -f "$DEV/config.json" ]]; then
    cp "$DEV/config.example.json" "$DEV/config.json"
    chmod 600 "$DEV/config.json"
    echo "==> config.json aus config.example.json angelegt"
else
    echo "==> config.json existiert bereits — unverändert gelassen"
fi

# 4. Provisioning — nicht-interaktiv, ohne comitup-Live-Restart (Reboot folgt).
echo "==> Provisioning läuft (install-wifi-onboarding.sh) — das dauert ein paar Minuten…"
sudo KAKABOX_ASSUME_YES=1 KAKABOX_NO_COMITUP_RESTART=1 \
    bash "$DEV/setup/install-wifi-onboarding.sh"

# 5. Reboot
if [[ "$REBOOT" == "1" ]]; then
    echo "==> Provisioning fertig. Reboot in 5s (aktiviert Audio-HAT + comitup)."
    echo "    Strg-C zum Abbrechen; danach manuell 'sudo reboot'."
    sleep 5
    sudo reboot
else
    echo "==> Fertig. Bitte noch 'sudo reboot' (Audio-HAT-Overlay + comitup aktivieren)."
fi
