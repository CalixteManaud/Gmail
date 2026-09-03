"""Trieur Gmail : un seul processus, sans file d'attente.

Par défaut en simulation (DRY_RUN) : calcule le classement, écrit un rapport,
ne touche à rien. `--apply` applique réellement les libellés.
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import classifier
import config
import gmail_client as gm
from labels import Taxonomy

log = logging.getLogger("main")


def _setup_logging(verbose: bool) -> None:
    # Les libellés contiennent des emoji : la console Windows est en cp1252 par
    # défaut et lèverait UnicodeEncodeError au premier log.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


def load_state() -> dict:
    try:
        return json.loads(config.STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    config.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(config.STATE_PATH)


def collect_thread_ids(service, state: dict, query: Optional[str], limit: int) -> List[str]:
    if query:
        ids = gm.search_thread_ids(service, query, limit)
        log.info("[REQUETE] %d threads pour %r", len(ids), query)
        return ids
    changed = gm.changed_thread_ids(service, state.get("history_id"))
    if changed is not None:
        log.info("[DELTA] %d threads modifies depuis le dernier passage", len(changed))
        ids = sorted(changed)
        return ids[:limit] if limit else ids
    ids = gm.search_thread_ids(service, config.BASE_QUERY, limit)
    log.info("[COMPLET] %d threads (amorcage ou curseur expire)", len(ids))
    return ids


def build_operation(thread: dict, label_name: str, taxonomy: Taxonomy) -> Optional[dict]:
    """Traduit un verdict en modification Gmail, ou None s'il n'y a rien a faire."""
    label_id = taxonomy.resolve(label_name)
    if label_id is None:
        log.warning("thread %s : libelle %r introuvable, ignore", thread["id"], label_name)
        return None

    current = set(thread.get("labels", []))
    add = [] if label_id in current else [label_id]

    if config.PROCESSED_LABEL:
        pid = taxonomy.resolve(config.PROCESSED_LABEL)
        if pid and pid not in current:
            add.append(pid)

    remove = []
    if config.ARCHIVE and label_name not in config.KEEP_IN_INBOX:
        for name in ["INBOX"] + gm.CATEGORIES:
            sid = taxonomy.system_id(name)
            if sid and sid in current:
                remove.append(sid)

    if not add and not remove:
        return None
    return {"thread_id": thread["id"], "add": add, "remove": remove}


def write_report(rows: List[dict], applied: bool) -> Path:
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.REPORT_DIR / f"{'applique' if applied else 'simulation'}-{stamp}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def summarize(rows: List[dict]) -> None:
    by_label: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    for r in rows:
        key = r["label"] or "(non classe)"
        by_label[key] = by_label.get(key, 0) + 1
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    log.info("Origine du classement : %s",
             ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
    for label, n in sorted(by_label.items(), key=lambda kv: -kv[1]):
        log.info("  %4d  %s", n, label)


def run_once(args) -> None:
    service = gm.open_service()
    taxonomy = Taxonomy.load(service)
    log.info("%d libelles utilisateur (%d feuilles)",
             len(taxonomy.user), len(taxonomy.leaves()))

    log.info("%d libelles proposables au classement (les autres sont ignores "
             "via IGNORED_LABEL_PREFIXES)", len(taxonomy.candidates()))

    rules = classifier.RuleSet.load(taxonomy)
    llm = classifier.LLMClassifier(taxonomy) if config.USE_LLM else None
    if config.USE_LLM:
        log.info("Secours LLM actif (%s, effort=%s)", config.LLM_MODEL, config.LLM_EFFORT)
    else:
        log.info("Secours LLM inactif (ANTHROPIC_API_KEY absente ou USE_LLM=false)")

    state = load_state()
    limit = args.limit or config.MAX_THREADS_PER_RUN
    thread_ids = collect_thread_ids(service, state, args.query, limit)
    if not thread_ids:
        log.info("Rien a traiter.")
        return

    threads = [gm.parse_thread(t) for t in gm.fetch_threads(service, thread_ids)]

    verdicts: Dict[str, classifier.Verdict] = {}
    pending = []
    for t in threads:
        label, source = rules.classify(t)
        if label:
            verdicts[t["id"]] = (label, source)
        else:
            pending.append(t)

    n_rules = len(verdicts)
    log.info("Regles : %d classes, %d a arbitrer", n_rules, len(pending))

    if llm and pending:
        for batch in classifier.chunks(pending, config.LLM_BATCH):
            verdicts.update(llm.classify_batch(batch))
        n_llm = len(verdicts) - n_rules
        if llm.disabled_reason:
            log.warning("LLM : arrete apres %d appels (%s) — %d classes, "
                        "%d laisses non classes",
                        llm.calls, llm.disabled_reason, n_llm, len(pending) - n_llm)
        else:
            log.info("LLM : %d appels, %d threads classes", llm.calls, n_llm)

    rows, ops = [], []
    for t in threads:
        label, source = verdicts.get(t["id"], (None, "-"))
        if label is None and config.UNSORTED_LABEL:
            label, source = config.UNSORTED_LABEL, "bac"
        rows.append({
            "thread_id": t["id"],
            "expediteur": t.get("sender"),
            "sujet": t.get("subject"),
            "label": label,
            "source": source,
        })
        if label:
            op = build_operation(t, label, taxonomy)
            if op:
                ops.append(op)

    summarize(rows)
    report = write_report(rows, applied=not config.DRY_RUN)

    if config.DRY_RUN:
        log.info("SIMULATION : %d threads seraient modifies. Rapport : %s", len(ops), report)
        log.info("Pour appliquer : python app/main.py --once --apply")
        return

    done = gm.modify_threads(service, ops)
    log.info("Applique sur %d/%d threads. Rapport : %s", done, len(ops), report)

    new_hist = gm.get_history_id(service)
    if new_hist:
        state["history_id"] = new_hist
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)


def main() -> None:
    p = argparse.ArgumentParser(description="Trieur automatique de boite Gmail")
    p.add_argument("--once", action="store_true", help="un seul passage puis sortie")
    p.add_argument("--apply", action="store_true",
                   help="applique reellement les libelles (sinon simulation)")
    p.add_argument("--query", help="requete Gmail ad hoc (ex: from:amazon.fr newer_than:30d)")
    p.add_argument("--limit", type=int, default=0, help="plafond de threads pour ce passage")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    _setup_logging(args.verbose)
    if args.apply:
        config.DRY_RUN = False

    if args.once or args.query:
        run_once(args)
        return

    log.info("Boucle continue, intervalle %ds (Ctrl-C pour arreter)", config.POLL_INTERVAL)
    while True:
        try:
            run_once(args)
        except gm.AuthExpired as e:
            log.error("%s", e)
            return
        except Exception:
            log.exception("Passage en erreur, nouvelle tentative au prochain cycle")
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except gm.AuthExpired as exc:
        print(f"\nAuthentification : {exc}", file=sys.stderr)
        sys.exit(2)
