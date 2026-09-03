"""Inspecte la boite et propose des regles a partir du classement existant.

    python tools/sync_labels.py --sample 2000

Echantillonne les threads recents, croise domaine expediteur x libelles poses
a la main, et ecrit :
  - config/taxonomy.json  : la liste des libelles, avec leurs volumes
  - config/rules.suggested.json : regles domaine -> libelle deduites

Rien n'est ecrase : compare puis recopie vers config/rules.json toi-meme.
"""
import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config  # noqa: E402
import gmail_client as gm  # noqa: E402
from labels import Taxonomy  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="Deduit des regles de tri des libelles existants")
    p.add_argument("--sample", type=int, default=2000, help="threads a echantillonner")
    p.add_argument("--min-threads", type=int, default=3,
                   help="occurrences minimales d'un domaine pour proposer une regle")
    p.add_argument("--confidence", type=float, default=0.7,
                   help="part minimale du libelle dominant pour un domaine (0-1)")
    p.add_argument("--query", default="has:userlabels -in:chats",
                   help="requete Gmail a echantillonner (defaut : les threads deja etiquetes)")
    args = p.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    service = gm.open_service()
    taxonomy = Taxonomy.load(service)
    id_to_name = {lid: name for name, lid in taxonomy.user.items()}
    retenus = taxonomy.candidates()
    print(f"{len(taxonomy.user)} libelles utilisateur, {len(taxonomy.leaves())} feuilles, "
          f"{len(retenus)} retenus comme destinations")
    if len(retenus) < len(taxonomy.leaves()):
        print(f"  ({len(taxonomy.leaves()) - len(retenus)} ignores via "
              f"IGNORED_LABEL_PREFIXES : {', '.join(config.IGNORED_LABEL_PREFIXES)})")

    # On echantillonne les threads DEJA etiquetes : c'est la qu'est le signal.
    # Prendre les threads les plus recents ramene surtout du promo non classe.
    ids = gm.search_thread_ids(service, args.query, args.sample)
    print(f"Echantillon : {len(ids)} threads pour {args.query!r}")

    per_label = Counter()
    domain_labels = defaultdict(Counter)
    unlabelled = 0

    seen = 0
    for raw in gm.fetch_threads(service, ids):
        seen += 1
        if seen % 250 == 0:
            print(f"  {seen}/{len(ids)} threads analyses", flush=True)
        t = gm.parse_thread(raw)
        names = [id_to_name[lid] for lid in t["labels"]
                 if lid in id_to_name and not taxonomy.is_ignored(id_to_name[lid])]
        if not names:
            unlabelled += 1
            continue
        for name in names:
            per_label[name] += 1
            if t["domain"]:
                domain_labels[t["domain"]][name] += 1

    taxonomy_out = [
        {"label": name, "leaf": name in set(taxonomy.leaves()),
         "threads_dans_echantillon": per_label.get(name, 0)}
        for name in taxonomy.names()
    ]
    config.TAXONOMY_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.TAXONOMY_PATH.write_text(
        json.dumps(taxonomy_out, ensure_ascii=False, indent=2), encoding="utf-8")

    rules = defaultdict(lambda: {"domains": [], "keywords": []})
    for domain, counts in domain_labels.items():
        label, n = counts.most_common(1)[0]
        total = sum(counts.values())
        if n >= args.min_threads and n / total >= args.confidence:
            rules[label]["domains"].append(domain)

    suggested = {label: {"domains": sorted(spec["domains"]), "keywords": []}
                 for label, spec in sorted(rules.items())}
    out = config.RULES_PATH.with_name("rules.suggested.json")
    out.write_text(json.dumps(suggested, ensure_ascii=False, indent=2), encoding="utf-8")

    covered = sum(len(s["domains"]) for s in suggested.values())
    print(f"\n{seen - unlabelled}/{seen} threads porteurs d'un libelle exploitable")
    print(f"{covered} domaines rattaches a {len(suggested)} libelles")
    if not suggested and seen:
        print("Aucune regle deduite : baisse --min-threads/--confidence, "
              "ou elargis --query.")
    for label, spec in sorted(suggested.items(), key=lambda kv: -len(kv[1]["domains"]))[:10]:
        print(f"  {len(spec['domains']):3d} domaines -> {label}")
    print(f"Ecrit : {config.TAXONOMY_PATH}")
    print(f"Ecrit : {out}  (relis-le, puis copie-le vers {config.RULES_PATH})")


if __name__ == "__main__":
    main()
