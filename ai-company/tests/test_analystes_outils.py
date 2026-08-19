# -*- coding: utf-8 -*-
"""POINT 2 — analystes tool-capables (opt-in), avec repli integral.

Verifie :
  - quand le drapeau est actif ET le modele capable, l'analyste passe par la boucle d'outils ;
  - les OBSERVATIONS des outils comptent comme preuve (une valeur juste lue n'est pas ecartee
    par le filtre de preuves) ;
  - toute panne d'outils retombe sur le mode JSON pre-charge (l'analyste ne disparait jamais) ;
  - par defaut (drapeau off), on ne touche pas aux outils.
"""
import _isolation  # noqa: F401
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig
from desk.base import DeskAgent, DeskUnavailable
from desk.analysts import AnalysteTechnique


def _cfg(outils=True):
    cfg = AgentConfig()
    return replace(cfg, desk=replace(cfg.desk, analystes_outils=outils, use_debate=False))


def _dossier_minimal(monkeypatch):
    # dossier pre-charge PAUVRE : la seule facon de sourcer le point cle est via l'outil
    monkeypatch.setattr(AnalysteTechnique, "dossier",
                        lambda self, symbol, ctx, live, commun: {"symbole": symbol})


def test_chemin_outils_utilise_les_observations_comme_preuve(monkeypatch):
    _dossier_minimal(monkeypatch)
    monkeypatch.setattr(DeskAgent, "tools_available", lambda self: True)

    # l'outil "revele" un ATR de 0.0040 ; le brief cite 0.0040 -> doit etre garde meme si le
    # dossier pre-charge ne le contenait pas.
    brief_json = ('{"biais":"haussier","confiance":0.7,'
                  '"resume":"cassure","points_cles":["ATR D1 a 0.0040"],'
                  '"invalidation":"sous 1.09"}')

    def fake_run_tools(self, system, human, tools, max_iter=3, return_steps=False):
        obs = ["compute_indicator -> {'atr': 0.0040}"]
        return (brief_json, obs) if return_steps else brief_json

    monkeypatch.setattr(DeskAgent, "run_tools", fake_run_tools)

    b = AnalysteTechnique(_cfg()).brief("EURUSD", {}, {})
    assert b["biais"] == "haussier"
    assert b["points_cles"] == ["ATR D1 a 0.0040"]      # sourcé par l'observation d'outil
    assert "ecartes_sans_preuve" not in b


def test_panne_outils_retombe_sur_json(monkeypatch):
    _dossier_minimal(monkeypatch)
    monkeypatch.setattr(DeskAgent, "tools_available", lambda self: True)

    def run_tools_casse(self, system, human, tools, max_iter=3, return_steps=False):
        raise DeskUnavailable("boucle d'outils en echec (test)")
    monkeypatch.setattr(DeskAgent, "run_tools", run_tools_casse)

    # repli : ask_json est appelé et rend un brief canned
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: {
        "biais": "neutre", "confiance": 0.3, "resume": "repli", "points_cles": []})

    b = AnalysteTechnique(_cfg()).brief("EURUSD", {}, {})
    assert b["biais"] == "neutre"                # le repli a bien produit un brief


def test_drapeau_off_ne_touche_pas_aux_outils(monkeypatch):
    _dossier_minimal(monkeypatch)
    monkeypatch.setattr(DeskAgent, "tools_available", lambda self: True)

    appelé = {"run_tools": False}

    def run_tools_espion(self, *a, **k):
        appelé["run_tools"] = True
        raise AssertionError("run_tools ne doit pas etre appele quand le drapeau est off")
    monkeypatch.setattr(DeskAgent, "run_tools", run_tools_espion)
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: {
        "biais": "haussier", "confiance": 0.5, "resume": "json", "points_cles": []})

    AnalysteTechnique(_cfg(outils=False)).brief("EURUSD", {}, {})
    assert appelé["run_tools"] is False


def test_modele_incapable_ne_tente_pas_les_outils(monkeypatch):
    _dossier_minimal(monkeypatch)
    monkeypatch.setattr(DeskAgent, "tools_available", lambda self: False)   # DeepSeek-like

    def run_tools_espion(self, *a, **k):
        raise AssertionError("run_tools ne doit pas etre appele si le modele est incapable")
    monkeypatch.setattr(DeskAgent, "run_tools", run_tools_espion)
    # le point cite un chiffre PRESENT dans le dossier pre-charge -> reste sourcé
    monkeypatch.setattr(AnalysteTechnique, "dossier",
                        lambda self, symbol, ctx, live, commun: {"symbole": symbol,
                                                                 "atr": 0.0040})
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: {
        "biais": "haussier", "confiance": 0.5, "resume": "json",
        "points_cles": ["ATR 0.0040"]})

    b = AnalysteTechnique(_cfg()).brief("EURUSD", {}, {})
    # pas d'AssertionError = run_tools jamais appelé ; le brief JSON pre-charge a servi
    assert b["biais"] == "haussier"
