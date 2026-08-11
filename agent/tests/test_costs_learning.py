"""Frictions reelles (spread/commission/slippage/gap/marge) + apprentissage chiffre."""
import _isolation  # noqa: F401  (base SQLite temporaire)
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import FTMOConfig
from risk.ftmo import FTMOEngine, AccountState, TradeProposal, TradeCosts
from brain.postmortem import PostMortem

SPEC = dict(pip_value_per_lot=10.0, price_to_pips=10_000.0,
            min_lot=0.01, max_lot=100.0, lot_step=0.01)


def _acc(**kw):
    base = dict(equity=100_000.0, balance=100_000.0, day_start_balance=100_000.0,
                initial_balance=100_000.0, open_positions=0, open_risk_by_symbol={},
                trades_today=0)
    base.update(kw)
    return AccountState(**base)


def _prop(sl=1.0950, tp=1.1150):
    return TradeProposal(symbol="EURUSD", direction="buy", entry=1.1000, sl=sl, tp=tp)


def _engine():
    return FTMOEngine(FTMOConfig())


def _costs(**kw):
    base = dict(spread_pips=2.0, slippage_pips=1.0, commission_per_lot=7.0,
                max_spread_pips=3.0, max_spread_atr_ratio=0.12, atr_pips=100.0,
                max_margin_pct_of_free=20.0)
    base.update(kw)
    return TradeCosts(**base)


# ------------------------------------------------------------------ sizing
def test_les_frais_reduisent_le_lot_et_le_rr_net():
    e = _engine()
    sans = e.validate(_prop(), _acc(), **SPEC)
    avec = e.validate(_prop(), _acc(), **SPEC, costs=_costs())
    assert sans.approved and avec.approved
    assert avec.lot < sans.lot                       # meme risque $ -> moins de lot
    assert avec.effective_stop_pips > avec.stop_pips  # spread + slippage comptes
    assert avec.rr_net < avec.rr                     # le R:R net paie les frais
    assert avec.costs_dollars > 0
    assert avec.risk_dollars <= 100_000 * 0.01 * 1.05


def test_sizing_sans_couts_reste_compatible():
    d = _engine().validate(_prop(), _acc(), **SPEC)
    assert d.approved and d.lot == 2.0 and round(d.risk_dollars) == 1000
    assert d.rr == 3.0


def test_veto_spread_absolu_et_relatif_a_l_atr():
    e = _engine()
    large = e.validate(_prop(), _acc(), **SPEC, costs=_costs(spread_pips=5.0))
    assert not large.approved and "Spread anormal" in large.reasons[0]
    # spread absolu OK mais enorme face a l'ATR (marche fin)
    fin = e.validate(_prop(), _acc(), **SPEC, costs=_costs(spread_pips=2.5, atr_pips=10.0))
    assert not fin.approved and "ATR" in fin.reasons[0]


def test_veto_stop_plus_proche_que_le_minimum_broker():
    d = _engine().validate(_prop(), _acc(), **SPEC, costs=_costs(stops_level_pips=60.0))
    assert not d.approved and "Stop trop proche" in d.reasons[0]


def test_veto_rr_net_insuffisant():
    # 50 pips de stop pour 55 de cible : brut 1.1, mais les frais tuent l'esperance
    d = _engine().validate(_prop(tp=1.1055), _acc(), **SPEC, costs=_costs())
    assert not d.approved
    assert any("R:R NET" in r for r in d.reasons)


def test_veto_marge_insuffisante():
    d = _engine().validate(_prop(), _acc(), **SPEC,
                           costs=_costs(margin_required_per_lot=5_000.0, free_margin=10_000.0))
    assert not d.approved and any("Marge requise" in r for r in d.reasons)


def test_garde_week_end_bloque_le_vendredi_soir():
    import run as R
    o = R.Orchestrator()
    assert o._weekend_guard(datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc))   # vendredi 21h
    assert not o._weekend_guard(datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc))
    assert not o._weekend_guard(datetime(2026, 8, 13, 23, 0, tzinfo=timezone.utc))  # jeudi


# ------------------------------------------------------------------ apprentissage
def _t(**kw):
    base = {"kind": "trade_closed", "symbol": "EURUSD", "strategy": "trend_follow",
            "regime": "trend_up", "R": 1.0, "pnl": 100.0, "rr_planifie": 2.0,
            "mfe_R": 1.5, "mae_R": -0.3, "confidence": 0.6,
            "slippage_pips": 0.5, "spread_pips_entree": 1.2, "couts_estimes": 10.0}
    base.update(kw)
    return base


