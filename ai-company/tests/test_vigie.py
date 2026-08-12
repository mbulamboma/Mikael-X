# -*- coding: utf-8 -*-
"""Phase 1B — VIGIE & SESSION EXTRAORDINAIRE.

Ce qui est verifie ici :
  - les declencheurs sont DETERMINISTES (aucun appel LLM a vide) et rate-limites par ticket ;
  - la chaine Vigie -> DG -> Trade Manager ne s'active qu'en cas d'escalade puis de
    convocation, et ne rend que des actions sur le ticket vise ;
  - une session extraordinaire ne peut QUE reduire le risque (jamais ouvrir) ;
  - toute panne (vigie, DG, trade manager injoignables) laisse le filet deterministe
    intact au lieu de casser le watchdog.
"""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig
from brain import tools as T
from desk.base import DeskAgent, DeskUnavailable
from desk.desk import TradingDesk


SUMMARY = {"equity": 100_000.0, "perte_jour_pct": 0.8, "positions_ouvertes": 1}


def _position(ticket=77, symbol="EURUSD", floating_R=-0.9, mfe_R=0.2):
    return {"ticket": ticket, "symbol": symbol, "direction": "buy", "volume": 0.1,
            "entry": 1.1000, "price_now": 1.0960, "sl": 1.0950, "tp": 1.1200,
            "strategy": "trend_follow", "floating_R": floating_R, "floating_pnl": -450.0,
            "dist_sl_pips": 10.0, "dist_tp_pips": 240.0, "mfe_R": mfe_R, "mae_R": -0.9,
            "age_h": 30.0, "trailing": None}


def _bind(positions):
    T.bind_context({}, {}, SUMMARY, positions, "", "", {"blackout": {}})
    T.bind_live(None)


class Router:
    """Reponses LLM simulees par role ; `down` leve DeskUnavailable."""
    def __init__(self, by_role=None, down=()):
        self.by_role = by_role or {}
        self.down = set(down)
        self.calls = []

    def json(self, agent, system, human):
        self.calls.append(agent.role)
        if agent.role in self.down:
            raise DeskUnavailable(f"{agent.role} down (test)")
        return self.by_role.get(agent.role, {})


def install(monkeypatch, router):
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: router.json(self, s, h))


def _alerte(pos=None, declencheurs=("perte flottante -0.9R",)):
    return [{"position": pos or _position(), "declencheurs": list(declencheurs)}]


# =========================================================== chaine d'escalade (desk)
def test_escalade_complete_produit_une_action_sur_le_ticket(monkeypatch):
    pos = _position()
    _bind([pos])
    router = Router({
        "vigie": {"gravite": "escalader", "raison": "these cassee sous le support",
                  "recommandation": "fermer"},
        "gerant": {"session_extraordinaire": True, "decision": "on sort",
                   "consignes": "verifier la structure D1"},
        "suivi": {"actions": [{"type": "close", "ticket": 77, "fraction": 1.0,
                               "reason": "these cassee"}]},
    })
    install(monkeypatch, router)
    actions = TradingDesk(AgentConfig()).watch(_alerte(pos), SUMMARY)
    assert len(actions) == 1
    assert actions[0]["type"] == "close" and actions[0]["ticket"] == 77
    assert router.calls == ["vigie", "gerant", "suivi"]


def test_gravite_ok_ne_reveille_pas_le_dg(monkeypatch):
    _bind([_position()])
    router = Router({"vigie": {"gravite": "ok", "raison": "bruit de marche"}})
    install(monkeypatch, router)
    assert TradingDesk(AgentConfig()).watch(_alerte(), SUMMARY) == []
    assert router.calls == ["vigie"]                  # le DG n'a jamais ete sollicite


def test_dg_peut_refuser_la_session(monkeypatch):
    _bind([_position()])
    router = Router({
        "vigie": {"gravite": "escalader", "raison": "perte qui s'installe"},
        "gerant": {"session_extraordinaire": False, "decision": "le stop fait son travail"},
    })
    install(monkeypatch, router)
    assert TradingDesk(AgentConfig()).watch(_alerte(), SUMMARY) == []
    assert router.calls == ["vigie", "gerant"]        # le Trade Manager n'a pas tourne


