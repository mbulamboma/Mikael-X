# -*- coding: utf-8 -*-
"""POINT 3 — reflexion hybride (note factuelle bornee + sourcee, retrouvee par proximite).

Verifie :
  - la note deterministe resume les faits sans rien inventer ;
  - une note LLM qui ne cite AUCUN fait est rejetee au profit de la note deterministe ;
  - une note LLM sourcee est gardee ;
  - la panne LLM retombe sur la note deterministe (jamais d'exception) ;
  - la recuperation par situation ne remonte que des reflexions comparables.
"""
import _isolation  # noqa: F401
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig
from desk import reflexion as R
from desk.base import DeskUnavailable


SIT = {"symbol": "EURUSD", "regime": "range", "direction": "buy", "rsi": 71.0,
       "pos_range": 85.0, "atr_pct": 0.42}
TRADE = {"symbol": "EURUSD", "direction": "buy", "strategy": "mean_revert",
         "R": -0.3, "result": "sl", "mfe_R": 1.4, "mae_R": -0.3,
         "derive_entree_pips": 6.0, "duree_h": 30.0, "dossier": {"situation": SIT}}


class _Reflecteur:
    """Double du Reflecteur : rend la note choisie sans toucher a Bedrock."""
    def __init__(self, note):
        self._note = note

    def note(self, faits):
        return self._note


# ------------------------------------------------------------------ note
def test_note_deterministe_resume_les_faits():
    note = R.note_deterministe(R._faits(TRADE))
    assert "EURUSD" in note and "sl" in note and "MFE 1.4R" in note


def test_note_llm_non_sourcee_est_rejetee():
    cfg = AgentConfig()
    rec = R.ecrire(cfg, TRADE, reflecteur=_Reflecteur(
        "le marche va probablement continuer a monter"))     # aucun chiffre du trade
    # tombe sur la note deterministe (qui, elle, cite les faits)
    assert "EURUSD" in rec["note"] and "probablement" not in rec["note"]


def test_note_llm_sourcee_est_gardee():
    cfg = AgentConfig()
    rec = R.ecrire(cfg, TRADE, reflecteur=_Reflecteur(
        "RSI 71 en range, MFE 1.4R rendu jusqu'a -0.3R"))    # cite des faits
    assert rec["note"].startswith("RSI 71")


def test_panne_llm_retombe_sur_deterministe():
    class _Casse:
        def note(self, faits):
            raise DeskUnavailable("bedrock down")
    rec = R.ecrire(AgentConfig(), TRADE, reflecteur=_Casse())
    assert "EURUSD" in rec["note"]               # jamais d'exception, note produite


def test_desactive_ne_produit_aucune_note_llm():
    cfg = AgentConfig()
    cfg = replace(cfg, desk=replace(cfg.desk, reflexion_enabled=False))
    rec = R.ecrire(cfg, TRADE, reflecteur=_Reflecteur("RSI 71 cite"))
    # reflexion desactivee -> on ne consulte pas le LLM, note deterministe seule
    assert rec["note"].startswith("EURUSD") or "sl" in rec["note"]


# ------------------------------------------------------------------ recuperation
def test_bloc_prompt_ne_remonte_que_les_cas_proches():
    proche = {"symbol": "EURUSD", "R": -0.3, "note": "RSI 71 range",
              "dossier": {"situation": SIT}}
    loin = {"symbol": "USDJPY", "R": 1.0, "note": "tendance forte",
            "dossier": {"situation": {"symbol": "USDJPY", "regime": "trend",
                                      "direction": "sell", "rsi": 30.0}}}
    bloc = R.bloc_prompt(SIT, [proche, loin], k=1)
    assert "RSI 71 range" in bloc and "tendance forte" not in bloc


def test_bloc_prompt_vide_si_rien_de_comparable():
    assert R.bloc_prompt(SIT, []) == ""
