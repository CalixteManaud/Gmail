"""Supprime les libelles fabriques par l'ancien script (prefixes ignores).

    python tools/prune_labels.py              # simulation : liste, ne supprime rien
    python tools/prune_labels.py --apply      # supprime, apres confirmation tapee

Supprimer un libelle ne supprime AUCUN message : les mails concernes perdent
seulement cette etiquette et restent dans « Tous les messages ». L'operation
est neanmoins irreversible cote Gmail, d'ou la simulation par defaut et la
sauvegarde ecrite avant toute suppression.
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config  # noqa: E402
import gmail_client as gm  # noqa: E402
from labels import Taxonomy  # noqa: E402

log = logging.getLogger("prune")

UNITS_LABELS_GET = 1
UNITS_LABELS_DELETE = 5


def main() -> None:
    p = argparse.ArgumentParser(
        description="Supprime les libelles correspondant a des prefixes donnes")
    p.add_argument("--apply", action="store_true",
                   help="supprime reellement (sinon simulation)")
    p.add_argument("--prefix", action="append", default=None,
                   help="prefixe a supprimer (repetable ; defaut : IGNORED_LABEL_PREFIXES)")
    p.add_argument("--keep-nonempty", action="store_true",
                   help="epargne les libelles portant au moins un thread")
    p.add_argument("--yes", action="store_true",
                   help="saute la confirmation interactive (usage script)")
    args = p.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    prefixes = args.prefix or config.IGNORED_LABEL_PREFIXES
    if not prefixes:
        raise SystemExit("Aucun prefixe : passe --prefix ou renseigne "
                         "IGNORED_LABEL_PREFIXES dans .env")

    service = gm.open_service()
    taxonomy = Taxonomy.load(service)
    cibles = sorted(n for n in taxonomy.user
                    if any(n.startswith(pref) for pref in prefixes))

    print(f"Prefixes      : {', '.join(prefixes)}")
    print(f"Libelles total: {len(taxonomy.user)}")
    print(f"Cibles        : {len(cibles)}")
    epargnes = sorted(set(taxonomy.user) - set(cibles))
    print(f"Conserves     : {len(epargnes)}")
    for n in epargnes:
        print(f"    garde  {n}")
    if not cibles:
        print("\nRien a supprimer.")
        return

    # Volumes portes par chaque cible, pour la sauvegarde et pour --keep-nonempty.
    print(f"\nLecture des volumes de {len(cibles)} libelles...")
    pacer = gm._Pacer(config.QUOTA_UNITS_PER_SECOND)
    details = []
    for name in cibles:
        pacer.spend(UNITS_LABELS_GET)
        lid = taxonomy.user[name]
        try:
            d = gm._execute(service.users().labels().get(userId="me", id=lid))
        except Exception as e:
            log.warning("%s : volume illisible (%s)", name, e)
            d = {}
        details.append({
            "name": name, "id": lid,
            "threadsTotal": d.get("threadsTotal", 0),
            "messagesTotal": d.get("messagesTotal", 0),
        })

    if args.keep_nonempty:
        gardes = [d for d in details if d["threadsTotal"] > 0]
        details = [d for d in details if d["threadsTotal"] == 0]
        print(f"{len(gardes)} libelles epargnes car non vides (--keep-nonempty)")

    vides = sum(1 for d in details if d["threadsTotal"] == 0)
    portes = sum(d["threadsTotal"] for d in details)
    print(f"\nA supprimer   : {len(details)} libelles "
          f"({vides} vides, {portes} threads perdront cette etiquette)")
    for d in sorted(details, key=lambda d: -d["threadsTotal"])[:10]:
        print(f"    {d['threadsTotal']:5d} threads  {d['name']}")
    if len(details) > 10:
        print(f"    ... et {len(details) - 10} autres")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    backup = config.REPORT_DIR / f"libelles-supprimes-{stamp}.json"

    if not args.apply:
        backup = backup.with_name(f"libelles-a-supprimer-{stamp}.json")
        backup.write_text(json.dumps(details, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"\nSIMULATION : rien n'a ete supprime. Liste : {backup}")
        print("Pour supprimer : python tools/prune_labels.py --apply")
        return

    print("\nAucun message ne sera supprime, seulement les etiquettes.")
    if not args.yes:
        rep = input(f"Taper SUPPRIMER pour effacer {len(details)} libelles : ")
        if rep.strip() != "SUPPRIMER":
            print("Annule.")
            return

    # Sauvegarde AVANT suppression : sans elle, la liste serait perdue.
    backup.write_text(json.dumps(details, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"Sauvegarde : {backup}")

    ok, echecs = 0, []
    for i, d in enumerate(details, 1):
        pacer.spend(UNITS_LABELS_DELETE)
        try:
            gm._execute(service.users().labels().delete(userId="me", id=d["id"]))
            ok += 1
        except Exception as e:
            echecs.append((d["name"], str(e).split("\n", 1)[0][:120]))
        if i % 50 == 0:
            print(f"  {i}/{len(details)}...", flush=True)

    print(f"\nSupprimes : {ok}/{len(details)}")
    for name, err in echecs:
        log.warning("echec %s : %s", name, err)
    print("Pense a retirer ces prefixes de IGNORED_LABEL_PREFIXES dans .env "
          "s'ils n'ont plus lieu d'etre.")


if __name__ == "__main__":
    main()
