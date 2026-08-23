#!/usr/bin/env python3
"""
pidog-energy — surveille la batterie du PiDog et le fait aboyer quand elle baisse.

Service de fond, volontairement minimal :
  - lit la tension via l'ADC du robot-hat, SANS instancier Pidog()
    -> aucun conflit GPIO avec pidog-voice, les deux tournent ensemble
  - aboie N fois par le haut-parleur uniquement, SANS bouger les servos
    -> sans danger si le robot est pose sur une table
  - chaque palier n'aboie qu'une fois, jusqu'a rechargement (etat persistant)
  - ne dit rien pendant la charge

Usage :
    python3 energy.py                # service (boucle infinie)
    python3 energy.py --etat         # tension, pourcentage, paliers deja declenches
    python3 energy.py --verifier     # valide config.json et sort
    python3 energy.py --test 30      # joue l'alerte du palier 30 % et sort
    python3 energy.py --oublier      # rearme tous les paliers
"""
import json
import os
import statistics
import sys
import time
from collections import deque

RACINE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("PIDOG_ENERGY_CONFIG", os.path.join(RACINE, "config.json"))
ETAT = os.environ.get(
    "PIDOG_ENERGY_ETAT",
    os.path.expanduser("~/.local/state/pidog-energy/etat.json"))


