#!/usr/bin/env bash
# Superviseur de pidog-energy : relance le service s'il s'arrête.
# Usage : nohup ./energy_loop.sh > ~/pidog-energy.log 2>&1 &
export PATH=$PATH:/usr/sbin:/usr/local/bin
cd "$(dirname "$(readlink -f "$0")")" || exit 1

if [ "$1" = "--boot" ]; then
    echo "[sup] $(date '+%H:%M:%S') démarrage au boot : attente de PipeWire"
    for i in $(seq 1 30); do pactl info >/dev/null 2>&1 && break; sleep 2; done
fi

while true; do
    echo "[sup] $(date '+%Y-%m-%d %H:%M:%S') démarrage du service"
    python3 -u energy.py
    code=$?
    [ $code -eq 0 ] && { echo "[sup] arrêt propre"; break; }
    echo "[sup] $(date '+%H:%M:%S') service mort (code $code) — relance dans 10 s"
    sleep 10
done
