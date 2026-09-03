"""Autorisation OAuth Gmail (a lancer hors conteneur : ouvre un navigateur).

    python tools/auth.py

Ecrit secrets/token.json, que le trieur consomme ensuite en lecture seule.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import config  # noqa: E402
from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

CRED = config.SECRETS_DIR / "credentials.json"
TOK = config.SECRETS_DIR / "token.json"


def main() -> None:
    if not CRED.exists():
        raise SystemExit(
            f"{CRED} absent. Telecharge les identifiants OAuth 'Desktop app' "
            "depuis la Google Cloud Console et depose-les la."
        )

    creds = None
    if TOK.exists():
        creds = Credentials.from_authorized_user_file(str(TOK), config.SCOPES)
        if creds.valid:
            print(f"{TOK} deja valide, rien a faire.")
            return
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"Refresh impossible ({e}) : nouvelle autorisation.")
                creds = None

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(CRED), config.SCOPES)
        creds = flow.run_local_server(port=0)

    config.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    TOK.write_text(creds.to_json(), encoding="utf-8")
    print(f"OK : {TOK} ecrit.")
    print("Si l'application OAuth est en mode 'Testing' dans la Google Cloud "
          "Console, ce token expirera dans 7 jours. Passe-la en 'In production' "
          "pour eviter de refaire l'operation chaque semaine.")


if __name__ == "__main__":
    main()
