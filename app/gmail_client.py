"""Accès Gmail : authentification, lecture par lots, application des libellés."""
import logging
import random
import re
import time
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

log = logging.getLogger("gmail")

# Catégories Gmail retirées en même temps qu'INBOX lors de l'archivage.
CATEGORIES = ["CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS"]

# Champs demandés pour un thread : on ne descend jamais dans le corps du message.
THREAD_FIELDS = "id,messages(id,internalDate,labelIds,payload/headers),snippet"
META_HEADERS = ["From", "To", "Subject", "Date", "List-Id"]

# Coût en unités de quota Gmail (budget : 250 unités/utilisateur/seconde).
UNITS_THREADS_GET = 10
UNITS_THREADS_MODIFY = 10


class AuthExpired(RuntimeError):
    """Le refresh token n'est plus valide : il faut repasser par tools/auth.py."""


def open_service():
    tok = config.SECRETS_DIR / "token.json"
    if not tok.exists():
        raise AuthExpired(
            f"{tok} absent. Lance : python tools/auth.py (ouvre un navigateur)."
        )
    creds = Credentials.from_authorized_user_file(str(tok), config.SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthExpired(
                "Refresh token rejeté par Google (%s). Relance : python tools/auth.py\n"
                "Si l'app OAuth est en mode 'Testing' dans la Google Cloud Console, "
                "le refresh token expire tous les 7 jours : passe-la en 'In production'." % e
            ) from e
        tok.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _execute(request, tries: int = 5):
    """Exécute une requête avec backoff sur les erreurs transitoires."""
    for attempt in range(tries):
        try:
            return request.execute(num_retries=0)
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if status in (403, 429, 500, 502, 503, 504) and attempt < tries - 1:
                delay = (2 ** attempt) + random.random()
                log.warning("HTTP %s, nouvelle tentative dans %.1fs", status, delay)
                time.sleep(delay)
                continue
            raise


# --- Profil / historique -------------------------------------------------

def get_history_id(service) -> Optional[str]:
    p = _execute(service.users().getProfile(userId="me"))
    hid = p.get("historyId")
    return str(hid) if hid else None


def changed_thread_ids(service, start_history_id: Optional[str]) -> Optional[Set[str]]:
    """Threads modifiés depuis le curseur. None si le curseur est inutilisable."""
    if not start_history_id:
        return None
    threads: Set[str] = set()
    token = None
    while True:
        try:
            res = _execute(service.users().history().list(
                userId="me", startHistoryId=start_history_id,
                historyTypes=["messageAdded", "labelAdded"],
                maxResults=500, pageToken=token,
            ))
        except HttpError as e:
            if getattr(e.resp, "status", 0) == 404:
                log.info("Curseur historique expiré, resynchronisation complète.")
                return None
            raise
        for h in res.get("history", []):
            for entry in h.get("messagesAdded", []) + h.get("labelsAdded", []):
                m = entry.get("message", {})
                labels = m.get("labelIds", [])
                if "SPAM" in labels or "TRASH" in labels or "DRAFT" in labels:
                    continue
                if m.get("threadId"):
                    threads.add(m["threadId"])
        token = res.get("nextPageToken")
        if not token:
            return threads


def search_thread_ids(service, query: str, limit: int = 0) -> List[str]:
    out: List[str] = []
    token = None
    while True:
        res = _execute(service.users().threads().list(
            userId="me", q=query, maxResults=500, pageToken=token,
            includeSpamTrash=False, fields="nextPageToken,threads/id",
        ))
        out += [t["id"] for t in res.get("threads", [])]
        token = res.get("nextPageToken")
        if not token or (limit and len(out) >= limit):
            break
    return out[:limit] if limit else out


# --- Lecture par lots ----------------------------------------------------

def _chunks(seq: Sequence, n: int) -> Iterator[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class _Pacer:
    """Étale les requêtes pour rester sous le quota Gmail (unités/seconde).

    Une requête dans un batch HTTP compte comme une requête normale : un lot de
    50 `threads.get` = 500 unités d'un coup, bien au-delà des 250/s autorisées,
    d'où les 429 « Too many concurrent requests for user ».
    """

    def __init__(self, units_per_second: int):
        self.rate = max(1, units_per_second)
        self._next = 0.0

    def spend(self, units: int) -> None:
        now = time.monotonic()
        wait = self._next - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        self._next = max(now, self._next) + units / self.rate


def _is_transient(exception) -> bool:
    status = getattr(getattr(exception, "resp", None), "status", 0)
    return status in (403, 429, 500, 502, 503, 504)


def _run_batch(service, pacer: _Pacer, items: Sequence, add_request, units: int):
    """Exécute un lot et sépare succès, échecs transitoires et abandons.

    add_request(batch, item) enregistre la requête ; la clé de corrélation est
    la valeur renvoyée par cette fonction (request_id).
    """
    results: Dict[str, dict] = {}
    retry: List[str] = []
    dropped: List[str] = []

    def _cb(request_id, response, exception, _r=results, _t=retry, _d=dropped):
        if exception is None:
            _r[request_id] = response
        elif _is_transient(exception):
            _t.append(request_id)              # attendu sous charge : on réessaie
        else:
            _d.append(request_id)
            log.warning("%s abandonné : %s", request_id, exception)

    batch = service.new_batch_http_request(callback=_cb)
    by_id = {}
    for item in items:
        rid = add_request(batch, item)
        by_id[rid] = item

    pacer.spend(units * len(items))
    try:
        batch.execute()
    except HttpError as e:
        if not _is_transient(e):
            raise
        # Le lot entier a été rejeté : tout est à réessayer.
        return {}, list(by_id), []
    return results, retry, dropped


def _drain(service, items: Sequence, add_request, units: int, what: str):
    """Traite tous les items par vagues, en réduisant le lot à chaque échec."""
    pacer = _Pacer(config.QUOTA_UNITS_PER_SECOND)
    by_id = {}
    pending: List[str] = []
    for item in items:
        rid = item if isinstance(item, str) else item["thread_id"]
        by_id[rid] = item
        pending.append(rid)

    total, seen, dropped_total = len(pending), 0, 0
    for attempt in range(config.MAX_RETRY_ROUNDS + 1):
        if not pending:
            break
        size = max(5, config.BATCH_SIZE >> attempt)   # 20, 10, 5, 5...
        retry_next: List[str] = []
        for chunk in _chunks(pending, size):
            results, retry, dropped = _run_batch(
                service, pacer, [by_id[r] for r in chunk], add_request, units)
            retry_next += retry
            dropped_total += len(dropped)
            seen += len(results)
            for rid in chunk:
                if rid in results:
                    yield results[rid]
            if total > size:
                log.debug("%s : %d/%d", what, seen, total)

        pending = retry_next
        if pending and attempt < config.MAX_RETRY_ROUNDS:
            delay = min(2 ** attempt, 30) + random.random()
            log.info("%s : %d en attente (quota Gmail), reprise dans %.0fs",
                     what, len(pending), delay)
            time.sleep(delay)

    if pending:
        log.warning("%s : %d abandonnés après %d tentatives",
                    what, len(pending), config.MAX_RETRY_ROUNDS + 1)
    if dropped_total:
        log.warning("%s : %d en erreur définitive", what, dropped_total)


def fetch_threads(service, thread_ids: Sequence[str]) -> Iterator[dict]:
    """Récupère les métadonnées des threads, par lots cadencés sur le quota."""
    ids = list(thread_ids)

    def _add(batch, tid):
        batch.add(service.users().threads().get(
            userId="me", id=tid, format="metadata",
            metadataHeaders=META_HEADERS, fields=THREAD_FIELDS,
        ), request_id=tid)
        return tid

    log.info("Lecture de %d threads (~%.0fs au quota courant)",
             len(ids), len(ids) * UNITS_THREADS_GET / config.QUOTA_UNITS_PER_SECOND)
    yield from _drain(service, ids, _add, UNITS_THREADS_GET, "lecture")


# --- Écriture ------------------------------------------------------------

def modify_threads(service, ops: List[dict]) -> int:
    """Applique des modifications de libellés par lots. Retourne le nombre d'OK."""

    def _add(batch, op):
        batch.add(service.users().threads().modify(
            userId="me", id=op["thread_id"],
            body={"addLabelIds": op.get("add", []), "removeLabelIds": op.get("remove", [])},
        ), request_id=op["thread_id"])
        return op["thread_id"]

    return sum(1 for _ in _drain(service, ops, _add, UNITS_THREADS_MODIFY, "écriture"))


# --- Extraction des métadonnées utiles ---

def _header(headers: List[dict], name: str) -> str:
    lname = name.lower()
    for h in headers:
        if h.get("name", "").lower() == lname:
            return h.get("value", "")
    return ""


def sender_domain(sender: str) -> str:
    from email.utils import parseaddr
    addr = parseaddr(sender)[1] or sender
    m = re.search(r"@([A-Za-z0-9.\-]+)", addr)
    return (m.group(1) if m else addr).strip("._-").lower()


def parse_thread(thread: dict) -> dict:
    """Réduit un thread Gmail au strict nécessaire pour le classement."""
    msgs = thread.get("messages") or []
    last = msgs[-1] if msgs else {}
    headers = last.get("payload", {}).get("headers", [])
    sender = _header(headers, "From")
    return {
        "id": thread.get("id"),
        "sender": sender,
        "domain": sender_domain(sender),
        "subject": _header(headers, "Subject"),
        "list_id": _header(headers, "List-Id"),
        "snippet": (thread.get("snippet") or "")[:400],
        "labels": sorted({lb for m in msgs for lb in m.get("labelIds", [])}),
        "n_messages": len(msgs),
    }
