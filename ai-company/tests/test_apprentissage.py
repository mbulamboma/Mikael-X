# -*- coding: utf-8 -*-
"""Phase 4 — ATTRIBUTION PAR ROLE + MEMOIRE SITUATIONNELLE.

Points verifies :
  - la signature de situation est deterministe et tolere les donnees manquantes ;
  - la distance rapproche les configurations semblables, pas les symboles au hasard ;
  - le bloc injecte au Trader contient les cas passes ET leur bilan chiffre ;
  - a la cloture, les lecons sont ECRITES PAR ROLE (taguees), plafonnees a 3, et un rol
    inconnu est ignore ;
  - un trade sans dossier (agent solo, trades d'avant la Phase 1C) garde l'ancien
    comportement : une lecon generale, sans attribution.
"""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig
from brain import tools as T
from brain.memory import Memory
from desk import situation as SIT
from desk.base import DeskAgent, DeskUnavailable
from desk.desk import TradingDesk
from store import Store, set_default_store


import pytest
import store as store_mod


@pytest.fixture(autouse=True)
def _store_restaure():
    """Plusieurs tests d'ici remplacent la base par defaut : on la remet en place ensuite,
    pour qu'aucun autre module de tests n'herite d'une base surprise."""
    precedent = store_mod._DEFAULT
    yield
    set_default_store(precedent)


SNAP = {"symbol": "EURUSD", "atr_pct_price": 0.55, "rsi14": 62.0,
        "pos_in_range_pct": 78.0, "ret_20b_pct": 1.4}
SUMMARY = {"equity": 100_000.0, "objectif_atteint": False, "perte_jour_pct": 0.2,
           "positions_ouvertes": 0, "ouvertures_bloquees": False, "gate_raisons": []}


# =========================================================== signature & distance
def test_signature_deterministe():
    now = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)      # session europe
    a = SIT.signature("EURUSD", SNAP, regime="trend_up", direction="buy", now=now)
    b = SIT.signature("EURUSD", SNAP, regime="trend_up", direction="buy", now=now)
    assert a == b
    assert a["session"] == "europe" and a["atr_pct"] == 0.55 and a["rsi"] == 62.0


def test_signature_tolere_un_snapshot_vide():
    s = SIT.signature("EURUSD", {}, regime="", direction="")
    assert s["atr_pct"] is None and s["regime"] is None
    assert SIT.distance(s, s) is not None          # comparables par session/news


def test_distance_rapproche_les_configurations_semblables():
    ref = SIT.signature("EURUSD", SNAP, regime="trend_up", direction="buy")
    proche = SIT.signature("EURUSD", {**SNAP, "rsi14": 64.0}, regime="trend_up",
                           direction="buy")
    loin = SIT.signature("EURUSD", {**SNAP, "rsi14": 22.0, "pos_in_range_pct": 5.0,
                                    "ret_20b_pct": -4.0}, regime="range", direction="sell")
    assert SIT.distance(ref, proche) < SIT.distance(ref, loin)


def test_distance_ignore_les_variables_absentes():
    ref = SIT.signature("EURUSD", SNAP, regime="trend_up")
    partiel = SIT.signature("EURUSD", {"rsi14": 62.0}, regime="trend_up")
    assert SIT.distance(ref, partiel) is not None
    assert SIT.distance(ref, {}) is None            # rien de comparable -> on ne dit rien


def _trade(R, rsi=62.0, regime="trend_up", direction="buy", symbol="EURUSD"):
    return {"symbol": symbol, "strategy": "trend_follow", "R": R,
            "result": "tp" if R > 0 else "sl",
            "dossier": {"situation": SIT.signature(
                symbol, {**SNAP, "rsi14": rsi}, regime=regime, direction=direction)}}


def test_similaires_classe_par_proximite():
    ref = SIT.signature("EURUSD", SNAP, regime="trend_up", direction="buy")
    trades = [_trade(1.0, rsi=25.0, regime="range", direction="sell"),
              _trade(-1.0, rsi=61.0),
              {"symbol": "X", "R": 3.0}]                       # sans signature : ignore
    proches = SIT.similaires(ref, trades, k=5)
    assert len(proches) == 2 and proches[0]["R"] == -1.0


def test_bloc_prompt_donne_le_bilan_des_cas_proches():
    ref = SIT.signature("EURUSD", SNAP, regime="trend_up", direction="buy")
    bloc = SIT.bloc_prompt(ref, [_trade(-1.0), _trade(-1.0), _trade(2.0)], k=5)
    assert "BILAN DE CES 3 CAS" in bloc and "expectancy" in bloc
    assert "1 gagnant" in bloc


def test_bloc_prompt_sans_historique():
    ref = SIT.signature("EURUSD", SNAP, regime="trend_up")
    assert "inedite" in SIT.bloc_prompt(ref, [])


# =========================================================== injection au Trader
class Capture:
    def __init__(self, by_role=None, down=(), texte="lecon generique"):
        self.by_role = by_role or {}
        self.down = set(down)
        self.texte = texte
        self.prompts = {}

    def json(self, agent, system, human):
        if agent.role in self.down:
            raise DeskUnavailable(f"{agent.role} down (test)")
        self.prompts.setdefault(agent.role, []).append(human)
        return self.by_role.get(agent.role, {})

    def txt(self, agent, system, human):
        if agent.role in self.down:
            raise DeskUnavailable("coach down (test)")
        self.prompts.setdefault(agent.role + ":text", []).append(human)
        return self.texte


def install(monkeypatch, cap):
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: cap.json(self, s, h))
    monkeypatch.setattr(DeskAgent, "ask_text", lambda self, s, h: cap.txt(self, s, h))