def test_session_ignore_les_actions_hors_ticket(monkeypatch):
    """La session est CIBLEE : elle ne doit pas devenir une revue generale du book."""
    pos = _position(ticket=77)
    _bind([pos, _position(ticket=88, symbol="GBPUSD")])
    router = Router({
        "vigie": {"gravite": "escalader", "raison": "gains rendus"},
        "gerant": {"session_extraordinaire": True, "decision": "on securise"},
        "suivi": {"actions": [
            {"type": "modify", "ticket": 77, "sl": 1.1000, "tp": 0, "reason": "break-even"},
            {"type": "close", "ticket": 88, "fraction": 1.0, "reason": "hors sujet"}]},
    })
    install(monkeypatch, router)
    actions = TradingDesk(AgentConfig()).watch(_alerte(pos), SUMMARY)
    assert [a["ticket"] for a in actions] == [77]


def test_session_ne_peut_jamais_ouvrir(monkeypatch):
    """Meme si un LLM devie, aucune ouverture ne peut sortir d'une session extraordinaire."""
    pos = _position()
    _bind([pos])
    router = Router({
        "vigie": {"gravite": "escalader", "raison": "news imminente"},
        "gerant": {"session_extraordinaire": True, "decision": "on protege"},
        "suivi": {"actions": [{"type": "open", "strategy": "trend_follow", "symbol": "EURUSD",
                               "direction": "buy", "entry": 1.10, "sl": 1.09, "tp": 1.13,
                               "confidence": 0.9, "rationale": "rebond"}]},
    })
    install(monkeypatch, router)
    assert TradingDesk(AgentConfig()).watch(_alerte(pos), SUMMARY) == []


def test_verdict_illisible_ne_reveille_pas_le_dg(monkeypatch):
    _bind([_position()])
    router = Router({"vigie": {"gravite": "n'importe quoi"}})
    install(monkeypatch, router)
    assert TradingDesk(AgentConfig()).watch(_alerte(), SUMMARY) == []


# =========================================================== degradations
def test_vigie_injoignable_ne_casse_rien(monkeypatch):
    _bind([_position()])
    install(monkeypatch, Router(down=("vigie",)))
    desk = TradingDesk(AgentConfig())
    assert desk.watch(_alerte(), SUMMARY) == []
    assert desk.degraded is False          # le watchdog deterministe protege deja


def test_dg_injoignable_pendant_l_escalade(monkeypatch):
    _bind([_position()])
    install(monkeypatch, Router({"vigie": {"gravite": "escalader", "raison": "x"}},
                                down=("gerant",)))
    assert TradingDesk(AgentConfig()).watch(_alerte(), SUMMARY) == []


def test_trade_manager_injoignable_pendant_la_session(monkeypatch):
    _bind([_position()])
    install(monkeypatch, Router({
        "vigie": {"gravite": "escalader", "raison": "x"},
        "gerant": {"session_extraordinaire": True, "decision": "on sort"}}, down=("suivi",)))
    assert TradingDesk(AgentConfig()).watch(_alerte(), SUMMARY) == []


def test_vigie_desactivable(monkeypatch):
    from dataclasses import replace
    _bind([_position()])
    router = Router({"vigie": {"gravite": "escalader"}})
    install(monkeypatch, router)
    cfg = AgentConfig()
    cfg = replace(cfg, desk=replace(cfg.desk, vigie_enabled=False))
    assert TradingDesk(cfg).watch(_alerte(), SUMMARY) == []
    assert router.calls == []                        # aucun appel LLM


# =========================================================== declencheurs (orchestrateur)
def _orchestrateur():
    import run as R
    o = R.Orchestrator()

    class _News:
        enabled = True

        def blackout_for(self, s):
            return {"active": False}

        def snapshot(self, symbols):
            return {"enabled": False, "per_symbol": {}, "blackout": {}}

    o.news = _News()
    return o


def test_declencheur_perte_flottante():
    o = _orchestrateur()
    assert o._vigie_triggers(_position(floating_R=-0.9))          # seuil defaut -0.6R
    assert not o._vigie_triggers(_position(floating_R=-0.2))