def journal(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# ----------------------------------------------------------------- config
def charger_config(chemin=CONFIG):
    with open(chemin, encoding="utf-8") as f:
        c = json.load(f)
    b = c["batterie"]
    if b["tension_pleine_v"] <= b["tension_vide_v"]:
        raise ValueError("tension_pleine_v doit etre > tension_vide_v")
    if not c.get("alertes"):
        raise ValueError("aucune alerte definie")
    for a in c["alertes"]:
        if not 0 < a["pourcent"] <= 100:
            raise ValueError(f"palier hors bornes : {a['pourcent']}")
        if a["aboiements"] < 1:
            raise ValueError(f"aboiements doit etre >= 1 (palier {a['pourcent']})")
    paliers = [a["pourcent"] for a in c["alertes"]]
    if len(set(paliers)) != len(paliers):
        raise ValueError(f"paliers en double : {paliers}")
    if c["rearmement_pourcent"] <= max(paliers):
        raise ValueError("rearmement_pourcent doit etre au-dessus du plus haut palier")
    return c


# ------------------------------------------------------------------ etat
def lire_etat():
    try:
        with open(ETAT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"paliers_declenches": []}


def ecrire_etat(etat):
    os.makedirs(os.path.dirname(ETAT), exist_ok=True)
    tmp = ETAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(etat, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ETAT)          # ecriture atomique : pas d'etat corrompu


# ------------------------------------------------------------- batterie
def tension(n=7):
    """Mediane de n lectures : l'ADC bruite de +/-0.08 V."""
    from robot_hat.device import get_battery_voltage
    lues = []
    for _ in range(n):
        try:
            lues.append(get_battery_voltage())
        except Exception as e:
            journal(f"!! lecture ADC en echec : {type(e).__name__}: {e}")
        time.sleep(0.05)
    return statistics.median(lues) if lues else None


def pourcentage(v, cfg):
    b = cfg["batterie"]
    p = (v - b["tension_vide_v"]) / (b["tension_pleine_v"] - b["tension_vide_v"]) * 100
    return max(0, min(100, round(p)))


def en_charge(historique, cfg):
    """Deduit la charge : hausse soutenue, ou tension de fin de charge.

    Le materiel n'expose aucun signal de charge. C'est une INFERENCE : elle peut
    manquer les toutes premieres minutes d'un branchement, et un pack fraichement
    debranche reste un moment au-dessus du seuil de fin de charge.
    """
    c = cfg["charge"]
    if not historique:
        return False
    if historique[-1] >= c["tension_pleine_charge_v"]:
        return True
    if len(historique) < c["fenetre_mesures"]:
        return False
    moitie = len(historique) // 2
    ancien = statistics.median(list(historique)[:moitie])
    recent = statistics.median(list(historique)[moitie:])
    return (recent - ancien) >= c["hausse_v"]


# ------------------------------------------------------------- aboiement
def preparer_audio(cfg):
    """Force le haut-parleur du robot et le volume — PipeWire elit parfois le HDMI."""
    import re
    import subprocess

    def pactl(*a):
        return subprocess.run(["pactl", *a], capture_output=True,
                              text=True, timeout=5).stdout
    try:
        sinks = pactl("list", "sinks", "short")
        hp = next((l.split("\t")[1] for l in sinks.splitlines() if "soc_sound" in l), None)
        if hp and pactl("get-default-sink").strip() != hp:
            pactl("set-default-sink", hp)
            journal(f"sortie audio forcee sur {hp}")
        v = pactl("get-sink-volume", "@DEFAULT_SINK@")
        pct = int(re.search(r"(\d+)%", v).group(1)) if re.search(r"(\d+)%", v) else 0
        if pct < cfg.get("volume_pourcent", 100):
            pactl("set-sink-volume", "@DEFAULT_SINK@", f"{cfg.get('volume_pourcent', 100)}%")
        pactl("set-sink-mute", "@DEFAULT_SINK@", "0")
    except Exception as e:
        journal(f"!! audio non configure : {type(e).__name__}: {e}")


def parler(texte, cfg):
    """Fait parler le robot (pico2wave, fr-FR). Plus clair qu'un decompte d'aboiements :
    l'utilisateur entend le niveau exact au lieu de devoir compter."""
    if not texte:
        return True
    import subprocess
    wav = "/tmp/pidog_energy_tts.wav"
    try:
        subprocess.run(["pico2wave", "-l", cfg.get("voix_langue", "fr-FR"),
                        "-w", wav, texte], check=True, capture_output=True, timeout=15)
        subprocess.run(["aplay", "-q", "-D", "plug:speaker", wav],
                       timeout=30, capture_output=True)
        journal(f"dit : « {texte} »")
        return True
    except Exception as e:
        journal(f"!! synthese vocale en echec : {type(e).__name__}: {e}")
        return False


def aboyer(n, cfg):
    """N aboiements, son SEUL : aucun servo ne bouge (le robot peut etre sur une table)."""
    if n <= 0:
        return True
    os.environ.setdefault("SDL_AUDIODRIVER", "alsa")
    preparer_audio(cfg)
    try:
        from robot_hat import Music
        m = Music()
        fichier = os.path.expanduser(f"~/pidog/sounds/{cfg['son']}.mp3")
        if not os.path.exists(fichier):
            journal(f"!! son introuvable : {fichier}")
            return False
        for i in range(n):
            m.sound_play(fichier, volume=cfg.get("volume_pourcent", 100))
            if i < n - 1:
                time.sleep(cfg.get("intervalle_aboiement_s", 0.6))
        return True
    except Exception as e:
        journal(f"!! aboiement en echec : {type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------- service
def annoncer(palier, cfg):
    """Aboie (pour attirer l'attention) puis annonce le niveau a voix haute."""
    aboyer(palier.get("aboiements", 0), cfg)
    modele = palier.get("annonce", cfg.get("annonce_par_defaut"))
    if modele:
        time.sleep(0.3)
        parler(modele.format(pourcent=palier["pourcent"]), cfg)


def palier_a_declencher(pct, cfg, declenches):
    """Le palier le plus BAS atteint et pas encore annonce (une chute rapide
    de 35% a 8% doit donner l'alerte 10%, pas l'alerte 30%)."""
    candidats = [a for a in cfg["alertes"]
                 if pct <= a["pourcent"] and a["pourcent"] not in declenches]
    return min(candidats, key=lambda a: a["pourcent"]) if candidats else None


def service(cfg):
    etat = lire_etat()
    declenches = set(etat.get("paliers_declenches", []))
    historique = deque(maxlen=cfg["charge"]["fenetre_mesures"])
    resume = ", ".join(f"{a['pourcent']}% -> {a['aboiements']} aboiements"
                       for a in cfg["alertes"])
    journal(f"demarrage — paliers : {resume}")
    if declenches:
        journal(f"deja annonces (etat conserve) : {sorted(declenches)}")

    while True:
        v = tension(cfg["batterie"]["lectures_par_mesure"])
        if v is None:
            journal("!! batterie illisible, nouvelle tentative")
            time.sleep(cfg["batterie"]["intervalle_s"])
            continue
        historique.append(v)
        pct = pourcentage(v, cfg)
        charge = en_charge(historique, cfg)

        if charge:
            if declenches:
                journal(f"{v:.2f} V ({pct}%) — en charge, rearmement des paliers")
                declenches.clear()
                ecrire_etat({"paliers_declenches": []})
            else:
                journal(f"{v:.2f} V ({pct}%) — en charge, rien a faire")
        else:
            if pct >= cfg["rearmement_pourcent"] and declenches:
                journal(f"{v:.2f} V ({pct}%) — au-dessus de "
                        f"{cfg['rearmement_pourcent']}%, rearmement")
                declenches.clear()
                ecrire_etat({"paliers_declenches": []})

            palier = palier_a_declencher(pct, cfg, declenches)
            if palier:
                journal(f"{v:.2f} V ({pct}%) — palier {palier['pourcent']}% atteint")
                annoncer(palier, cfg)
                declenches.add(palier["pourcent"])
                ecrire_etat({"paliers_declenches": sorted(declenches),
                             "dernier_pourcent": pct,
                             "derniere_tension_v": round(v, 2),
                             "horodatage": time.strftime("%Y-%m-%d %H:%M:%S")})
            else:
                journal(f"{v:.2f} V ({pct}%)")

        time.sleep(cfg["batterie"]["intervalle_s"])


def main():
    args = sys.argv[1:]
    try:
        cfg = charger_config()
    except Exception as e:
        journal(f"!! config invalide : {e}")
        return 1

    if "--verifier" in args:
        journal(f"config valide — {len(cfg['alertes'])} paliers, "
                f"rearmement a {cfg['rearmement_pourcent']}%")
        return 0
    if "--test" in args:
        i = args.index("--test")
        p = int(args[i + 1]) if len(args) > i + 1 else cfg["alertes"][0]["pourcent"]
        palier = next((a for a in cfg["alertes"] if a["pourcent"] == p),
                      {"pourcent": p, "aboiements": 3})
        journal(f"test du palier {p}%")
        annoncer(palier, cfg)
        return 0
    if "--oublier" in args:
        ecrire_etat({"paliers_declenches": []})
        journal("paliers rearmes")
        return 0
    if "--etat" in args:
        v = tension(cfg["batterie"]["lectures_par_mesure"])
        e = lire_etat()
        journal(f"{v:.2f} V — {pourcentage(v, cfg)}% — "
                f"paliers annonces : {e.get('paliers_declenches') or 'aucun'}")
        return 0

    try:
        service(cfg)
    except KeyboardInterrupt:
        journal("arret")
    return 0


if __name__ == "__main__":
    sys.exit(main())
