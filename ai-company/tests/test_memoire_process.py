"""EMPREINTE MEMOIRE DU PROCESS — ce qui grossissait sans fin et faisait freezer la machine.

Trois fuites, trois bornes :
  1. le Market Watch MT5 : chaque symbole explore par l'agent y restait, et le TERMINAL
     gardait son historique en RAM (le plus gros contributeur, cote terminal64.exe) ;
  2. le cache web : aucune expulsion, chaque page aspiree restait a vie ;
  3. le cache de bougies d'un cycle : un cycle curieux tenait des centaines de DataFrames.
"""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import broker.mt5_broker as B
import data.web as W
from config import WebConfig


# --------------------------------------------------------------- 1. Market Watch MT5
class _FakeMT5:
    """Terminal MT5 minimal : retient qui est visible et refuse de fermer un symbole verrouille."""

    def __init__(self, visibles=(), verrouilles=()):
        self.visibles = set(visibles)
        self.verrouilles = set(verrouilles)      # ex : une position ouverte dessus

    def symbol_select(self, symbol, enable):
        if not enable and symbol in self.verrouilles:
            return False
        self.visibles.add(symbol) if enable else self.visibles.discard(symbol)
        return True


def _broker(monkeypatch, fake):
    """Broker branche sur un faux terminal, restaure apres le test (pas d'etat global qui fuit)."""
    monkeypatch.setattr(B, "mt5", fake, raising=False)
    monkeypatch.setattr(B, "_HAS_MT5", True, raising=False)
    b = B.MT5Broker.__new__(B.MT5Broker)         # pas de connexion reelle
    b.connected = True
    b._selectionnes = set()
    return b


def test_les_symboles_explores_sont_refermes(monkeypatch):
    fake = _FakeMT5()
    b = _broker(monkeypatch, fake)
    b._selectionnes = {"USDJPY", "AUDCAD", "NZDCHF"}
    fake.visibles |= b._selectionnes

    libere = b.release_symbols({"EURUSD"})       # watchlist seule

    assert libere == 3
    assert fake.visibles == set() and b._selectionnes == set()


def test_on_ne_referme_ni_la_watchlist_ni_une_position(monkeypatch):
    fake = _FakeMT5()
    b = _broker(monkeypatch, fake)
    b._selectionnes = {"XAUUSD", "AUDCAD"}
    fake.visibles |= b._selectionnes

    b.release_symbols({"XAUUSD"})                # XAUUSD porte une position

    assert fake.visibles == {"XAUUSD"}
    assert b._selectionnes == {"XAUUSD"}


def test_on_ne_touche_jamais_a_un_symbole_de_l_utilisateur(monkeypatch):
    """Ce que NOUS n'avons pas ouvert ne nous appartient pas : on n'y touche pas."""
    fake = _FakeMT5(visibles={"GBPJPY"})         # affiche par l'utilisateur, hors agent
    b = _broker(monkeypatch, fake)               # _selectionnes vide : rien n'est a nous

    assert b.release_symbols(set()) == 0
    assert fake.visibles == {"GBPJPY"}


def test_un_symbole_verrouille_reste_suivi(monkeypatch):
    """MT5 refuse de fermer un symbole portant un ordre : on ne l'oublie pas, on reessaiera."""
    fake = _FakeMT5(visibles={"EURJPY"}, verrouilles={"EURJPY"})
    b = _broker(monkeypatch, fake)
    b._selectionnes = {"EURJPY"}

    assert b.release_symbols(set()) == 0
    assert b._selectionnes == {"EURJPY"}         # toujours a nous -> retente au prochain cycle


# --------------------------------------------------------------- 2. cache web borne
def test_le_cache_web_est_plafonne():
    w = W.WebResearch(WebConfig())
    for i in range(W._CACHE_MAX + 50):
        w._store(f"search:q{i}", {"resultats": []})
    assert len(w._cache) <= W._CACHE_MAX


def test_le_cache_web_expulse_les_entrees_perimees():
    w = W.WebResearch(WebConfig())
    w._cache["vieux"] = (time.time() - (w.cfg.cache_min * 60 + 10), {"resultats": []})
    w._store("frais", {"resultats": []})
    assert "vieux" not in w._cache and "frais" in w._cache


# --------------------------------------------------------------- 3. cache de bougies borne
def test_le_cache_de_bougies_est_plafonne():
    import run as R
    o = R.Orchestrator.__new__(R.Orchestrator)
    o.cfg = R.CFG
    o._bars_cache = {}
    o.broker = type("B", (), {"candles": lambda self, s, tf, n: [s]})()

    for i in range(R._BARS_CACHE_MAX + 30):
        o._bars(f"SYM{i}", R.CFG.timeframe)

    assert len(o._bars_cache) == R._BARS_CACHE_MAX
