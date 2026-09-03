#!/usr/bin/env bash
# Une passe du trieur, pour le Planificateur de tâches Windows.
#
#   tools/run_daily.sh            # respecte DRY_RUN du .env (simulation)
#   tools/run_daily.sh --apply    # applique réellement
#
# Journalise dans state/logs/, garde 30 jours, et renvoie un code de sortie
# exploitable par le planificateur.
set -uo pipefail

PROJET="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJET/.venv/bin/python"
LOGS="$PROJET/state/logs"
LOG="$LOGS/trieur-$(date +%Y-%m-%d).log"

mkdir -p "$LOGS"

if [[ ! -x "$PYTHON" ]]; then
  echo "$(date -Is) FATAL venv introuvable : $PYTHON" | tee -a "$LOG"
  exit 3
fi

# Une seule passe à la fois : un scan complet peut durer plusieurs minutes et
# deux instances concurrentes doubleraient la consommation de quota Gmail.
exec 9>"$LOGS/.lock"
if ! flock -n 9; then
  echo "$(date -Is) IGNORE : une passe est déjà en cours" >> "$LOG"
  exit 0
fi

cd "$PROJET" || { echo "$(date -Is) FATAL cd $PROJET" >> "$LOG"; exit 3; }

echo "===== $(date -Is) démarrage ($*) =====" >> "$LOG"
"$PYTHON" app/main.py --once "$@" >> "$LOG" 2>&1
code=$?
echo "===== $(date -Is) fin, code $code =====" >> "$LOG"

# Un token mort ne se répare pas tout seul : on le rend visible.
if [[ $code -eq 2 ]]; then
  echo "$(date -Is) ACTION REQUISE : relancer 'python tools/auth.py'" >> "$LOG"
fi

find "$LOGS" -name 'trieur-*.log' -type f -mtime +30 -delete 2>/dev/null

exit $code
