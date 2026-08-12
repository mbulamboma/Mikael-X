"""Isolation des tests : toute persistance va dans une base SQLite TEMPORAIRE.

Importe en tete de chaque module de test (et par conftest.py) AVANT toute creation
de Memory/NewsFeed/Orchestrator, pour ne jamais toucher `agent/state/agent.db`
ni migrer l'etat reel de l'utilisateur.

Isole aussi le RESEAU SMTP : un test qui construit un `Orchestrator` construit un
`Mailer` a partir du VRAI `.env` (serveur, identifiants, destinataire). Le moindre
trade simule declenchait alors un envoi reel depuis le compte de l'utilisateur, dans
un thread daemon dont l'echec passait inapercu. On coupe la socket, pas la logique :
le formatage des messages et le comportement "serveur injoignable" restent testes.
"""
import smtplib
import sys
import tempfile
from pathlib import Path

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
