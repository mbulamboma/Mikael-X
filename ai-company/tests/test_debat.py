# -*- coding: utf-8 -*-
"""Phase 3 — DEBAT BULL/BEAR + JUGE.

Points verifies :
  - le Bear parle en SECOND et voit la these du Bull (sinon ce n'est pas une refutation) ;
  - le debat est ADAPTATIF : second tour seulement quand les convictions sont proches ;
  - le verdict du juge S'IMPOSE au Trader — deterministiquement, pas par la priere :
    abstention ou direction opposee = ouverture supprimee ;
  - un verdict illisible vaut ABSTENTION (le seul defaut qui ne peut pas perdre d'argent) ;
  - une panne pendant le debat retire le debat de CE symbole (jamais de plaidoirie sans
    contradicteur, ni de debat sans juge) et laisse le Trader decider seul ;
  - ANTI-SPECULATION : un argument qui ne cite aucune donnee du dossier est ecarte, un camp
    sans preuve perd sa conviction, et un verdict non fonde devient une abstention.
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
from desk.debat import Debat
from desk.desk import TradingDesk


SUMMARY = {"equity": 100_000.0, "objectif_atteint": False, "perte_jour_pct": 0.2,
           "positions_ouvertes": 0, "ouvertures_bloquees": False, "gate_raisons": []}
BRIEFS = {"EURUSD": {"technique": {"biais": "haussier", "confiance": 0.7,
                                   "resume": "cassure du plus haut 1.1120",
                                   "points_cles": ["plus haut 20j a 1.1120"]}}}
#: le scan porte de VRAIS chiffres : sans dossier chiffre, aucune plaidoirie ne serait
#: recevable (c'est precisement ce que le filtre de preuves garantit).
SNAP = {"symbol": "EURUSD", "close": 1.1000, "atr": 0.0040,
        "haut_20": 1.1120, "bas_20": 1.0900}


def _bind():
    T.bind_context({"EURUSD": SNAP}, {}, SUMMARY, [], "trend_up", {})
    T.bind_live(None)


class Capture:
    """Rend des reponses par role et garde la trace des prompts + de l'ordre des appels."""
    def __init__(self, by_role=None, down=(), suites=None):
        self.by_role = by_role or {}
        self.suites = suites or {}          # role -> liste de reponses successives
        self.down = set(down)
        self.ordre = []
        self.prompts = {}

    def json(self, agent, system, human):
        if agent.role in self.down:
            raise DeskUnavailable(f"{agent.role} down (test)")
        self.ordre.append(agent.role)
        self.prompts.setdefault(agent.role, []).append(human)
        suite = self.suites.get(agent.role)
        if suite:
            return suite.pop(0) if len(suite) > 1 else suite[0]
        return self.by_role.get(agent.role, {})


def install(monkeypatch, cap):
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: cap.json(self, s, h))


def _these(conviction, camp="bull", arguments=None):
    return {"conviction": conviction, "these": f"these du {camp}",
            "arguments": arguments if arguments is not None
            else ["cassure du plus haut 1.1120", "ATR D1 0.0040"],
            "risque_principal": "x", "refutation": "y"}


def _verdict(direction="buy", conviction=0.7, invalidation="cloture sous 1.0900"):
    return {"direction": direction, "conviction": conviction, "gagnant": "bull",
            "plan": "acheter le repli vers 1.1000", "invalidation": invalidation,
            "risques_non_resolus": ["FOMC"]}


# =========================================================== deroulement du debat
def test_le_bear_repond_au_bull(monkeypatch):
    _bind()
    cap = Capture({"bull": _these(0.8), "bear": _these(0.3, "bear"), "juge": _verdict()})
    install(monkeypatch, cap)
    Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})
    assert cap.ordre == ["bull", "bear", "juge"]          # le juge parle en dernier
    assert "THESE ADVERSE" in cap.prompts["bear"][0]
    assert "these du bull" in cap.prompts["bear"][0]
    assert "THESE ADVERSE" not in cap.prompts["bull"][0]  # le Bull ouvre a l'aveugle


def test_debat_tranche_ne_paie_pas_de_second_tour(monkeypatch):
    """0.9 contre 0.2 : un camp ecrase l'autre, relancer n'apprendrait rien."""
    _bind()
    cap = Capture({"bull": _these(0.9), "bear": _these(0.2, "bear"), "juge": _verdict()})
    install(monkeypatch, cap)
    res = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})
    assert res["EURUSD"]["tours"] == 1
    assert cap.ordre.count("bull") == 1 and cap.ordre.count("bear") == 1


def test_debat_serre_declenche_un_second_tour(monkeypatch):
    _bind()
    cap = Capture({"bull": _these(0.6), "bear": _these(0.55, "bear"), "juge": _verdict()})
    install(monkeypatch, cap)
    res = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})
    assert res["EURUSD"]["tours"] == 2
    assert cap.ordre == ["bull", "bear", "bull", "bear", "juge"]
    assert "THESE ADVERSE" in cap.prompts["bull"][1]      # le Bull refute a son tour


