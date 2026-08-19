import _isolation  # noqa: F401  (base SQLite temporaire)
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store import Store
from brain.memory import Memory
from brain import tools as T


def appel(outil, **kw):
    """Appelle un outil LangChain. Depuis LangChain 1.x un `@tool` est un StructuredTool
    qui n'est plus directement appelable : il faut passer par `.invoke({...})`. Le code de
    production, lui, appelle les fonctions privees (`_plan_open`...), pas les outils."""
    return outil.invoke(kw)
from risk.ftmo import FTMOEngine, AccountState, TradeProposal


def test_plan_open_rejects_incoherent_and_accepts_valid():
    T.bind_context({}, {}, {}, [], "", {})
    # buy avec sl == entry -> incoherent, ne doit PAS creer d'action
    appel(T.plan_open, strategy="trend_follow", symbol="EURUSD", direction="buy",
                entry=1.0, sl=1.0, tp=1.2, confidence=0.8, rationale="")
    assert T.pop_actions() == []
    # setup valide buy (sl < entry < tp) -> une action open
    T.bind_context({}, {}, {}, [], "", {})
    appel(T.plan_open, strategy="trend_follow", symbol="EURUSD", direction="buy",
                entry=1.10, sl=1.09, tp=1.13, confidence=0.7, rationale="ok")
    actions = T.pop_actions()
    assert len(actions) == 1 and actions[0]["type"] == "open"
    assert actions[0]["symbol"] == "EURUSD"


def test_plan_close_and_modify_collected():
    T.bind_context({}, {}, {}, [], "", {})
    appel(T.plan_close, ticket=42, fraction=0.5, reason="prise partielle")
    appel(T.plan_modify, ticket=42, sl=1.10, tp=0.0, reason="break-even")
    actions = T.pop_actions()
    kinds = sorted(a["type"] for a in actions)
    assert kinds == ["close", "modify"]


def test_memory_survit_a_un_etat_corrompu(tmp_path):
    """Ancien etat JSON illisible : il est ignore, pas fatal, et SQLite prend le relais."""
    (tmp_path / "open_meta.json").write_text("{not valid json", encoding="utf-8")
    (tmp_path / "session.json").write_text("<<corrompu>>", encoding="utf-8")
    mem = Memory(Store(tmp_path / "agent.db"))
    mem.log_event("order_sent", {"symbol": "EURUSD"})
    assert mem.store.events(kind="order_sent")[0]["symbol"] == "EURUSD"
    assert mem.load_session() == {}


def test_ftmo_rejects_invalid_trade_geometry():
    engine = FTMOEngine(type("Cfg", (), {"risk_per_trade_pct": 0.5, "daily_stop_pct": 3.0,
                                        "total_soft_stop_pct": 7.0, "profit_target_pct": 5.0,
                                        "max_open_positions": 3, "max_trades_per_day": 5,
                                        "max_risk_per_symbol_pct": 0.5,
                                        "cooldown_minutes_after_loss": 120})())
    acc = AccountState(equity=100_000.0, balance=100_000.0, day_start_balance=100_000.0,
                       initial_balance=100_000.0, open_positions=0, open_risk_by_symbol={}, trades_today=0)
    proposal = TradeProposal(symbol="EURUSD", direction="buy", entry=1.1000, sl=1.1000, tp=1.1200)
    decision = engine.validate(proposal, acc, pip_value_per_lot=10.0, price_to_pips=10_000.0,
                               min_lot=0.01, max_lot=100.0, lot_step=0.01)
    assert decision.approved is False
    assert any("Incoherence" in reason for reason in decision.reasons)
