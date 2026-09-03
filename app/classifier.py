"""Classement des threads : règles d'abord, Claude en secours.

Les deux étages ne proposent QUE des libellés déjà présents dans la boîte :
les règles sont validées au chargement, le LLM est contraint par un enum.
"""
import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import config
from labels import Taxonomy, normalize

log = logging.getLogger("classify")

Verdict = Tuple[Optional[str], str]  # (nom du libellé | None, origine)


# --- Étage 1 : règles -----------------------------------------------------

@dataclass
class Rule:
    label: str
    domains: List[str]
    keywords: List[str]
    list_ids: List[str]

    def matches(self, thread: dict, text: str) -> bool:
        dom = thread.get("domain", "")
        if any(dom == d or dom.endswith("." + d) for d in self.domains):
            return True
        lid = (thread.get("list_id") or "").lower()
        if lid and any(l in lid for l in self.list_ids):
            return True
        return any(re.search(rf"\b{re.escape(k)}\b", text) for k in self.keywords)


class RuleSet:
    def __init__(self, rules: List[Rule]):
        self.rules = rules

    @classmethod
    def load(cls, taxonomy: Taxonomy) -> "RuleSet":
        """Charge config/rules.json en ignorant les règles hors taxonomie."""
        try:
            raw = json.loads(config.RULES_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.warning("%s absent : aucune règle, tout passe au LLM.", config.RULES_PATH)
            return cls([])
        except json.JSONDecodeError as e:
            raise SystemExit(f"{config.RULES_PATH} illisible : {e}")

        rules, ignored = [], []
        for label, spec in raw.items():
            if taxonomy.resolve(label) is None:
                ignored.append(label)
                continue
            rules.append(Rule(
                label=label,
                domains=[str(d).lower().lstrip("@.") for d in spec.get("domains", [])],
                keywords=[normalize(str(k)) for k in spec.get("keywords", [])],
                list_ids=[str(l).lower() for l in spec.get("list_ids", [])],
            ))
        if ignored:
            log.warning(
                "Règles ignorées (libellé inexistant dans ta boîte) : %s",
                ", ".join(ignored),
            )
        return cls(rules)

    def classify(self, thread: dict) -> Verdict:
        text = normalize(f"{thread.get('subject','')} {thread.get('sender','')} "
                         f"{thread.get('snippet','')}")
        for rule in self.rules:
            if rule.matches(thread, text):
                return rule.label, "règle"
        return None, "-"


# --- Étage 2 : Claude -----------------------------------------------------

SYSTEM_THREADS = """Tu tries la boîte mail d'une personne selon SA propre taxonomie de libellés.

Pour chaque thread, choisis le libellé le plus approprié parmi la liste fournie,
ou la chaîne vide si aucun ne convient raisonnablement. Ne devine pas : préfère
la chaîne vide à un classement approximatif, l'utilisateur relira ces cas.

Tu ne vois que l'expéditeur, le sujet et un extrait — c'est normal, décide avec ça.
Réponds une entrée par thread, en reprenant exactement l'identifiant fourni."""


SYSTEM_DOMAINS = """Tu associes des domaines expéditeurs aux libellés d'une boîte mail.

Pour chaque domaine, on te donne son volume et quelques sujets représentatifs.
Choisis le libellé où TOUS les mails de ce domaine ont leur place, ou la chaîne
vide si le domaine est trop hétérogène pour une règle unique.

La chaîne vide est la bonne réponse quand un domaine mélange des natures
différentes — un fournisseur de messagerie, ou un grand groupe qui envoie à la
fois des alertes de sécurité, des factures et de la publicité. Ces mails-là
seront arbitrés un par un ensuite ; une règle fausse, elle, s'appliquerait à
tout le volume du domaine d'un coup.

Réponds une entrée par domaine, en reprenant exactement le domaine fourni."""


def _enum_labels(labels: Sequence[str]) -> dict:
    return {"type": "string", "enum": list(labels) + [""]}


def _schema_threads(labels: Sequence[str]) -> dict:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "assignments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": _enum_labels(labels),
                        },
                        "required": ["id", "label"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["assignments"],
            "additionalProperties": False,
        },
    }


def _schema_domains(labels: Sequence[str]) -> dict:
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "mappings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "domain": {"type": "string"},
                            "label": _enum_labels(labels),
                        },
                        "required": ["domain", "label"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["mappings"],
            "additionalProperties": False,
        },
    }