def test_plafond_de_tours_respecte(monkeypatch):
    """Debat serre mais DESK_DEBATE_ROUNDS=1 : aucune relance."""
    _bind()
    cap = Capture({"bull": _these(0.6), "bear": _these(0.6, "bear"), "juge": _verdict()})
    install(monkeypatch, cap)
    cfg = AgentConfig()
    cfg = replace(cfg, desk=replace(cfg.desk, debate_rounds=1))
    assert Debat(cfg).sur(["EURUSD"], BRIEFS, {})["EURUSD"]["tours"] == 1


def test_mesure_capture_l_ecart_du_tour_1(monkeypatch):
    """gap_initial doit refleter le TOUR 1, meme si le tour 2 change les convictions —
    sinon on ne pourrait pas mesurer ce qu'un second tour a change. C'est la donnee qui
    manquait pour regler DESK_DEBATE_GAP sur des mesures."""
    _bind()
    cap = Capture(suites={
        "bull": [_these(0.6), _these(0.9)],                 # tour 1: 0.6 ; tour 2: 0.9
        "bear": [_these(0.55, "bear"), _these(0.2, "bear")],
        "juge": [_verdict()]})
    install(monkeypatch, cap)
    res = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})["EURUSD"]
    assert res["tours"] == 2
    m = res["mesure"]
    assert m["gap_initial"] == 0.05                          # |0.6 - 0.55|, capture au tour 1
    assert m["gap_final"] == 0.70                            # |0.9 - 0.2|, apres le tour 2


def test_le_juge_voit_tous_les_tours(monkeypatch):
    _bind()
    cap = Capture({"bull": _these(0.6), "bear": _these(0.55, "bear"), "juge": _verdict()})
    install(monkeypatch, cap)
    Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})
    assert cap.prompts["juge"][0].count("these du bull") == 2       # les 2 tours


def test_verdict_illisible_vaut_abstention(monkeypatch):
    _bind()
    cap = Capture({"bull": _these(0.8), "bear": _these(0.2, "bear"),
                   "juge": {"direction": "peut-etre", "conviction": "haute"}})
    install(monkeypatch, cap)
    v = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})["EURUSD"]["verdict"]
    assert v["direction"] == "abstention" and v["conviction"] == 0.0


def test_panne_retire_le_debat_du_symbole(monkeypatch):
    _bind()
    install(monkeypatch, Capture({"bull": _these(0.8)}, down=("bear",)))
    assert Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {}) == {}


def test_juge_injoignable_annule_le_debat(monkeypatch):
    """Un debat sans arbitre n'est qu'une paire d'opinions : on ne le transmet pas."""
    _bind()
    install(monkeypatch, Capture({"bull": _these(0.9), "bear": _these(0.2, "bear")},
                                 down=("juge",)))
    assert Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {}) == {}


# =========================================================== anti-speculation
def test_argument_sans_preuve_est_ecarte(monkeypatch):
    _bind()
    cap = Capture({"bull": _these(0.8, arguments=["le dollar va faiblir",
                                                  "cassure du plus haut 1.1120"]),
                   "bear": _these(0.2, "bear"), "juge": _verdict()})
    install(monkeypatch, cap)
    bull = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})["EURUSD"]["bull"]
    assert bull["arguments"] == ["cassure du plus haut 1.1120"]
    assert bull["ecartes_sans_preuve"] == ["le dollar va faiblir"]
    assert bull["conviction"] == 0.8                  # il reste un argument sourcé


def test_plaidoirie_sans_aucune_preuve_perd_sa_conviction(monkeypatch):
    """Plaider sans chiffre ne doit pas payer : la conviction tombe a zero."""
    _bind()
    cap = Capture({"bull": _these(0.9, arguments=["ca va monter", "le marche est haussier"]),
                   "bear": _these(0.2, "bear"), "juge": _verdict()})
    install(monkeypatch, cap)
    res = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})["EURUSD"]
    assert res["bull"]["conviction"] == 0.0 and res["bull"]["arguments"] == []


def test_verdict_non_source_devient_abstention(monkeypatch):
    """Le juge conclut a l'achat sans rattacher son plan a une donnee : abstention."""
    _bind()
    cap = Capture({"bull": _these(0.9), "bear": _these(0.2, "bear"),
                   "juge": {"direction": "buy", "conviction": 0.8, "gagnant": "bull",
                            "plan": "acheter, la tendance est bien orientee",
                            "invalidation": "si la tendance se retourne"}})
    install(monkeypatch, cap)
    v = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})["EURUSD"]["verdict"]
    assert v["direction"] == "abstention" and v["conviction"] == 0.0
    assert "citent aucune donnee" in v["raison_abstention"]


