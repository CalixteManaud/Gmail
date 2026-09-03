"""Cartographie les domaines expediteurs de l'arriere vers tes libelles.

    python tools/suggest_rules_llm.py --sample 3000

Classer un domaine coute une fraction de ce que coute classer chacun de ses
threads, et le resultat est durable : la correspondance devient une regle qui
vaut pour le passe ET pour le futur.

Ecrit config/rules.suggested.json (regles existantes + nouvelles). Rien n'est
applique : relis le fichier, puis recopie vers config/rules.json.
"""
import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import classifier  # noqa: E402
import config  # noqa: E402
import gmail_client as gm  # noqa: E402
from labels import Taxonomy  # noqa: E402

log = logging.getLogger("suggest")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Deduit des regles domaine -> libelle avec l'aide du LLM")
    p.add_argument("--sample", type=int, default=3000,
                   help="threads de l'arriere a analyser")
    p.add_argument("--query", default="has:nouserlabels in:inbox",
                   help="requete Gmail ciblant l'arriere")
    p.add_argument("--min-threads", type=int, default=2,
                   help="volume minimal d'un domaine pour meriter une regle")
    p.add_argument("--batch", type=int, default=30, help="domaines par appel LLM")
    p.add_argument("--max-calls", type=int, default=30, help="plafond d'appels LLM")
    p.add_argument("--subjects", type=int, default=3,
                   help="sujets d'exemple envoyes par domaine")
    args = p.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    if not config.USE_LLM:
        raise SystemExit("USE_LLM est desactive ou ANTHROPIC_API_KEY est absente.")

    service = gm.open_service()
    taxonomy = Taxonomy.load(service)
    rules = classifier.RuleSet.load(taxonomy)
    print(f"{len(taxonomy.candidates())} libelles proposables, "
          f"{len(rules.rules)} regles existantes")

    ids = gm.search_thread_ids(service, args.query, args.sample)
    print(f"Echantillon : {len(ids)} threads pour {args.query!r}\n")

    volumes = Counter()
    sujets = defaultdict(list)
    deja = 0
    vus = 0

    for raw in gm.fetch_threads(service, ids):
        t = gm.parse_thread(raw)
        vus += 1
        if vus % 500 == 0:
            print(f"  {vus}/{len(ids)} threads analyses", flush=True)
        # Un domaine deja couvert par une regle n'a rien a apprendre au LLM.
        if rules.classify(t)[0]:
            deja += 1
            continue
        dom = t["domain"]
        if not dom:
            continue
        volumes[dom] += 1
        if len(sujets[dom]) < args.subjects and t.get("subject"):
            sujets[dom].append(t["subject"][:120])

    print(f"\n{deja}/{vus} threads deja couverts par les regles existantes")
    candidats = [(d, n) for d, n in volumes.most_common() if n >= args.min_threads]
    ecartes = sum(n for d, n in volumes.items() if n < args.min_threads)
    print(f"{len(candidats)} domaines a cartographier "
          f"({sum(n for _, n in candidats)} threads)")
    print(f"{len(volumes) - len(candidats)} domaines ecartes car sous "
          f"--min-threads={args.min_threads} ({ecartes} threads)\n")

    if not candidats:
        print("Rien a proposer.")
        return

    mapper = classifier.DomainMapper(taxonomy, max_calls=args.max_calls)
    payload = [{"domain": d, "volume": n, "sujets": sujets[d]} for d, n in candidats]

    mappings = {}
    for lot in classifier.chunks(payload, args.batch):
        mappings.update(mapper.map_batch(lot))
        print(f"  {mapper.calls} appels, {len(mappings)} domaines associes", flush=True)
        if not mapper.available:
            break

    if mapper.disabled_reason:
        log.warning("Cartographie interrompue : %s", mapper.disabled_reason)

    # Fusion avec les regles existantes, sans ecraser ce qui est deja ecrit.
    try:
        fusion = json.loads(config.RULES_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        fusion = {}
    for spec in fusion.values():
        spec.setdefault("domains", [])
        spec.setdefault("keywords", [])

    ajoutes = 0
    for dom, label in mappings.items():
        spec = fusion.setdefault(label, {"domains": [], "keywords": []})
        if dom not in spec["domains"]:
            spec["domains"].append(dom)
            ajoutes += 1
    for spec in fusion.values():
        spec["domains"] = sorted(spec["domains"])

    out = config.RULES_PATH.with_name("rules.suggested.json")
    out.write_text(json.dumps(fusion, ensure_ascii=False, indent=2), encoding="utf-8")

    # Recapitulatif par libelle, tries par volume : de quoi relire l'essentiel.
    par_label = defaultdict(list)
    for dom, label in mappings.items():
        par_label[label].append((volumes[dom], dom))
    couvert = sum(volumes[d] for d in mappings)
    non_associes = [(n, d) for d, n in candidats if d not in mappings]

    print(f"\n{ajoutes} domaines ajoutes, {couvert}/{vus} threads de l'echantillon "
          f"couverts ({100*couvert/max(vus,1):.0f} %)")
    for label in sorted(par_label, key=lambda k: -sum(n for n, _ in par_label[k])):
        doms = sorted(par_label[label], reverse=True)
        total = sum(n for n, _ in doms)
        print(f"\n  {total:5d} threads -> {label}")
        for n, d in doms[:8]:
            print(f"          {n:5d}  {d}")
        if len(doms) > 8:
            print(f"          ... et {len(doms) - 8} autres domaines")

    if non_associes:
        top = sorted(non_associes, reverse=True)[:10]
        print(f"\n{len(non_associes)} domaines laisses sans regle "
              f"({sum(n for n, _ in non_associes)} threads) — trop heterogenes, "
              "ils seront arbitres thread par thread :")
        for n, d in top:
            print(f"    {n:5d}  {d}")

    print(f"\nEcrit : {out}")
    print(f"Relis-le, puis : cp {out} {config.RULES_PATH}")


if __name__ == "__main__":
    main()