class _LLMSession:
    """Client Claude partagé, avec plafond d'appels et coupe-circuit.

    Le plafond compte les appels *émis*, pas les réussis : sinon une erreur
    répétée ne consommerait jamais le budget et tournerait indéfiniment.
    """

    # Une erreur de ce type ne se répare pas en réessayant : clé invalide,
    # crédit épuisé, modèle inaccessible. On coupe l'étage LLM pour le passage.
    PERMANENT = (400, 401, 403, 404)

    def __init__(self, max_calls: Optional[int] = None):
        self.max_calls = config.LLM_MAX_CALLS_PER_RUN if max_calls is None else max_calls
        self.calls = 0
        self.disabled_reason: Optional[str] = None
        self._client = None

    @property
    def available(self) -> bool:
        return self.disabled_reason is None

    def _client_lazy(self):
        if self._client is None:
            import anthropic  # import tardif : inutile si USE_LLM=false
            self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        return self._client

    def request_json(self, system_blocks: List[dict], user_text: str,
                     schema: dict, max_tokens: int = 8000) -> Optional[dict]:
        """Un appel contraint par schéma. None si indisponible ou en échec."""
        if self.disabled_reason:
            return None
        if self.calls >= self.max_calls:
            self.disabled_reason = (f"plafond de {self.max_calls} appels atteint "
                                    f"(LLM_MAX_CALLS_PER_RUN)")
            log.warning("Étage LLM arrêté : %s", self.disabled_reason)
            return None

        self.calls += 1  # compté avant l'envoi : un échec consomme le budget aussi
        try:
            resp = self._client_lazy().messages.create(
                model=config.LLM_MODEL,
                max_tokens=max_tokens,
                system=system_blocks,
                output_config={"effort": config.LLM_EFFORT, "format": schema},
                messages=[{"role": "user", "content": user_text}],
            )
        except Exception as e:
            if getattr(e, "status_code", None) in self.PERMANENT:
                self.disabled_reason = _short_error(e)
                log.error("Étage LLM désactivé : %s", self.disabled_reason)
            else:
                log.error("Appel LLM échoué : %s", _short_error(e))
            return None

        if resp.stop_reason == "refusal":
            log.error("Requête déclinée par le modèle.")
            return None

        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.error("Réponse LLM non parsable.")
            return None


class LLMClassifier(_LLMSession):
    """Classe par lots les threads qu'aucune règle n'a tranchés."""

    def __init__(self, taxonomy: Taxonomy, max_calls: Optional[int] = None):
        super().__init__(max_calls)
        self.taxonomy = taxonomy
        self.labels = taxonomy.candidates()

    def _system_blocks(self) -> List[dict]:
        # Bloc stable et volumineux -> mis en cache, relu à ~0,1x le prix.
        listing = "\n".join(f"- {n}" for n in self.labels)
        return [{
            "type": "text",
            "text": f"{SYSTEM_THREADS}\n\nLibellés disponibles :\n{listing}",
            "cache_control": {"type": "ephemeral"},
        }]

    def classify_batch(self, threads: Sequence[dict]) -> Dict[str, Verdict]:
        out: Dict[str, Verdict] = {}
        if not threads or not self.labels:
            return out

        payload = [
            {
                "id": t["id"],
                "expediteur": t.get("sender", ""),
                "sujet": t.get("subject", ""),
                "extrait": t.get("snippet", ""),
            }
            for t in threads
        ]
        data = self.request_json(
            self._system_blocks(),
            json.dumps(payload, ensure_ascii=False, indent=1),
            _schema_threads(self.labels),
        )
        if data is None:
            return out

        known = {t["id"] for t in threads}
        for item in data.get("assignments", []):
            tid, label = item.get("id"), (item.get("label") or "").strip()
            if tid in known and label:
                out[tid] = (label, "llm")
        return out


class DomainMapper(_LLMSession):
    """Fait correspondre des domaines expéditeurs à des libellés.

    Bien moins cher que de classer thread par thread : une correspondance
    couvre tout le passé ET tout le futur de ce domaine, une fois écrite
    dans les règles.
    """

    def __init__(self, taxonomy: Taxonomy, max_calls: Optional[int] = None):
        super().__init__(max_calls)
        self.taxonomy = taxonomy
        self.labels = taxonomy.candidates()

    def _system_blocks(self) -> List[dict]:
        listing = "\n".join(f"- {n}" for n in self.labels)
        return [{
            "type": "text",
            "text": f"{SYSTEM_DOMAINS}\n\nLibellés disponibles :\n{listing}",
            "cache_control": {"type": "ephemeral"},
        }]

    def map_batch(self, domains: Sequence[dict]) -> Dict[str, str]:
        """domains : [{'domain':..., 'volume':int, 'sujets':[...]}] -> {domaine: libellé}"""
        out: Dict[str, str] = {}
        if not domains or not self.labels:
            return out

        data = self.request_json(
            self._system_blocks(),
            json.dumps(list(domains), ensure_ascii=False, indent=1),
            _schema_domains(self.labels),
        )
        if data is None:
            return out

        known = {d["domain"] for d in domains}
        for item in data.get("mappings", []):
            dom, label = item.get("domain"), (item.get("label") or "").strip()
            if dom in known and label:
                out[dom] = label
        return out


def _short_error(exc: Exception) -> str:
    """Extrait le message utile d'une erreur SDK, sans le JSON complet."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message")
        if msg:
            return msg
    return str(exc).split("\n", 1)[0][:300]


def chunks(seq: Sequence, n: int) -> Iterable[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