def test_bilan_agrege_par_strategie_symbole_regime():
    pm = PostMortem([_t(R=2.0, pnl=200), _t(R=-1.0, pnl=-100, strategy="momentum"),
                     _t(R=0.5, pnl=50, symbol="XAUUSD")])
    s = pm.summary()
    assert s["n_trades"] == 3 and s["winrate"] == round(2 / 3 * 100, 1)
    assert s["par_strategie"]["momentum"]["expectancy_R"] == -1.0
    assert "XAUUSD" in s["par_symbole"] and "trend_up" in s["par_regime"]
    assert s["execution"]["slippage_moyen_pips"] == 0.5
    json.dumps(s)                                  # doit rester serialisable


def test_detecte_les_gains_rendus():
    trades = [_t(R=0.0, pnl=0.0, mfe_R=2.2) for _ in range(3)] + [_t(R=1.0) for _ in range(3)]
    flaws = " ".join(PostMortem(trades).flaws())
    assert "GAINS RENDUS" in flaws


def test_detecte_un_stop_trop_serre():
    trades = [_t(R=-1.0, pnl=-100, mfe_R=0.1) for _ in range(4)] + [_t(R=1.0) for _ in range(2)]
    flaws = " ".join(PostMortem(trades).flaws())
    assert "STOP TROP SERRE" in flaws


def test_detecte_l_ecart_plan_realite_et_l_asymetrie():
    trades = [_t(R=0.1, pnl=10, rr_planifie=3.0) for _ in range(4)] + \
             [_t(R=-1.2, pnl=-120, rr_planifie=3.0, mfe_R=0.8)]
    flaws = " ".join(PostMortem(trades).flaws())
    assert "ECART PLAN/REALITE" in flaws and "ASYMETRIE" in flaws


def test_detecte_la_sur_confiance_et_les_frais():
    trades = [_t(R=-0.8, pnl=-80, confidence=0.9, couts_estimes=40.0) for _ in range(5)]
    flaws = " ".join(PostMortem(trades).flaws())
    assert "SUR-CONFIANCE" in flaws and "FRAIS EXCESSIFS" in flaws


def test_signale_une_strategie_a_eviter():
    trades = [_t(R=-1.0, pnl=-100, strategy="mean_reversion", mfe_R=0.9) for _ in range(5)]
    flaws = " ".join(PostMortem(trades).flaws())
    assert "A EVITER" in flaws and "mean_reversion" in flaws


def test_bilan_prompt_lisible_et_prudent_sans_donnees():
    assert "aucun trade" in PostMortem([]).as_prompt_block()
    bloc = PostMortem([_t()] * 3).as_prompt_block()
    assert "Bilan:" in bloc and "pas encore assez de trades" in bloc


def test_mfe_mae_suivis_a_chaque_cycle():
    """MFE/MAE = la matiere premiere du post-mortem : on les enregistre en continu."""
    import run as R

    class B:
        connected = True

        def ensure_symbol(self, s):
            return True

        def symbol_spec(self, s):
            return {"digits": 5, "price_to_pips": 10_000.0, "pip_value_per_lot": 10.0,
                    "min_lot": 0.01, "max_lot": 100.0, "lot_step": 0.01}

        def tick(self, s):
            return {"bid": 1.1050, "ask": 1.1052}

    class M:
        def __init__(self):
            self.saved = None

        def save_meta(self, d):
            self.saved = d

    o = R.Orchestrator()
    o.broker, o.mem = B(), M()
    meta = {"5": {"ticket": 5, "risk_dollars": 100.0, "mfe_R": 0.0, "mae_R": 0.0}}
    pos = [{"ticket": 5, "symbol": "EURUSD", "direction": "buy", "volume": 0.1,
            "entry": 1.1000, "sl": 1.0950, "tp": 1.1200, "floating": 250.0,
            "open_time": datetime.now(timezone.utc)}]

    vue = o._enrich_positions(pos, meta)
    assert vue[0]["mfe_R"] == 2.5 and meta["5"]["mfe_R"] == 2.5   # plus haut atteint
    pos[0]["floating"] = -50.0
    o._enrich_positions(pos, meta)
    assert meta["5"]["mfe_R"] == 2.5 and meta["5"]["mae_R"] == -0.5   # le pic reste memorise
    assert o.mem.saved is meta                                        # persiste


def test_lecons_triees_par_pertinence_et_dedupliquees():
    from brain.memory import Memory
    from store import Store
    mem = Memory(Store(Path(tempfile.mkdtemp()) / "agent.db"))
    mem.add_lesson("USDJPY", "win", "Vieille lecon sans rapport", tags=["momentum"])
    mem.add_lesson("EURUSD", "loss", "Stop trop serre sur EURUSD", tags=["trend_follow"])
    mem.add_lesson("GBPUSD", "win", "Stop trop serre sur EURUSD", tags=["trend_follow"])  # doublon
    txt = mem.relevant_lessons_text(symbols=["EURUSD"], strategies=["trend_follow"], k=5)
    assert txt.splitlines()[0].startswith("- [loss/EURUSD]")        # le plus pertinent d'abord
    assert txt.count("Stop trop serre") == 1                         # doublon ecarte
    assert "Vieille lecon" in txt
