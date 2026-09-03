# Trieur Gmail

Classe automatiquement les threads de ta boîte **dans tes libellés existants**.
Aucun libellé n'est jamais créé : les règles ou le modèle proposent un nom, et si
ce nom ne correspond à rien dans ta taxonomie, le thread est laissé tel quel.

Deux étages de classement :

1. **Règles** (`config/rules.json`) — domaine expéditeur, `List-Id`, mots-clés.
   Gratuit, déterministe, traite le gros du volume.
2. **Claude** — appelé uniquement sur les threads que les règles n'ont pas
   tranchés, par lots, avec la liste de tes libellés en contrainte de sortie.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate       # venv Linux, depuis WSL
pip install -r app/requirements.txt
cp .env.example .env            # puis ajuster
```

Dépose `credentials.json` (identifiants OAuth « Desktop app », Google Cloud
Console) dans `secrets/`, puis autorise l'accès :

```bash
python tools/auth.py
```

> Si l'application OAuth est en mode *Testing*, le refresh token expire tous les
> 7 jours. Passe-la en *In production* pour éviter de recommencer chaque semaine.

## Construire les règles à partir de ton tri existant

```bash
python tools/sync_labels.py --sample 2000
```

Échantillonne les threads **déjà étiquetés** (`has:userlabels`), croise domaine
expéditeur × libellés que tu as posés à la main, et écrit
`config/rules.suggested.json`. Relis-le, puis recopie ce qui te convient dans
`config/rules.json`.

Les libellés listés dans `IGNORED_LABEL_PREFIXES` sont exclus des destinations
proposées, ici comme au classement.

## Utilisation

```bash
# Simulation : rien n'est modifié, un rapport est écrit dans state/reports/
python app/main.py --once

# Vérifier le classement sur un sous-ensemble
python app/main.py --query "from:amazon.fr newer_than:60d" --limit 50

# Appliquer réellement
python app/main.py --once --apply

# Boucle continue (intervalle POLL_INTERVAL)
python app/main.py --apply
```

Le mode simulation est le défaut (`DRY_RUN=true`) et n'avance pas le curseur
d'historique : tu peux le relancer autant de fois que nécessaire.

## Format des règles

```json
{
  "📦 Commandes": {
    "domains":  ["amazon.fr", "cdiscount.com"],
    "keywords": ["commande", "colis", "livraison"],
    "list_ids": ["newsletter.exemple.fr"]
  }
}
```

La clé doit être un libellé **qui existe déjà** dans Gmail — les autres sont
ignorées avec un avertissement au démarrage. Un domaine matche aussi ses
sous-domaines ; les mots-clés sont cherchés sur mot entier, sans accent, dans
sujet + expéditeur + extrait.

## Résorber un arriéré

Quand beaucoup de mails anciens sont sans libellé, les soumettre un par un au LLM
coûte cher et ne sert qu'une fois. Faire cartographier les **domaines** coûte
deux ordres de grandeur de moins, et le résultat devient des règles qui valent
aussi pour l'avenir :

```bash
python tools/suggest_rules_llm.py --sample 3000
```

Le LLM reçoit la liste des domaines non couverts avec leur volume et quelques
sujets, et rend une correspondance domaine → libellé. Il répond « aucun » sur les
domaines hétérogènes (`gmail.com`, un grand groupe qui envoie à la fois des
alertes de sécurité et de la publicité) : ceux-là seront arbitrés thread par
thread, une règle fausse s'appliquant sinon à tout leur volume d'un coup.

Le résultat va dans `config/rules.suggested.json` — **relis-le avant de le
recopier**, une règle erronée sur un gros domaine se propage sur des centaines
de threads. Puis :

```bash
python app/main.py --query "has:nouserlabels in:inbox" --limit 1000 --apply
```

## Archivage

`ARCHIVE=false` par défaut : le trieur pose les libellés et laisse tout dans
l'Inbox, le temps de vérifier que le classement est juste. Passe à `true` pour
qu'il retire `INBOX` et les catégories Gmail des threads étiquetés ;
`KEEP_IN_INBOX` liste les libellés qui restent visibles malgré tout.

## Structure

| Chemin | Rôle |
|---|---|
| `app/main.py` | boucle, orchestration, rapports |
| `app/gmail_client.py` | auth, lecture par lots, écriture des libellés |
| `app/labels.py` | taxonomie existante, résolution de noms |
| `app/classifier.py` | règles puis secours Claude |
| `app/config.py` | configuration (env + `.env`) |
| `tools/auth.py` | autorisation OAuth |
| `tools/sync_labels.py` | déduit des règles du tri existant |
| `tools/prune_labels.py` | supprime des libellés par préfixe (simulation par défaut) |
| `tools/suggest_rules_llm.py` | fait cartographier les domaines de l'arriéré par le LLM |
| `tools/run_daily.sh` | une passe, journalisée et verrouillée, pour le planificateur |
| `tools/install-task.ps1` | installe/retire la tâche quotidienne Windows |

## Tri automatique quotidien

```powershell
.\tools\install-task.ps1 -Apply -At "08:00"   # installer
.\tools\install-task.ps1 -Remove              # désinstaller
Start-ScheduledTask -TaskName 'Trieur Gmail'  # déclencher maintenant
Get-ScheduledTaskInfo -TaskName 'Trieur Gmail'
```

Le Planificateur de tâches Windows appelle `wsl.exe`, qui exécute
`tools/run_daily.sh --apply`. WSL2 s'éteint dès qu'aucun processus n'y tourne :
un `cron` interne raterait ses rendez-vous, alors que le planificateur Windows
réveille WSL à la demande. Si le PC était éteint à l'heure dite, la passe est
rattrapée au démarrage suivant.

Chaque passage journalise dans `state/logs/trieur-AAAA-MM-JJ.log` (30 jours de
rétention). Un verrou `flock` empêche deux passes simultanées. Code de sortie 2 =
token OAuth mort, il faut relancer `python tools/auth.py`.

Sans `--apply` la tâche tourne en simulation : utile pour observer une semaine
avant de laisser le trieur écrire.

## Quota Gmail

Gmail alloue 250 unités de quota par utilisateur et par seconde ; un
`threads.get` en coûte 10, et une requête à l'intérieur d'un batch HTTP compte
comme une requête normale. Un lot de 50 threads consomme donc 500 unités d'un
coup et déclenche des `429 Too many concurrent requests for user`.

Le client cadence ses requêtes sur `QUOTA_UNITS_PER_SECOND` (180 par défaut, soit
~18 threads/seconde) et rejoue les échecs transitoires par vagues, en réduisant
la taille du lot à chaque tentative. Un scan de 2000 threads prend donc environ
deux minutes — c'est normal. Les 404 et 400 ne sont pas rejoués.

## Coût

Les règles ne coûtent rien. Le LLM n'est appelé que sur le reliquat, par lots de
`LLM_BATCH` threads, avec `LLM_MAX_CALLS_PER_RUN` comme garde-fou et le prompt de
taxonomie mis en cache. `LLM_MODEL=claude-haiku-4-5` réduit fortement la facture
si le volume compte davantage que la finesse du classement.
