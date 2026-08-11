"""Pilote de secours : si l'IA tombe, un script dur protege les positions puis on sort."""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import SafeModeConfig, FTMOConfig, AgentConfig
from brain.autopilot import SafePilot, implied_r


def _pilot(**kw):
    return SafePilot(SafeModeConfig(**kw), FTMOConfig())


def _pos(**kw):
    base = {"ticket": 1, "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
            "entry": 1.1000, "price_now": 1.1050, "sl": 1.0950, "tp": 1.1200,
            "floating_R": 1.0, "age_h": 24.0, "trailing": None}
    base.update(kw)
    return base


def _account(**kw):
    base = {"equity": 100_000.0, "pnl_total_pct": 0.5, "perte_jour_pct": 0.2}
    base.update(kw)
    return base


def test_rien_a_faire_sans_position():
    assert _pilot().actions([], _account()) == []


def test_perte_du_jour_proche_de_la_limite_ferme_tout():
    p = _pilot()
    acts = p.actions([_pos(ticket=1), _pos(ticket=2, symbol="XAUUSD")],
                     _account(perte_jour_pct=3.2))       # stop agent 4 % x 0.75 = 3 %
    assert [a["type"] for a in acts] == ["close", "close"]
    assert all(a["fraction"] == 1.0 for a in acts)
    assert "perte du jour" in acts[0]["reason"]


def test_perte_totale_declenche_la_fermeture_generale():
    acts = _pilot().actions([_pos()], _account(pnl_total_pct=-7.5))
    assert acts[0]["type"] == "close" and "perte totale" in acts[0]["reason"]


def test_position_sans_stop_recoit_un_sl_d_urgence():
    acts = _pilot().actions([_pos(sl=0.0, floating_R=None)], _account(),
                            atr_by_symbol={"EURUSD": 0.0100})
    assert len(acts) == 1 and acts[0]["type"] == "modify"
    assert acts[0]["sl"] == round(1.1000 - 1.5 * 0.0100, 5)      # 1.5 x ATR sous l'entree


def test_sl_urgence_en_vente_place_au_dessus():
    acts = _pilot().actions([_pos(direction="sell", sl=0.0, floating_R=None)], _account(),
                            atr_by_symbol={"EURUSD": 0.0100})
    assert acts[0]["sl"] == round(1.1000 + 1.5 * 0.0100, 5)


def test_break_even_a_partir_de_1R_puis_plus_de_recul():
    acts = _pilot().actions([_pos(floating_R=1.2)], _account())
    modifs = [a for a in acts if a["type"] == "modify"]
    assert modifs and modifs[0]["sl"] == 1.1000                  # stop ramene a l'entree
    # stop deja au break-even -> plus de modify, seulement le trailing
    acts2 = _pilot().actions([_pos(floating_R=1.2, sl=1.1000)], _account())
    assert [a["type"] for a in acts2] == ["trail"]


def test_trailing_arme_une_seule_fois():
    acts = _pilot().actions([_pos(floating_R=0.5)], _account())
    trails = [a for a in acts if a["type"] == "trail"]
    assert len(trails) == 1 and trails[0]["atr_mult"] == 2.0 and trails[0]["enabled"] is True
    deja = _pilot().actions([_pos(floating_R=0.5, trailing={"enabled": True})], _account())
    assert deja == []


def test_time_stop_ferme_une_position_qui_traine():
    acts = _pilot().actions([_pos(age_h=24 * 12, floating_R=0.1)], _account())
    assert acts[0]["type"] == "close" and "time-stop" in acts[0]["reason"]
    # meme age mais bien en profit -> on laisse courir
    acts2 = _pilot().actions([_pos(age_h=24 * 12, floating_R=1.5)], _account())
    assert all(a["type"] != "close" for a in acts2)


def test_le_pilote_n_ouvre_jamais():
    cas = [_pos(), _pos(sl=0.0), _pos(age_h=500, floating_R=0.0), _pos(floating_R=5.0)]
    for acc in (_account(), _account(perte_jour_pct=9.0), _account(pnl_total_pct=-9.0)):
        for a in _pilot().actions(cas, acc):
            assert a["type"] in {"close", "modify", "trail"}


def test_r_implicite_depuis_la_geometrie():
    # pas de floating_R connu (position heritee) -> deduit de entree/stop/prix
    assert implied_r(_pos(floating_R=None)) == 1.0               # +50 pips pour 50 de risque
    assert implied_r(_pos(floating_R=None, direction="sell", price_now=1.0950)) == 1.0
    assert implied_r(_pos(floating_R=None, sl=0.0)) is None


