"""Configuration centralisée, lue depuis l'environnement (+ .env optionnel)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Mini-loader .env : pas de dépendance, ne surcharge jamais l'env réel."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _flag(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# --- Chemins ---
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", ROOT / "secrets"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state" / "gmail_state.json"))
RULES_PATH = Path(os.environ.get("RULES_JSON", ROOT / "config" / "rules.json"))
TAXONOMY_PATH = Path(os.environ.get("TAXONOMY_JSON", ROOT / "config" / "taxonomy.json"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", ROOT / "state" / "reports"))

# --- Gmail ---
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
BASE_QUERY = os.environ.get("GMAIL_BASE_QUERY", "in:anywhere -in:spam -in:trash newer_than:12m")
BATCH_SIZE = _int("BATCH_SIZE", 20)          # threads par requête batch HTTP
# Gmail alloue 250 unités de quota par utilisateur et par seconde ; un
# threads.get en coûte 10. On garde une marge pour ne pas déclencher de 429.
QUOTA_UNITS_PER_SECOND = _int("QUOTA_UNITS_PER_SECOND", 180)
MAX_RETRY_ROUNDS = _int("MAX_RETRY_ROUNDS", 5)
MAX_THREADS_PER_RUN = _int("MAX_THREADS_PER_RUN", 0)  # 0 = pas de limite
POLL_INTERVAL = _int("POLL_INTERVAL", 60)

# --- Comportement ---
# DRY_RUN : on calcule le classement et on écrit un rapport, sans rien modifier.
DRY_RUN = _flag("DRY_RUN", True)
# ARCHIVE : retirer INBOX (et les catégories Gmail) une fois le thread étiqueté.
ARCHIVE = _flag("ARCHIVE", False)
# Libellés qui restent dans l'Inbox même quand ARCHIVE=true.
KEEP_IN_INBOX = [s.strip() for s in os.environ.get("KEEP_IN_INBOX", "").split(",") if s.strip()]
# Libellé posé sur tout ce que le trieur a traité (vide = désactivé).
PROCESSED_LABEL = os.environ.get("PROCESSED_LABEL", "").strip()
# Bac pour les threads qu'aucune règle ni le LLM n'ont su classer (vide = on ne touche pas).
UNSORTED_LABEL = os.environ.get("UNSORTED_LABEL", "").strip()
# Interdit toute création de libellé : on ne travaille que dans la taxonomie existante.
ALLOW_LABEL_CREATION = _flag("ALLOW_LABEL_CREATION", False)
# Libellés exclus des destinations proposées (préfixes, séparés par des virgules).
# Par défaut : les libellés fabriqués par l'ancien script Celery, qui créait un
# libellé par domaine expéditeur. Ils restent dans Gmail, simplement le trieur
# ne les propose plus comme destination.
IGNORED_LABEL_PREFIXES = [
    s.strip() for s in os.environ.get(
        "IGNORED_LABEL_PREFIXES", "📧 Expéditeurs/,🧹 Tri/"
    ).split(",") if s.strip()
]

# --- LLM (secours sur les threads que les règles n'ont pas tranchés) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
USE_LLM = _flag("USE_LLM", True) and bool(ANTHROPIC_API_KEY)
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-opus-5")
LLM_EFFORT = os.environ.get("LLM_EFFORT", "low")  # low | medium | high | xhigh | max
LLM_BATCH = _int("LLM_BATCH", 12)            # threads par appel LLM
LLM_MAX_CALLS_PER_RUN = _int("LLM_MAX_CALLS_PER_RUN", 20)  # garde-fou budget
