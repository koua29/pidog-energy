#!/usr/bin/env bash
#
# Installe pidog-energy comme service au démarrage (cron @reboot).
#
#   ./install/pi_service.sh            installe et démarre
#   ./install/pi_service.sh --retirer  désinstalle
#
# cron plutôt que systemd : aucun privilège root requis sur le Pi.
set -euo pipefail

RACINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUP="$RACINE/energy_loop.sh"
LOG="$HOME/pidog-energy.log"
MARQUE="# pidog-energy"

crontab_actuelle() { crontab -l 2>/dev/null || true; }

if [ "${1:-}" = "--retirer" ]; then
    crontab_actuelle | { grep -v "$MARQUE" || true; } | crontab -
    pkill -f "[e]nergy.py" 2>/dev/null || true
    pkill -f "[e]nergy_loop" 2>/dev/null || true
    echo "Service retiré."
    exit 0
fi

chmod +x "$SUP"
python3 "$RACINE/energy.py" --verifier

LIGNE="@reboot $SUP --boot >> $LOG 2>&1 $MARQUE"
{ crontab_actuelle | { grep -v "$MARQUE" || true; } ; echo "$LIGNE" ; } | crontab -
echo "== cron installé :"
crontab -l | grep "$MARQUE"

pkill -f "[e]nergy_loop" 2>/dev/null || true
pkill -f "[e]nergy.py" 2>/dev/null || true
sleep 1
setsid nohup "$SUP" >> "$LOG" 2>&1 < /dev/null &
sleep 3
if pgrep -f "[e]nergy.py" >/dev/null; then
    echo "== service démarré — journal : $LOG"
else
    echo "!! le service n'a pas démarré, voir $LOG"; exit 1
fi
