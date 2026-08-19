# -*- coding: utf-8 -*-
"""Desk multi-agents — Phase 1 (noyau : Gerant, Trade Manager, Trader, Risk Manager).

On monkeypatche les appels LLM (DeskAgent.ask_json/ask_text) par des reponses canned
dispatchees par ROLE, pour tester la LOGIQUE d'orchestration et de DEGRADATION sans Bedrock.
Le point critique verifie ici : quand le cerveau de gestion tombe, on bascule sur le pilote
deterministe (comme l'agent solo) ; quand seule la recherche tombe, on gere le book sans paniquer.
"""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig, FTMOConfig
from brain import tools as T
from brain.memory import Memory
from desk.base import DeskAgent, DeskUnavailable
from desk.desk import TradingDesk


# --------------------------------------------------------------------------- utilitaires
def cfg_noyau():
    """Isole le NOYAU decisionnel de la Phase 1 : analystes (Phase 2), debat (Phase 3) et
    college du risque (Phase 3B) coupes — chacun a son propre fichier de tests. Sans ca, le
    juge (absent des reponses simulees ici) conclurait a l'abstention, et le Risk Manager
    mono-voix teste dans ce fichier ne serait meme pas appele."""
    c = AgentConfig()
    return replace(c, desk=replace(c.desk, use_analysts=False, use_debate=False,
                                   use_risk_debate=False))


def test_le_dossier_est_autonome():
    """Le deployable se suffit a lui-meme : code, configuration et etat sont sous la meme
    racine. Si ce test casse, c'est qu'une dependance est remontee hors du dossier."""
    import config
    import desk.desk as d
    assert Path(d.__file__).resolve().parents[1] == config.ROOT
    assert (config.ROOT / ".env.example").exists()      # sa configuration
    assert (config.ROOT / "requirements.txt").exists()  # ses dependances
    assert (config.ROOT / "run.py").exists()            # son point d'entree
    # aucun module du deployable ne doit vivre au-dessus de la racine
    for module in ("run", "store", "notify", "brain.memory", "risk.ftmo", "desk.desk"):
        chemin = Path(__import__(module, fromlist=["_"]).__file__).resolve()
        assert config.ROOT in chemin.parents, f"{module} est hors du dossier deploye"


def test_modeles_a_deux_vitesses():
    """Override individuel > classe rapide/fort > modele partage."""
    c = AgentConfig()
    d = replace(c.desk, model_rapide="rapide", model_fort="fort", model_gerant="special")
    assert d.model_for("gerant", "partage") == "special"      # override individuel gagne
    assert d.model_for("suivi", "partage") == "rapide"        # appele a chaque cycle
    assert d.model_for("vigie", "partage") == "rapide"
    assert d.model_for("trader", "partage") == "fort"         # rare mais lourd
    assert d.model_for("debat", "partage") == "fort"
    assert c.desk.model_for("trader", "partage") == "partage"  # rien de configure


def bind(account=None, positions=None, snapshots=None):
    """Prepare le contexte de cycle (comme le fait l'orchestrateur avant decide)."""
    positions = positions or []
    account = account or {"equity": 100000.0, "objectif_atteint": False,
                          "perte_jour_pct": 0.5, "positions_ouvertes": len(positions),
                          "ouvertures_bloquees": False, "gate_raisons": []}
    T.bind_context(snapshots if snapshots is not None else
                   {"EURUSD": {"symbol": "EURUSD", "close": 1.1000, "atr": 0.0040,
                               "haut_20": 1.1120, "bas_20": 1.0900}},
                   {}, account, positions, "Regime dominant: trend_up", {})
    T.bind_live(None)
    return account


class Router:
    """Reponses LLM simulees par role ; leve DeskUnavailable pour les roles 'down'."""
    def __init__(self, json_by_role=None, text="Lecon test.", down=()):
        self.json_by_role = json_by_role or {}
        self.text = text
        self.down = set(down)
        self.calls = []

    def json(self, agent, system, human):
        self.calls.append(agent.role)
        if agent.role in self.down:
            raise DeskUnavailable(f"{agent.role} down (test)")
        return self.json_by_role.get(agent.role, {})

    def txt(self, agent, system, human):
        self.calls.append(agent.role + ":text")
        if agent.role in self.down:
            raise DeskUnavailable("coach down (test)")
        return self.text


