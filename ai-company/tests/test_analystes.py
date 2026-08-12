# -*- coding: utf-8 -*-
"""Phase 2 — LES 4 ANALYSTES.

Points verifies :
  - chaque analyste recoit un dossier SPECIALISE (le technique ne voit pas les news,
    le fondamental voit la macro, etc.) — c'est le remplacant du tool-calling ;
  - ils sont INDEPENDANTS : aucun ne voit le brief d'un autre ;
  - un brief mal forme devient neutre/0.0 (le bruit ne doit pas passer pour une conviction) ;
  - une source de donnees en panne laisse un trou, jamais une exception ;
  - l'echec d'un analyste retire son seul brief ; le desk continue.
"""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig
from brain import tools as T
from desk.base import DeskAgent, DeskUnavailable
from desk.analysts import (Analystes, AnalysteTechnique, AnalysteFondamental,
                           AnalysteSentiment, AnalysteActualite)
from desk.desk import TradingDesk


SUMMARY = {"equity": 100_000.0, "objectif_atteint": False, "perte_jour_pct": 0.2,
           "positions_ouvertes": 0, "ouvertures_bloquees": False, "gate_raisons": []}


class _Live:
    """Fournisseur d'acces marche simule (ce que branche l'orchestrateur)."""
    def __init__(self, casse=()):
        self.casse = set(casse)
        self.appels = []

    def _check(self, quoi):
        self.appels.append(quoi)
        if quoi in self.casse:
            raise RuntimeError(f"{quoi} indisponible (test)")

    def market(self, symbol, timeframe=None):
        self._check("market")
        return {"symbol": symbol, "close": 1.1000, "atr": 0.0040}

    def chart(self, symbol, timeframes, candles=10):
        self._check("chart")
        return {"D1": ["bougie"]}

    def indicator(self, symbol, timeframe, name, params):
        self._check(f"indicator:{name}")
        return {"name": name, "value": 42.0}

    def costs_view(self, symbol, lot=0.0, direction="buy"):
        self._check("costs")
        return {"spread_pips": 1.0, "swap": -2.0}

    def fred_series(self, series_id, limit=12):
        self._check("fred")
        return {"serie": series_id, "dernier": 4.2}

    def major_events(self, hours=72):
        self._check("events")
        return {"evenements": ["FOMC"]}

    def news_search(self, query, hours=48):
        self._check("news_search")
        return {"resultats": ["titre"]}

    def retail_sentiment(self, symbol=""):
        self._check("retail")
        return {"long_pct": 78.0, "short_pct": 22.0}


def _bind(live=None, news=None):
    T.bind_context({"EURUSD": {"symbol": "EURUSD", "close": 1.1000}}, {"EURUSD": {"D1": []}},
                   SUMMARY, [], "", "Regime dominant: trend_up",
                   news if news is not None else {"per_symbol": {"EURUSD": {"titres": ["CPI"]}},
                                                  "blackout": {}})
    T.bind_live(live)


class Capture:
    """Capture les prompts envoyes a chaque analyste + rend un brief canned."""
    def __init__(self, by_role=None, down=()):
        self.by_role = by_role or {}
        self.down = set(down)
        self.prompts: dict[str, str] = {}

    def json(self, agent, system, human):
        if agent.role in self.down:
            raise DeskUnavailable(f"{agent.role} down (test)")
        self.prompts[agent.role] = human
        return self.by_role.get(agent.role, {"biais": "haussier", "confiance": 0.6,
                                             "resume": f"avis de {agent.role}"})


def install(monkeypatch, capture):
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: capture.json(self, s, h))


# =========================================================== dossiers specialises
def test_dossier_technique_sans_actualite():
    live = _Live()
    _bind(live)
    d = AnalysteTechnique(AgentConfig()).dossier("EURUSD", T.cycle_context(), live, {})
    assert d["scan"] and d["chart"] and d["atr_D1"] and d["rsi_D1"]
    assert "news" not in d and "blackout" not in d       # ce n'est pas son metier


def test_dossier_fondamental_porte_la_macro_pas_le_graphique():
    live = _Live()
    _bind(live)
    commun = {"macro": {"DGS10": {"dernier": 4.2}}, "evenements": {"evenements": ["FOMC"]}}
    d = AnalysteFondamental(AgentConfig()).dossier("EURUSD", T.cycle_context(), live, commun)
    assert d["series_macro"]["DGS10"]["dernier"] == 4.2
    assert d["devises"] == ["EUR", "USD"] or set(d["devises"]) == {"EUR", "USD"}
    assert "chart" not in d


def test_dossier_sentiment_est_contrarien():
    live = _Live()
    _bind(live)
    d = AnalysteSentiment(AgentConfig()).dossier("EURUSD", T.cycle_context(), live, {})
    assert d["sentiment_retail"]["long_pct"] == 78.0


def test_dossier_actualite_porte_les_news():
    live = _Live()
    _bind(live)
    d = AnalysteActualite(AgentConfig()).dossier("EURUSD", T.cycle_context(), live, {})
    assert d["news_du_symbole"] == {"titres": ["CPI"]}
    assert "recherche" not in d                          # deja des news : pas de requete en plus


def test_actualite_cherche_si_aucune_news():
    live = _Live()
    _bind(live, news={"per_symbol": {}, "blackout": {}})
    d = AnalysteActualite(AgentConfig()).dossier("EURUSD", T.cycle_context(), live, {})
    assert d["recherche"] == {"resultats": ["titre"]}


