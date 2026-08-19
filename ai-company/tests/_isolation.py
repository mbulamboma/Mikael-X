"""Isolation des tests : toute persistance va dans une base SQLite TEMPORAIRE.

Importe en tete de chaque module de test (et par conftest.py) AVANT toute creation
de Memory/NewsFeed/Orchestrator, pour ne jamais toucher `agent/state/agent.db`
ni migrer l'etat reel de l'utilisateur.

Isole aussi la CONFIGURATION : sans ca, `config.py` charge le `.env` REEL de
l'operateur et les tests qui construisent un `FTMOConfig()`/`DeskConfig()` assertent
contre SES reglages. La suite devenait alors verte ou rouge selon la machine, et un
changement de configuration legitime (ex. passage a un seul symbole, 2026-08-19) faisait
echouer un test qui n'avait rien a voir. Ici : `.env` neutralise + variables purgees ->
les tests assertent contre les DEFAUTS DU CODE, partout, toujours.

Isole aussi le RESEAU SMTP : un test qui construit un `Orchestrator` construit un
`Mailer` a partir du VRAI `.env` (serveur, identifiants, destinataire). Le moindre
trade simule declenchait alors un envoi reel depuis le compte de l'utilisateur, dans
un thread daemon dont l'echec passait inapercu. On coupe la socket, pas la logique :
le formatage des messages et le comportement "serveur injoignable" restent testes.
"""
import os
import smtplib
import sys
import tempfile
from pathlib import Path

# --- 1) CONFIGURATION : couper le `.env` reel AVANT le premier import de config -----
import dotenv

dotenv.load_dotenv = lambda *a, **kw: False        # type: ignore[assignment]

#: prefixes pilotant le comportement teste (risque, desk, news, evaluation, modeles).
#: Purges de l'environnement : un test doit voir les defauts du code, pas la machine.
_PREFIXES = ("AGENT_", "FTMO_", "DESK_", "EVAL_", "NEWS_", "BEDROCK_", "SAFE_",
             "WEB_", "MT5_", "SMTP_", "GOOGLE_", "MYFXBOOK_", "FRED_", "AWS_")
for _cle in [k for k in os.environ if k.startswith(_PREFIXES)]:
    del os.environ[_cle]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store import Store, set_default_store

TMP_DB = Path(tempfile.mkdtemp(prefix="agent-tests-")) / "agent.db"
set_default_store(Store(TMP_DB, migrate=False))


class _SMTPCoupe(smtplib.SMTP):
    """Toute tentative de connexion SMTP echoue immediatement pendant les tests."""
    def __init__(self, *a, **kw):
        raise ConnectionRefusedError("SMTP desactive pendant les tests (_isolation)")


smtplib.SMTP = _SMTPCoupe          # type: ignore[misc]
smtplib.SMTP_SSL = _SMTPCoupe      # type: ignore[misc]

# Le reessai d'envoi attend 5 s puis 20 s en production. Ici, tout echoue par
# construction : on garde le NOMBRE de tentatives (la logique reste testee) mais on
# supprime l'attente, sinon la suite durerait des minutes.
import notify                       # noqa: E402  (apres la coupure SMTP, volontairement)
notify.BACKOFF = (0.0, 0.0)