def install(monkeypatch, router):
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: router.json(self, s, h))
    monkeypatch.setattr(DeskAgent, "ask_text", lambda self, s, h: router.txt(self, s, h))


def _open(symbol, direction="buy", entry=1.10, sl=1.09, tp=1.13, conf=0.7):
    return {"type": "open", "strategy": "trend_follow", "symbol": symbol,
            "direction": direction, "entry": entry, "sl": sl, "tp": tp,
            "confidence": conf,
            "rationale": "cassure du plus haut 1.1120, ATR 0.0040"}


# --------------------------------------------------------------------------- flux nominal
def test_desk_ouvre_via_gerant_trader_risk(monkeypatch):
    summary = bind()
    router = Router(json_by_role={
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"], "posture": "selectif"},
        "trader": {"actions": [_open("EURUSD")]},
        "risk": {"verdicts": [{"symbol": "EURUSD", "direction": "buy", "verdict": "approve"}]},
    })
    install(monkeypatch, router)
    actions = TradingDesk(cfg_noyau()).decide(summary)
    assert len(actions) == 1
    a = actions[0]
    assert a["type"] == "open" and a["symbol"] == "EURUSD" and "risk_pct" not in a
    assert {"gerant", "trader", "risk"} <= set(router.calls)


def test_desk_risk_manager_durcit(monkeypatch):
    # snapshots SOURCES pour les deux symboles : sans les chiffres cites par la rationale
    # (1.1120, ATR 0.0040), le filtre de preuves supprimerait les ouvertures avant le risque.
    summary = bind(snapshots={
        "EURUSD": {"symbol": "EURUSD", "close": 1.1000, "atr": 0.0040, "haut_20": 1.1120},
        "GBPUSD": {"symbol": "GBPUSD", "close": 1.2700, "atr": 0.0040, "haut_20": 1.1120}})
    router = Router(json_by_role={
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD", "GBPUSD"]},
        "trader": {"actions": [_open("EURUSD"), _open("GBPUSD", entry=1.27, sl=1.26, tp=1.30)]},
        "risk": {"verdicts": [
            {"symbol": "EURUSD", "direction": "buy", "verdict": "reduce", "risk_pct": 0.5},
            {"symbol": "GBPUSD", "direction": "buy", "verdict": "reject", "reason": "correle USD"},
        ]},
    })
    install(monkeypatch, router)
    actions = TradingDesk(cfg_noyau()).decide(summary)
    assert len(actions) == 1
    assert actions[0]["symbol"] == "EURUSD" and actions[0]["risk_pct"] == 0.5


def test_desk_trade_manager_gere_le_book(monkeypatch):
    positions = [{"ticket": 55, "symbol": "EURUSD", "direction": "buy", "floating_R": 1.5,
                  "sl": 1.09, "tp": 1.13, "strategy": "trend_follow"}]
    summary = bind(positions=positions)
    router = Router(json_by_role={
        "gerant": {"convoquer_desk": False, "candidats": []},
        "suivi": {"actions": [{"type": "modify", "ticket": 55, "sl": 1.10, "reason": "break-even"}]},
    })
    install(monkeypatch, router)
    actions = TradingDesk(cfg_noyau()).decide(summary)
    assert any(a["type"] == "modify" and a["ticket"] == 55 for a in actions)
    assert "trader" not in router.calls              # desk non convoque -> pas de recherche


# --------------------------------------------------------------------------- degradation
def test_desk_degrade_si_gerant_injoignable(monkeypatch):
    summary = bind()
    router = Router(down={"gerant"})
    install(monkeypatch, router)
    desk = TradingDesk(cfg_noyau())
    actions = desk.decide(summary)
    assert desk.degraded and actions == []           # -> l'orchestrateur bascule sur SafePilot