def test_source_en_panne_laisse_un_trou_pas_une_exception():
    live = _Live(casse=("retail",))
    _bind(live)
    d = AnalysteSentiment(AgentConfig()).dossier("EURUSD", T.cycle_context(), live, {})
    assert d["sentiment_retail"] is None


def test_sans_provider_live_le_dossier_reste_exploitable():
    """Rejeu hors-ligne : pas de provider branche, on travaille sur le contexte archive."""
    _bind(None)
    d = AnalysteTechnique(AgentConfig()).dossier("EURUSD", T.cycle_context(), None, {})
    assert d["scan"] == {"symbol": "EURUSD", "close": 1.1000}


# =========================================================== independance & normalisation
def test_les_analystes_ne_se_lisent_pas(monkeypatch):
    """Chacun doit ignorer l'avis des autres : sinon on paie 4 appels pour 1 avis."""
    live = _Live()
    _bind(live)
    cap = Capture()
    install(monkeypatch, cap)
    Analystes(AgentConfig()).briefs(["EURUSD"], {"consignes": "prudence"}, None)
    assert set(cap.prompts) == {"technique", "fondamental", "sentiment", "actualite"}
    for role, prompt in cap.prompts.items():
        for autre in cap.prompts:
            if autre != role:
                assert f"avis de {autre}" not in prompt


def test_brief_illisible_devient_neutre(monkeypatch):
    _bind(_Live())
    cap = Capture({"technique": {"biais": "TRES HAUSSIER", "confiance": "beaucoup",
                                 "points_cles": "pas une liste"}})
    install(monkeypatch, cap)
    b = AnalysteTechnique(AgentConfig()).brief("EURUSD", {}, "", {})
    assert b["biais"] == "neutre" and b["confiance"] == 0.0 and b["points_cles"] == []


def test_confiance_bornee(monkeypatch):
    _bind(_Live())
    install(monkeypatch, Capture({"technique": {"biais": "haussier", "confiance": 5}}))
    assert AnalysteTechnique(AgentConfig()).brief("EURUSD", {}, "", {})["confiance"] == 1.0


def test_un_analyste_hs_ne_retire_que_son_brief(monkeypatch):
    _bind(_Live())
    cap = Capture(down=("sentiment",))
    install(monkeypatch, cap)
    briefs = Analystes(AgentConfig()).briefs(["EURUSD"], {}, None)
    assert set(briefs["EURUSD"]) == {"technique", "fondamental", "actualite"}


def test_lecons_filtrees_par_analyste(monkeypatch):
    """Chaque analyste apprend de SES erreurs : on lui passe ses lecons, pas celles du desk."""
    _bind(_Live())
    cap = Capture()
    install(monkeypatch, cap)
    vus = []
    Analystes(AgentConfig()).briefs(["EURUSD"], {},
                                    lambda roles: vus.append(roles) or f"lecon {roles[0]}")
    assert vus == [["technique"], ["fondamental"], ["sentiment"], ["actualite"]]
    assert "lecon technique" in cap.prompts["technique"]
    assert "lecon technique" not in cap.prompts["sentiment"]


def test_dossier_commun_charge_une_seule_fois(monkeypatch):
    """La toile de fond macro ne depend pas du symbole : un seul chargement par cycle."""
    live = _Live()
    _bind(live)
    install(monkeypatch, Capture())
    Analystes(AgentConfig()).briefs(["EURUSD", "GBPUSD"], {}, None)
    assert live.appels.count("fred") == 5          # SERIES_MACRO, pas 5 x 2 symboles
    assert live.appels.count("events") == 1


# =========================================================== integration desk
def _router_desk(briefs_ok=True):
    canned = {
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"], "posture": "selectif"},
        "trader": {"actions": [{"type": "open", "strategy": "trend_follow", "symbol": "EURUSD",
                                "direction": "buy", "entry": 1.10, "sl": 1.09, "tp": 1.13,
                                "confidence": 0.7, "rationale": "macro + technique"}]},
        "risk": {"verdicts": [{"symbol": "EURUSD", "direction": "buy", "verdict": "approve"}]},
    }
    if briefs_ok:
        for role in ("technique", "fondamental", "sentiment", "actualite"):
            canned[role] = {"biais": "haussier", "confiance": 0.7, "resume": f"vu par {role}"}
    return canned


def _cfg_sans_debat():
    """Phase 2 isolee : le debat (Phase 3) a son propre fichier de tests."""
    cfg = AgentConfig()
    return replace(cfg, desk=replace(cfg.desk, use_debate=False))


def test_les_briefs_arrivent_au_trader_et_dans_le_dossier(monkeypatch):
    _bind(_Live())
    cap = Capture(_router_desk())
    install(monkeypatch, cap)
    actions = TradingDesk(_cfg_sans_debat()).decide(SUMMARY)
    assert len(actions) == 1
    assert "BRIEFS DES ANALYSTES" in cap.prompts["trader"]
    assert "vu par technique" in cap.prompts["trader"]
    briefs = actions[0]["dossier"]["briefs"]
    assert set(briefs) == {"technique", "fondamental", "sentiment", "actualite"}
    assert briefs["technique"]["biais"] == "haussier"


def test_analystes_desactivables(monkeypatch):
    _bind(_Live())
    cap = Capture(_router_desk())
    install(monkeypatch, cap)
    cfg = replace(_cfg_sans_debat(), desk=replace(_cfg_sans_debat().desk,
                                                  use_analysts=False, use_debate=False))
    actions = TradingDesk(cfg).decide(SUMMARY)
    assert len(actions) == 1                       # le desk fonctionne toujours sans eux
    assert "technique" not in cap.prompts
    assert "briefs" not in actions[0]["dossier"]
