"""Taxonomie Gmail de l'utilisateur : résolution de noms, jamais de création."""
import logging
import re
import unicodedata
from typing import Dict, List, Optional

import config

log = logging.getLogger("labels")

# Libellés système qu'on ne propose jamais comme destination de classement.
SYSTEM_EXCLUDED = {
    "INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT",
    "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


def normalize(text: str) -> str:
    """Minuscule, sans accents, sans emoji ni ponctuation décorative."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = "".join(c if (c.isalnum() or c in " /-_") else " " for c in text)
    return re.sub(r"\s+", " ", text).strip().lower()


class Taxonomy:
    """Index des libellés existants. Aucune méthode ne crée de libellé."""

    def __init__(self, labels: List[dict]):
        self.raw = labels
        self.by_id: Dict[str, dict] = {lb["id"]: lb for lb in labels}
        self.system: Dict[str, str] = {
            lb["name"]: lb["id"] for lb in labels if lb.get("type") == "system"
        }
        self.user: Dict[str, str] = {
            lb["name"]: lb["id"] for lb in labels
            if lb.get("type") == "user" and lb["name"] not in SYSTEM_EXCLUDED
        }
        # Index normalisé du nom complet, puis de la feuille (dernier segment).
        self._norm: Dict[str, str] = {}
        self._leaf: Dict[str, List[str]] = {}
        for name in self.user:
            self._norm.setdefault(normalize(name), name)
            leaf = normalize(name.split("/")[-1])
            self._leaf.setdefault(leaf, []).append(name)

    @classmethod
    def load(cls, service) -> "Taxonomy":
        res = service.users().labels().list(userId="me").execute()
        return cls(res.get("labels", []))

    # --- Consultation ---

    def names(self) -> List[str]:
        return sorted(self.user)

    def leaves(self) -> List[str]:
        """Libellés sans enfant : les seules destinations de classement sensées."""
        allnames = set(self.user)
        return sorted(n for n in allnames if not any(o.startswith(n + "/") for o in allnames))

    def is_ignored(self, name: str) -> bool:
        return any(name.startswith(p) for p in config.IGNORED_LABEL_PREFIXES)

    def candidates(self) -> List[str]:
        """Feuilles proposables au classement, hors préfixes ignorés.

        Sert de liste de choix au LLM : y laisser les centaines de libellés
        générés automatiquement par l'ancien script noierait les vrais.
        """
        return [n for n in self.leaves() if not self.is_ignored(n)]

    def id_of(self, name: str) -> Optional[str]:
        return self.user.get(name)

    def resolve(self, name: str) -> Optional[str]:
        """Nom proposé (règle ou LLM) -> id d'un libellé EXISTANT, sinon None."""
        if not name:
            return None
        name = name.strip().strip("/")
        if name in self.user:
            return self.user[name]
        hit = self._norm.get(normalize(name))
        if hit:
            return self.user[hit]
        candidates = self._leaf.get(normalize(name.split("/")[-1]), [])
        if len(candidates) == 1:  # feuille non ambiguë
            return self.user[candidates[0]]
        if len(candidates) > 1:
            log.debug("libellé '%s' ambigu : %s", name, candidates)
        return None

    def system_id(self, name: str) -> Optional[str]:
        return self.system.get(name)