def test_cycle_complet_bascule_en_secours_puis_arrete_le_script():
    """Bout en bout (broker/memoire/news simules) : IA HS -> gestion deterministe,
    puis arret du script des que la derniere position est fermee."""
    import tempfile
    from datetime import datetime, timezone
    import numpy as np
    import pandas as pd
    import run as R

    def candles(n=300):
        rng = np.random.default_rng(1)
        c = 1.10 + np.cumsum(rng.normal(0, 0.002, n))
        return pd.DataFrame({"time": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
                             "open": c, "high": c + 0.002, "low": c - 0.002, "close": c,
                             "tick_volume": np.full(n, 100)})

    class StubBroker:
        connected = True

        def __init__(self):
            self.pos = [{"ticket": 77, "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
                         "entry": 1.1000, "sl": 1.0950, "tp": 1.1200, "floating": 500.0,
                         "open_time": datetime.now(timezone.utc)}]
            self.modifs, self.closes = [], []

        def account(self):
            return {"equity": 101_000.0, "balance": 101_000.0, "currency": "USD"}

        def positions(self, own_only=None):
            return [dict(p, a_nous=True) for p in self.pos]

        def server_now(self):
            return datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)   # mardi midi

        def ensure_symbol(self, s):
            return True

        def symbol_spec(self, s):
            return {"digits": 5, "point": 1e-5, "min_lot": 0.01, "max_lot": 100.0,
                    "lot_step": 0.01, "pip_value_per_lot": 10.0, "price_to_pips": 10_000.0}

        def candles(self, s, tf, n=300):
            return candles()

        def tick(self, s):
            return {"bid": 1.1050, "ask": 1.1052, "time": 0}

        def modify_position(self, ticket, sl=None, tp=None):
            self.modifs.append((ticket, sl, tp))
            return {"ok": True, "message": "modify OK"}

        def close_position(self, ticket, fraction=1.0, comment=""):
            self.closes.append((ticket, fraction))
            self.pos = [p for p in self.pos if p["ticket"] != ticket]
            return {"ok": True, "full": True, "message": "close OK"}

        def realized_pnl(self, ticket):
            return 250.0

        def shutdown(self):
            pass

    class StubMem:
        def __init__(self):
            self.events, self.session, self.meta, self.lessons = [], {}, {}, []
            self.safe = {}

        def load_session(self):
            return dict(self.session)

        def save_session(self, d):
            self.session = dict(d)

        def load_meta(self):
            return dict(self.meta)

        def save_meta(self, d):
            self.meta = dict(d)

        def load_safe_mode(self):
            return dict(self.safe)

        def save_safe_mode(self, d):
            self.safe = dict(d)

        def clear_safe_mode(self):
            self.safe = {}

        def log_event(self, kind, payload):
            self.events.append((kind, payload))

        def closed_trades(self):
            return []

        def recent_lessons_text(self, k=12):
            return ""

        def relevant_lessons_text(self, symbols=None, strategies=None, k=12):
            return ""

        def add_lesson(self, *a, **kw):
            self.lessons.append((a, kw))

    class StubNews:
        def snapshot(self, symbols):
            return {"enabled": False, "per_symbol": {}, "blackout": {}}

        def blackout_for(self, s):
            return {"active": False}

    class DeadAgent:
        degraded, last_error = True, "Bedrock injoignable (test)"

        def decide(self, account):
            return []

        def reflect(self, trade):
            return "lecon de secours"

    o = R.Orchestrator()
    o.broker, o.mem, o.news, o.agent = StubBroker(), StubMem(), StubNews(), DeadAgent()

    o.cycle()                                   # 1er cycle : ancre les tickets existants
    o.cycle()                                   # 2e cycle : l'IA est HS -> pilote de secours
    types = [k for k, _ in o.mem.events]
    assert "safe_mode" in types                 # bascule tracee
    assert o.mem.load_safe_mode()["actif"] is True    # etat persiste (survit au redemarrage)
    assert o.mem.meta["77"]["trail"]["enabled"] is True     # trailing arme sans LLM
    assert o.broker.modifs                                  # stop remonte au break-even

    o.broker.pos = []                           # la position se ferme (SL/TP touche)
    try:
        o.cycle()
        raise AssertionError("le script aurait du s'arreter (SafeExit)")
    except R.SafeExit:
        pass
    assert o.mem.load_safe_mode() == {}          # trace nettoyee a l'arret propre
    assert "safe_exit" in [k for k, _ in o.mem.events]


def test_agent_signale_l_indisponibilite_du_llm():
    from brain.agent import TraderAgent
    a = TraderAgent(AgentConfig())
    if a.llm is None:                       # cas reel de ce poste : langchain/bedrock absent
        assert a.degraded is True and a.last_error
        assert a.decide({}) == []           # aucune action a l'aveugle
        assert a.degraded is True