def test_declencheur_gains_rendus():
    """MFE a +2R puis flottant retombe a +0.4R : c'est le defaut n1 du bilan."""
    o = _orchestrateur()
    decl = o._vigie_triggers(_position(floating_R=0.4, mfe_R=2.0))
    assert decl and "gains rendus" in decl[0]
    assert not o._vigie_triggers(_position(floating_R=1.8, mfe_R=2.0))


def test_declencheur_news_imminente():
    o = _orchestrateur()
    o.news.blackout_for = lambda s: {"active": True, "reason": "NFP", "in_hours": 1}
    decl = o._vigie_triggers(_position(floating_R=0.0))
    assert decl and "news imminente" in decl[0]


class _AgentVigie:
    """Cerveau simule : compte les reveils de la vigie et rend une action de reduction."""
    degraded, last_error = False, ""

    def __init__(self, actions=None):
        self.vus = []
        self.actions = actions if actions is not None else []

    def watch(self, alertes, summary):
        self.vus.append([a["position"]["ticket"] for a in alertes])
        return list(self.actions)

    def decide(self, summary):
        return []


def _pos_broker(ticket=77):
    return {"ticket": ticket, "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
            "entry": 1.1000, "sl": 1.0950, "tp": 1.1200, "floating": -450.0,
            "open_time": datetime.now(timezone.utc) - timedelta(hours=30)}


def _prepare(o, agent):
    """Broker minimal + meta d'un trade en perte, comme le verrait le watchdog."""
    class _Broker:
        connected = True

        def positions(self, own_only=None):
            return [dict(_pos_broker(), a_nous=True)]

        def symbol_spec(self, s):
            return {"digits": 5, "price_to_pips": 10_000.0, "pip_value_per_lot": 10.0,
                    "min_lot": 0.01, "max_lot": 100.0, "lot_step": 0.01}

        def ensure_symbol(self, s):
            return True

        def tick(self, s):
            return {"bid": 1.0960, "ask": 1.0962}

        def close_position(self, ticket, fraction=1.0, comment=""):
            self.ferme = ticket
            return {"ok": True, "full": True, "message": "close OK"}

    o.broker = _Broker()
    o.agent = agent
    o.mail = type("_M", (), {"alerte": lambda *a, **k: None})()
    return {"77": {"ticket": 77, "symbol": "EURUSD", "strategy": "trend_follow",
                   "risk_dollars": 500.0, "mfe_R": 0.2, "mae_R": -0.9}}


def test_watchdog_reveille_la_vigie_et_applique_la_reduction():
    o = _orchestrateur()
    agent = _AgentVigie([{"type": "close", "ticket": 77, "fraction": 1.0, "reason": "these"}])
    meta = _prepare(o, agent)
    o._vigie_watch(o.broker.positions(), meta, SUMMARY)
    assert agent.vus == [[77]]                       # perte flottante -0.9R -> alerte
    assert o.broker.ferme == 77                      # action de reduction executee


def test_anti_spam_par_ticket():
    """Un meme ticket n'est pas re-soumis a la vigie avant DESK_VIGIE_MIN_MINUTES."""
    o = _orchestrateur()
    agent = _AgentVigie()
    meta = _prepare(o, agent)
    o._vigie_watch(o.broker.positions(), meta, SUMMARY)
    o._vigie_watch(o.broker.positions(), meta, SUMMARY)
    assert agent.vus == [[77]]                       # une seule consultation
    o._vigie_last["77"] -= timedelta(minutes=o.cfg.desk.vigie_min_minutes + 1)
    o._vigie_watch(o.broker.positions(), meta, SUMMARY)
    assert agent.vus == [[77], [77]]                 # le delai ecoule, on peut reconsulter


def test_agent_solo_sans_vigie_est_ignore():
    """L'agent solo n'a pas de methode `watch` : le watchdog ne doit pas broncher."""
    o = _orchestrateur()
    meta = _prepare(o, type("_Solo", (), {"degraded": False})())
    o._vigie_watch(o.broker.positions(), meta, SUMMARY)   # ne leve pas


def test_panne_de_la_boucle_vigie_est_silencieuse():
    o = _orchestrateur()

    class _KO(_AgentVigie):
        def watch(self, alertes, summary):
            raise RuntimeError("bedrock down")

    meta = _prepare(o, _KO())
    o._vigie_watch(o.broker.positions(), meta, SUMMARY)   # ne leve pas
