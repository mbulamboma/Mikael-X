"""Isolation des tests : toute persistance va dans une base SQLite TEMPORAIRE.

Importe en tete de chaque module de test (et par conftest.py) AVANT toute creation
de Memory/NewsFeed/Orchestrator, pour ne jamais toucher `agent/state/agent.db`
ni migrer l'etat reel de l'utilisateur.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store import Store, set_default_store

TMP_DB = Path(tempfile.mkdtemp(prefix="agent-tests-")) / "agent.db"
set_default_store(Store(TMP_DB, migrate=False))