def _cfg():
    """Noyau + memoire : analystes, debat et college coupes (testes ailleurs)."""
    c = AgentConfig()
    return replace(c, desk=replace(c.desk, use_analysts=False, use_debate=False,
                                   use_risk_debate=False))


def _bind():
    T.bind_context({"EURUSD": SNAP}, {}, SUMMARY, [], "", "trend_up", {"blackout": {}})
    T.bind_live(None)


def test_la_memoire_situationnelle_arrive_au_trader(monkeypatch, tmp_path):
    set_default_store(Store(tmp_path / "m.db", migrate=False))
    mem = Memory()
    for r in (-1.0, -1.0, 2.0):
        mem.log_event("trade_closed", _trade(r))
    _bind()
    cap = Capture({
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"], "posture": "selectif"},
        "trader": {"actions": []},
    })
    install(monkeypatch, cap)
    TradingDesk(_cfg()).decide(SUMMARY)
    prompt = cap.prompts["trader"][0]
    assert "CAS PASSES LES PLUS PROCHES" in prompt and "BILAN DE CES 3 CAS" in prompt


def test_la_signature_est_rangee_dans_le_dossier_du_trade(monkeypatch, tmp_path):
    set_default_store(Store(tmp_path / "m2.db", migrate=False))
    _bind()
    install(monkeypatch, Capture({
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"], "posture": "selectif"},
        "trader": {"actions": [{"type": "open", "strategy": "trend_follow",
                                "symbol": "EURUSD", "direction": "buy", "entry": 1.10,
                                "sl": 1.09, "tp": 1.13, "confidence": 0.7,
                                "rationale": "cassure"}]},
        "risk": {"verdicts": [{"symbol": "EURUSD", "direction": "buy", "verdict": "approve"}]},
    }))
    actions = TradingDesk(_cfg()).decide(SUMMARY)
    s = actions[0]["dossier"]["situation"]
    assert s["rsi"] == 62.0 and s["direction"] == "buy"       # direction reprise du trade


# =========================================================== attribution par role
def _trade_cloture(R=-1.0):
    return {"symbol": "EURUSD", "strategy": "trend_follow", "regime": "trend_up", "R": R,
            "result": "sl" if R < 0 else "tp",
            "dossier": {"mandat": {"posture": "selectif"},
                        "trader": {"strategy": "trend_follow", "confidence": 0.8},
                        "debat": {"verdict": {"direction": "buy", "conviction": 0.7}}}}


def test_lecons_ecrites_par_role(monkeypatch, tmp_path):
    set_default_store(Store(tmp_path / "a.db", migrate=False))
    install(monkeypatch, Capture({"desk": {
        "synthese": "on a paye un debat trop optimiste",
        "lecons": [{"role": "juge", "lecon": "conviction 0.7 sur 2 arguments: exiger 3 faits"},
                   {"role": "trader", "lecon": "stop a 10 pips sous un ATR de 40: elargir"}]}}))
    desk = TradingDesk(_cfg())
    synthese = desk.reflect(_trade_cloture(), "bilan")
    assert synthese == "on a paye un debat trop optimiste"
    lecons = Memory().lessons
    assert {l.tags[0] for l in lecons} == {"juge", "trader"}
    assert all("trend_follow" in l.tags and "trend_up" in l.tags for l in lecons)
    # chaque employe ne relit que les siennes
    assert "exiger 3 faits" in Memory().relevant_lessons_text(roles=["juge"])
    assert "exiger 3 faits" not in Memory().relevant_lessons_text(roles=["trader"])


def test_roles_inconnus_ignores_et_plafond_de_trois(monkeypatch, tmp_path):
    set_default_store(Store(tmp_path / "b.db", migrate=False))
    install(monkeypatch, Capture({"desk": {
        "synthese": "s",
        "lecons": [{"role": "juge", "lecon": "l1"}, {"role": "martien", "lecon": "l2"},
                   {"role": "trader", "lecon": "l3"}, {"role": "bull", "lecon": "l4"},
                   {"role": "bear", "lecon": "l5"}]}}))
    TradingDesk(_cfg()).reflect(_trade_cloture(), "")
    lecons = Memory().lessons
    assert len(lecons) == 2                       # 3 premieres examinees, 'martien' rejete
    assert {l.tags[0] for l in lecons} == {"juge", "trader"}


def test_aucune_lecon_est_une_reponse_valable(monkeypatch, tmp_path):
    """Un stop touche sur un bon processus n'appelle aucun reproche."""
    set_default_store(Store(tmp_path / "c.db", migrate=False))
    install(monkeypatch, Capture({"desk": {"synthese": "these correcte, stop normal",
                                           "lecons": []}}))
    assert TradingDesk(_cfg()).reflect(_trade_cloture(), "") == "these correcte, stop normal"
    assert Memory().lessons == []


def test_trade_sans_dossier_garde_l_ancien_comportement(monkeypatch, tmp_path):
    """Trades d'avant la Phase 1C ou ouverts par l'agent solo : lecon generale, sans blame."""
    set_default_store(Store(tmp_path / "d.db", migrate=False))
    cap = Capture(texte="lecon de coach")
    install(monkeypatch, cap)
    trade = {"symbol": "EURUSD", "strategy": "trend_follow", "R": -1.0}
    assert TradingDesk(_cfg()).reflect(trade, "") == "lecon de coach"
    assert "desk:text" in cap.prompts             # passe par le coach texte, pas l'attribution
    assert Memory().lessons == []


def test_coach_injoignable_donne_une_lecon_de_secours(monkeypatch, tmp_path):
    set_default_store(Store(tmp_path / "e.db", migrate=False))
    install(monkeypatch, Capture(down=("desk",)))
    lecon = TradingDesk(_cfg()).reflect(_trade_cloture(), "")
    assert "trend_follow" in lecon                # repli deterministe, jamais d'exception