def test_verdict_source_garde_sa_trace(monkeypatch):
    _bind()
    cap = Capture({"bull": _these(0.9), "bear": _these(0.2, "bear"), "juge": _verdict()})
    install(monkeypatch, cap)
    v = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})["EURUSD"]["verdict"]
    assert v["direction"] == "buy" and "1.0900" in v["preuves"]


def test_debat_entierement_speculatif_ne_produit_aucun_trade(monkeypatch):
    """Deux camps sans preuve : meme un juge convaincu ne peut pas engager d'argent."""
    _bind()
    cap = Capture({"bull": _these(0.9, arguments=["ca va monter"]),
                   "bear": _these(0.8, "bear", arguments=["ca va baisser"]),
                   "juge": _verdict()})
    install(monkeypatch, cap)
    v = Debat(AgentConfig()).sur(["EURUSD"], BRIEFS, {})["EURUSD"]["verdict"]
    assert v["direction"] == "abstention"
    assert "argument" in v["raison_abstention"]


# =========================================================== le verdict s'impose (desk)
def _open(direction="buy", symbol="EURUSD"):
    return {"type": "open", "strategy": "trend_follow", "symbol": symbol,
            "direction": direction, "entry": 1.10,
            "sl": 1.09 if direction == "buy" else 1.11,
            "tp": 1.13 if direction == "buy" else 1.07,
            "confidence": 0.7,
            "rationale": "cassure du plus haut 1.1120, invalidation sous 1.0900"}


def _desk_capture(verdict, direction_trader="buy"):
    return Capture({
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"], "posture": "selectif"},
        "technique": {"biais": "haussier", "confiance": 0.7, "resume": "cassure",
                      "points_cles": ["plus haut 20j a 1.1120"]},
        "fondamental": {"biais": "neutre", "confiance": 0.3, "resume": "taux stables"},
        "sentiment": {"biais": "baissier", "confiance": 0.5, "resume": "retail long 80%"},
        "actualite": {"biais": "neutre", "confiance": 0.4, "resume": "rien d'imminent"},
        "bull": _these(0.9), "bear": _these(0.2, "bear"),
        "juge": verdict,
        "trader": {"actions": [_open(direction_trader)]},
        "risk": {"verdicts": [{"symbol": "EURUSD", "direction": direction_trader,
                               "verdict": "approve"}]},
    })


def test_ouverture_conforme_au_juge_passe(monkeypatch):
    _bind()
    cap = _desk_capture(_verdict("buy"))
    install(monkeypatch, cap)
    actions = TradingDesk(AgentConfig()).decide(SUMMARY)
    assert len(actions) == 1
    d = actions[0]["dossier"]["debat"]
    assert d["verdict"]["direction"] == "buy" and d["tours"] == 1
    assert d["conviction_bull"] == 0.9 and d["conviction_bear"] == 0.2
    assert "VERDICT DU JUGE" in cap.prompts["trader"][0]


def test_le_dossier_archive_la_mesure_du_debat(monkeypatch):
    """La mesure du debat (ecart des convictions) doit survivre dans le dossier du trade,
    pour la revue par employe."""
    _bind()
    cap = _desk_capture(_verdict("buy"))       # bull 0.9 vs bear 0.2, un seul tour
    install(monkeypatch, cap)
    actions = TradingDesk(AgentConfig()).decide(SUMMARY)
    m = actions[0]["dossier"]["debat"]["mesure"]
    assert m["gap_initial"] == 0.7 and m["tours"] == 1


def test_abstention_du_juge_supprime_l_ouverture(monkeypatch):
    _bind()
    install(monkeypatch, _desk_capture(_verdict("abstention")))
    assert TradingDesk(AgentConfig()).decide(SUMMARY) == []


def test_direction_opposee_au_juge_supprimee(monkeypatch):
    """Le Trader propose un achat, le juge a tranche a la vente : l'ordre saute."""
    _bind()
    install(monkeypatch, _desk_capture(_verdict("sell"), direction_trader="buy"))
    assert TradingDesk(AgentConfig()).decide(SUMMARY) == []


def test_sans_debat_le_trader_reste_responsable(monkeypatch):
    """Debat coupe : aucun filtre de verdict, le desk fonctionne comme en Phase 2."""
    _bind()
    cap = _desk_capture(_verdict("sell"), direction_trader="buy")
    install(monkeypatch, cap)
    cfg = AgentConfig()
    cfg = replace(cfg, desk=replace(cfg.desk, use_debate=False))
    actions = TradingDesk(cfg).decide(SUMMARY)
    assert len(actions) == 1                       # l'achat passe : personne ne l'a juge
    assert "juge" not in cap.ordre


def test_debat_hs_laisse_le_trader_decider(monkeypatch):
    """Panne du debat : pas de filtre (on ne supprime pas un trade sur un debat absent)."""
    _bind()
    cap = _desk_capture(_verdict("buy"))
    cap.down = {"juge"}
    install(monkeypatch, cap)
    actions = TradingDesk(AgentConfig()).decide(SUMMARY)
    assert len(actions) == 1
    assert "debat" not in actions[0]["dossier"]
