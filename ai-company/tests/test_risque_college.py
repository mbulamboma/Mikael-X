# -*- coding: utf-8 -*-
"""Phase 3B — COLLEGE DU RISQUE (agressif / neutre / prudent) + arbitrage du DG.

Points verifies :
  - le PRUDENT parle en dernier et voit les avis des deux autres ;
  - UNANIMITE NEGATIVE : trois refus suppriment le trade sans meme arbitrer ;
  - l'arbitrage ne peut que DURCIR : `reduce` plafonne le risque, jamais l'inverse ;
  - la trace du college finit dans le dossier de decision du trade ;
  - une panne du college n'ouvre rien (pas de risque sans officier de risque) mais ne
    fait pas basculer le pilote — le book reste gere.
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
from desk.desk import TradingDesk
from desk.risque import CollegeRisque


SUMMARY = {"equity": 100_000.0, "objectif_atteint": False, "perte_jour_pct": 0.2,
           "positions_ouvertes": 0, "ouvertures_bloquees": False, "gate_raisons": []}


def _bind(positions=None):
    T.bind_context({"EURUSD": {"symbol": "EURUSD"}, "GBPUSD": {"symbol": "GBPUSD"}}, {},
                   SUMMARY, positions or [], "", "trend_up", {"blackout": {}})
    T.bind_live(None)


def _open(symbol="EURUSD", direction="buy"):
    return {"type": "open", "strategy": "trend_follow", "symbol": symbol,
            "direction": direction, "entry": 1.10, "sl": 1.09, "tp": 1.13,
            "confidence": 0.7, "rationale": "macro + technique"}


def _avis(symbol="EURUSD", direction="buy", avis="approve", risk_pct=None, raison="x"):
    ligne = {"symbol": symbol, "direction": direction, "avis": avis, "raison": raison}
    if risk_pct is not None:
        ligne["risk_pct"] = risk_pct
    return {"avis": [ligne]}


class Capture:
    def __init__(self, by_role=None, down=()):
        self.by_role = by_role or {}
        self.down = set(down)
        self.ordre = []
        self.prompts = {}

    def json(self, agent, system, human):
        if agent.role in self.down:
            raise DeskUnavailable(f"{agent.role} down (test)")
        self.ordre.append(agent.role)
        self.prompts.setdefault(agent.role, []).append(human)
        return self.by_role.get(agent.role, {})


def install(monkeypatch, cap):
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: cap.json(self, s, h))


class _Gerant:
    """Arbitre simule : rend directement des verdicts."""
    def __init__(self, verdicts=None):
        self.vus = None
        self.verdicts = verdicts or {"verdicts": []}

    def arbitrer_risque(self, proposes, avis, summary):
        self.vus = avis
        return self.verdicts


# =========================================================== deroulement du college
def test_le_prudent_parle_en_dernier_et_voit_les_autres(monkeypatch):
    _bind()
    cap = Capture({"agressif": _avis(), "neutre": _avis(), "prudent": _avis()})
    install(monkeypatch, cap)
    CollegeRisque(AgentConfig()).review([_open()], SUMMARY, _Gerant())
    assert cap.ordre == ["agressif", "neutre", "prudent"]
    assert "AVIS DEJA RENDUS" in cap.prompts["prudent"][0]
    assert "AVIS DEJA RENDUS" not in cap.prompts["agressif"][0]


def test_unanimite_negative_supprime_sans_arbitrage(monkeypatch):
    _bind()
    refus = _avis(avis="reject", raison="correlation")
    install(monkeypatch, Capture({"agressif": refus, "neutre": refus, "prudent": refus}))
    gerant = _Gerant()
    assert CollegeRisque(AgentConfig()).review([_open()], SUMMARY, gerant) == []
    assert gerant.vus is None                       # le DG n'a jamais ete derange


def test_desaccord_va_a_l_arbitrage(monkeypatch):
    _bind()
    install(monkeypatch, Capture({"agressif": _avis(),
                                  "neutre": _avis(avis="reduce", risk_pct=0.5),
                                  "prudent": _avis(avis="reject")}))
    gerant = _Gerant({"verdicts": [{"symbol": "EURUSD", "direction": "buy",
                                    "verdict": "reduce", "risk_pct": 0.4,
                                    "reason": "desaccord -> on entre petit"}]})
    retenues = CollegeRisque(AgentConfig()).review([_open()], SUMMARY, gerant)
    assert len(retenues) == 1 and retenues[0]["risk_pct"] == 0.4
    assert set(gerant.vus) == {"agressif", "neutre", "prudent"}


def test_arbitrage_ne_peut_pas_augmenter_le_risque(monkeypatch):
    """Le DG demande 5 % : le plafond du budget (1 %) s'applique quoi qu'il arrive."""
    _bind()
    install(monkeypatch, Capture({"agressif": _avis(), "neutre": _avis(), "prudent": _avis()}))
    gerant = _Gerant({"verdicts": [{"symbol": "EURUSD", "direction": "buy",
                                    "verdict": "reduce", "risk_pct": 5.0}]})
    cfg = AgentConfig()
    retenues = CollegeRisque(cfg).review([_open()], SUMMARY, gerant)
    assert retenues[0]["risk_pct"] == cfg.ftmo.risk_per_trade_pct


def test_arbitrage_peut_refuser(monkeypatch):
    _bind()
    install(monkeypatch, Capture({"agressif": _avis(), "neutre": _avis(), "prudent": _avis()}))
    gerant = _Gerant({"verdicts": [{"symbol": "EURUSD", "direction": "buy",
                                    "verdict": "reject", "reason": "trop tot"}]})
    assert CollegeRisque(AgentConfig()).review([_open()], SUMMARY, gerant) == []


def test_sans_verdict_le_moteur_ftmo_tranche(monkeypatch):
    """Arbitrage vide : on ne supprime pas, on ne plafonne pas — le plancher fait son travail."""
    _bind()
    install(monkeypatch, Capture({"agressif": _avis(), "neutre": _avis(), "prudent": _avis()}))
    retenues = CollegeRisque(AgentConfig()).review([_open()], SUMMARY, _Gerant())
    assert len(retenues) == 1 and "risk_pct" not in retenues[0]
    assert retenues[0]["verdict_risque"]["verdict"] == "sans_avis"


def test_avis_illisible_ne_bloque_pas(monkeypatch):
    _bind()
    install(monkeypatch, Capture({
        "agressif": {"avis": [{"symbol": "EURUSD", "direction": "buy", "avis": "peut-etre"}]},
        "neutre": _avis(), "prudent": _avis()}))
    gerant = _Gerant()
    assert len(CollegeRisque(AgentConfig()).review([_open()], SUMMARY, gerant)) == 1
    assert gerant.vus["agressif"][0]["avis"] == "approve"      # defaut neutre


def test_lecons_par_temperament(monkeypatch):
    _bind()
    cap = Capture({"agressif": _avis(), "neutre": _avis(), "prudent": _avis()})
    install(monkeypatch, cap)
    vus = []
    CollegeRisque(AgentConfig()).review([_open()], SUMMARY, _Gerant(),
                                        lambda r: vus.append(r[0]) or f"lecon {r[0]}")
    assert vus == ["agressif", "neutre", "prudent"]
    assert "lecon prudent" in cap.prompts["prudent"][0]


# =========================================================== integration desk
def _cap_desk(prudent_avis="approve", verdicts=None):
    return Capture({
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"], "posture": "selectif",
                   "verdicts": verdicts if verdicts is not None else []},
        "trader": {"actions": [_open()]},
        "agressif": _avis(), "neutre": _avis(), "prudent": _avis(avis=prudent_avis),
    })


def _cfg_noyau_risque():
    """Analystes et debat coupes : on teste la couche RISQUE seule."""
    cfg = AgentConfig()
    return replace(cfg, desk=replace(cfg.desk, use_analysts=False, use_debate=False))


def test_le_desk_utilise_le_college_et_trace_les_avis(monkeypatch):
    _bind()
    cap = _cap_desk(verdicts=[{"symbol": "EURUSD", "direction": "buy", "verdict": "reduce",
                               "risk_pct": 0.6, "reason": "prudence"}])
    install(monkeypatch, cap)
    actions = TradingDesk(_cfg_noyau_risque()).decide(SUMMARY)
    assert len(actions) == 1 and actions[0]["risk_pct"] == 0.6
    trace = actions[0]["dossier"]["risque"]
    assert trace["verdict"] == "reduce"
    assert trace["college"] == {"agressif": "approve", "neutre": "approve",
                                "prudent": "approve"}
    assert "risk" not in cap.ordre                  # le Risk Manager mono-voix ne tourne plus


def test_bascule_sur_le_risk_manager_mono_voix(monkeypatch):
    _bind()
    cap = Capture({
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"], "posture": "selectif"},
        "trader": {"actions": [_open()]},
        "risk": {"verdicts": [{"symbol": "EURUSD", "direction": "buy", "verdict": "approve"}]},
    })
    install(monkeypatch, cap)
    cfg = replace(_cfg_noyau_risque(),
                  desk=replace(_cfg_noyau_risque().desk, use_risk_debate=False))
    assert len(TradingDesk(cfg).decide(SUMMARY)) == 1
    assert "agressif" not in cap.ordre and "risk" in cap.ordre


def test_college_hs_n_ouvre_rien_mais_ne_degrade_pas(monkeypatch):
    """Pas de prise de risque sans officier de risque — mais le book reste gere."""
    _bind()
    cap = _cap_desk()
    cap.down = {"prudent"}
    install(monkeypatch, cap)
    desk = TradingDesk(_cfg_noyau_risque())
    assert desk.decide(SUMMARY) == []
    assert desk.degraded is False