def test_desk_degrade_si_trade_manager_injoignable(monkeypatch):
    positions = [{"ticket": 1, "symbol": "EURUSD", "direction": "buy", "sl": 1.09}]
    summary = bind(positions=positions)
    router = Router(json_by_role={"gerant": {"convoquer_desk": False, "candidats": []}},
                    down={"suivi"})
    install(monkeypatch, router)
    desk = TradingDesk(cfg_noyau())
    actions = desk.decide(summary)
    assert desk.degraded and actions == []


def test_desk_recherche_indisponible_ne_degrade_pas(monkeypatch):
    positions = [{"ticket": 7, "symbol": "EURUSD", "direction": "buy", "sl": 1.09}]
    summary = bind(positions=positions)
    router = Router(json_by_role={
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"]},
        "suivi": {"actions": [{"type": "close", "ticket": 7, "fraction": 1.0, "reason": "these"}]},
    }, down={"trader"})
    install(monkeypatch, router)
    desk = TradingDesk(cfg_noyau())
    actions = desk.decide(summary)
    assert not desk.degraded                          # une panne de recherche ne panique pas
    assert any(a["type"] == "close" and a["ticket"] == 7 for a in actions)  # book toujours gere
    assert all(a["type"] != "open" for a in actions)  # mais aucune ouverture


def test_desk_risk_manager_injoignable_bloque_les_ouvertures(monkeypatch):
    summary = bind()
    router = Router(json_by_role={
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"]},
        "trader": {"actions": [_open("EURUSD")]},
    }, down={"risk"})
    install(monkeypatch, router)
    desk = TradingDesk(cfg_noyau())
    actions = desk.decide(summary)
    assert not desk.degraded and actions == []        # pas d'officier de risque -> pas de risque


def test_desk_ouvertures_bloquees_saute_la_recherche(monkeypatch):
    summary = bind()
    summary["ouvertures_bloquees"] = True             # gate FTMO deterministe actif
    router = Router(json_by_role={
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"]},
        "trader": {"actions": [_open("EURUSD")]},
    })
    install(monkeypatch, router)
    actions = TradingDesk(cfg_noyau()).decide(summary)
    assert "trader" not in router.calls and actions == []


def test_desk_reponse_gerant_vide_defaut_prudent(monkeypatch):
    summary = bind()
    router = Router(json_by_role={"gerant": {}})       # JSON illisible -> defaut prudent
    install(monkeypatch, router)
    desk = TradingDesk(cfg_noyau())
    actions = desk.decide(summary)
    assert not desk.degraded                           # {} n'est pas une panne
    assert "trader" not in router.calls and actions == []  # on ne convoque pas, on n'ouvre pas


# --------------------------------------------------------------------------- moteur : override risque
def test_validate_risk_override_reduit_le_lot():
    from risk.ftmo import FTMOEngine, AccountState, TradeProposal, TradeCosts
    eng = FTMOEngine(FTMOConfig())
    acc = AccountState(equity=100000, balance=100000, day_start_balance=100000,
                       initial_balance=100000, open_positions=0, open_risk_by_symbol={},
                       trades_today=0)
    prop = TradeProposal(symbol="EURUSD", direction="buy", entry=1.10, sl=1.09, tp=1.13)
    costs = TradeCosts(spread_pips=0.5, slippage_pips=0.5, commission_per_lot=7.0,
                       atr_pips=100, free_margin=100000, margin_required_per_lot=3000,
                       max_spread_pips=3.0, max_margin_pct_of_free=20.0)
    kw = dict(pip_value_per_lot=10.0, price_to_pips=10000.0, min_lot=0.01,
              max_lot=100.0, lot_step=0.01, costs=costs)
    full = eng.validate(prop, acc, **kw)
    half = eng.validate(prop, acc, risk_pct_override=0.5, **kw)
    assert full.approved and half.approved
    assert half.lot < full.lot and half.risk_dollars < full.risk_dollars
    # un override PLUS GRAND que le budget ne peut jamais augmenter le risque
    bigger = eng.validate(prop, acc, risk_pct_override=5.0, **kw)
    assert bigger.lot == full.lot


