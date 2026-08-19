# -*- coding: utf-8 -*-
"""Phase 1C/1D — MESURE : garde d'entree, dossier de decision, journal, rejeu.

Trois choses verifiees ici :
  1. le niveau d'entree annonce par le LLM ne sert plus a dimensionner un lot sur une
     fiction : il est recale sur le prix reel, ou refuse s'il est trop loin ;
  2. chaque ouverture du desk emporte son DOSSIER DE DECISION (qui a dit quoi), sans
     lequel aucune attribution par role n'est possible ;
  3. le journal des cycles et le simulateur de rejeu donnent des chiffres corrects
     (dont le cas ambigu stop+cible dans la meme bougie, tranche en faveur du STOP).
"""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig
from brain import tools as T
from desk import journal, replay
from desk.base import DeskAgent, DeskUnavailable
from desk.desk import TradingDesk
from store import Store


T0 = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


# ============================================================ 1. garde d'entree
class _Broker:
    """Broker minimal : une cotation et des bougies plates (ATR ~4 pips)."""
    connected = True

    def __init__(self, bid=1.1050, ask=1.1052):
        self._tick = {"bid": bid, "ask": ask}

    def tick(self, symbol):
        return dict(self._tick)

    def candles(self, symbol, timeframe, n=300):
        import numpy as np
        import pandas as pd
        c = np.full(n, 1.1050)
        return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC"),
                             "open": c, "high": c + 0.0002, "low": c - 0.0002, "close": c,
                             "tick_volume": np.full(n, 100)})


SPEC = {"price_to_pips": 10_000.0, "digits": 5, "pip_value_per_lot": 10.0,
        "min_lot": 0.01, "max_lot": 100.0, "lot_step": 0.01, "spread_pips": 1.0}


def _orchestrateur():
    import run as R
    o = R.Orchestrator()
    o.broker = _Broker()
    o._bars_cache = {}
    return o


def _prop(entry, direction="buy"):
    from risk.ftmo import TradeProposal
    sl, tp = (entry - 0.0050, entry + 0.0150) if direction == "buy" \
        else (entry + 0.0050, entry - 0.0150)
    return TradeProposal(symbol="EURUSD", direction=direction, entry=entry, sl=sl, tp=tp)


def test_entree_proche_recalee_sur_le_prix_executable():
    """L'ordre part au marche : c'est le prix REEL qui doit servir au sizing."""
    o = _orchestrateur()
    prop, refus = o._entry_guard("EURUSD", SPEC, _prop(1.1051))
    assert refus == ""
    assert prop.entry == 1.1052                     # ask, car buy
    prop_sell, refus_sell = o._entry_guard("EURUSD", SPEC, _prop(1.1051, "sell"))
    assert refus_sell == "" and prop_sell.entry == 1.1050        # bid, car sell


def test_entree_inventee_trop_loin_est_refusee():
    """40 pips au-dessus du marche : le trade raisonne n'est pas celui qu'on executerait."""
    o = _orchestrateur()
    prop, refus = o._entry_guard("EURUSD", SPEC, _prop(1.1450))
    assert refus and "du prix reel" in refus
    assert prop.entry == 1.1450                     # inchangee : on refuse, on ne recale pas


def test_recalage_desactivable():
    o = _orchestrateur()
    o.cfg = replace(o.cfg, execution=replace(o.cfg.execution, entry_reprice=False))
    prop, refus = o._entry_guard("EURUSD", SPEC, _prop(1.1051))
    assert refus == "" and prop.entry == 1.1051


def test_sans_cotation_on_ne_bloque_pas():
    """Pas de tick (marche ferme, symbole exotique) : le broker tranchera, pas nous."""
    o = _orchestrateur()
    o.broker.tick = lambda s: None
    prop, refus = o._entry_guard("EURUSD", SPEC, _prop(1.1450))
    assert refus == "" and prop.entry == 1.1450


# ============================================================ 2. dossier de decision
class _Router:
    def __init__(self, by_role):
        self.by_role = by_role

    def json(self, agent, s, h):
        return self.by_role.get(agent.role, {})


def test_ouverture_du_desk_porte_son_dossier_de_decision(monkeypatch):
    """Sans trace de QUI a decide, on ne peut attribuer un resultat a personne."""
    account = {"equity": 100000.0, "objectif_atteint": False, "perte_jour_pct": 0.5,
               "positions_ouvertes": 0, "ouvertures_bloquees": False, "gate_raisons": []}
    T.bind_context({"EURUSD": {"symbol": "EURUSD", "close": 1.1000, "atr": 0.0040}},
                   {}, account, [], "trend_up", {})
    T.bind_live(None)
    router = _Router({
        "gerant": {"convoquer_desk": True, "candidats": ["EURUSD"], "posture": "selectif",
                   "revue": "objectif loin", "consignes": "privilegier le trend"},
        "trader": {"actions": [{"type": "open", "strategy": "trend_follow", "symbol": "EURUSD",
                                "direction": "buy", "entry": 1.10, "sl": 1.09, "tp": 1.13,
                                "confidence": 0.7,
                                "rationale": "cassure de 1.1000, ATR 0.0040"}]},
        "risk": {"verdicts": [{"symbol": "EURUSD", "direction": "buy", "verdict": "reduce",
                               "risk_pct": 0.5, "reason": "volatilite elevee"}]},
    })
    monkeypatch.setattr(DeskAgent, "ask_json", lambda self, s, h: router.json(self, s, h))

    # noyau seul : analystes, debat et college du risque ont leurs propres fichiers de tests
    cfg = AgentConfig()
    cfg = replace(cfg, desk=replace(cfg.desk, use_analysts=False, use_debate=False,
                                    use_risk_debate=False))
    actions = TradingDesk(cfg).decide(account)
    assert len(actions) == 1
    d = actions[0]["dossier"]
    assert d["mandat"]["posture"] == "selectif"
    assert d["mandat"]["consignes"] == "privilegier le trend"
    assert d["trader"]["strategy"] == "trend_follow" and d["trader"]["confidence"] == 0.7
    assert d["risque"]["verdict"] == "reduce" and d["risque"]["risk_pct"] == 0.5
    assert actions[0]["risk_pct"] == 0.5            # le durcissement reste transmis au moteur


# ============================================================ 3. journal des cycles
def _store(tmp_path) -> Store:
    return Store(tmp_path / "test.db", migrate=False)


def _ctx(sym="EURUSD"):
    return {"account": {"equity": 100000.0}, "positions": [],
            "snapshots": {sym: {"symbol": sym}}, "charts": {}, "news": {},
            "strategies": "trend_up", "postmortem": "", "lessons": "(aucune)"}


def _action(sym="EURUSD", entry=1.10):
    return {"type": "open", "symbol": sym, "direction": "buy", "entry": entry,
            "sl": entry - 0.01, "tp": entry + 0.02, "strategy": "trend_follow"}


def test_journal_enregistre_le_dossier_et_le_plan(tmp_path):
    st = _store(tmp_path)
    journal.record_cycle(st, _ctx(), [_action()], mode="desk", degraded=False, shadow=True)
    cycles = journal.cycles(st)
    assert len(cycles) == 1
    c = cycles[0]
    assert c["mode"] == "desk" and c["shadow"] is True
    assert c["snapshots"]["EURUSD"]["symbol"] == "EURUSD"     # dossier d'entree rejouable
    opens = journal.planned_opens(st)
    assert len(opens) == 1 and opens[0]["symbol"] == "EURUSD" and opens[0]["shadow"]


def test_journal_ne_garde_que_les_derniers_cycles(tmp_path):
    st = _store(tmp_path)
    for i in range(5):
        journal.record_cycle(st, _ctx(), [_action(entry=1.10 + i / 100)], mode="desk",
                             degraded=False, shadow=False, keep=2)
    cycles = journal.cycles(st)
    assert len(cycles) == 2
    assert round(cycles[-1]["actions"][0]["entry"], 4) == 1.14   # les plus recents survivent


def test_journal_ne_leve_jamais(tmp_path):
    """Une panne de journalisation ne doit jamais empecher de trader."""
    class _KO:
        def log_event(self, *a, **k):
            raise RuntimeError("disque plein")
    journal.record_cycle(_KO(), _ctx(), [_action()], mode="desk", degraded=False, shadow=False)


def test_executed_opens_ignore_les_ordres_rejetes(tmp_path):
    st = _store(tmp_path)
    prop = {"symbol": "EURUSD", "direction": "buy", "entry": 1.10, "sl": 1.09, "tp": 1.13}
    st.log_event("order_sent", {"proposal": prop, "strategy": "trend_follow",
                                "result": {"ok": True, "price": 1.1005}})
    st.log_event("order_sent", {"proposal": prop, "strategy": "trend_follow",
                                "result": {"ok": False, "message": "rejete"}})
    executes = journal.executed_opens(st)
    assert len(executes) == 1 and executes[0]["entry"] == 1.1005


# ============================================================ 3bis. journal des debats
def _debate_dict(direction="buy", conviction=0.7, gap_initial=0.1, gap_final=0.6, tours=2):
    return {"EURUSD": {"tours": tours,
                       "mesure": {"gap_initial": gap_initial, "gap_final": gap_final,
                                  "tours": tours},
                       "verdict": {"direction": direction, "conviction": conviction,
                                   "gagnant": "bull"}}}


def test_journal_enregistre_chaque_debat(tmp_path):
    st = _store(tmp_path)
    journal.record_debates(st, _debate_dict(), mode="desk", shadow=True)
    debs = journal.debates(st)
    assert len(debs) == 1
    d = debs[0]
    assert d["symbol"] == "EURUSD" and d["direction"] == "buy" and d["shadow"] is True
    assert d["gap_initial"] == 0.1 and d["gap_final"] == 0.6 and d["tours"] == 2


def test_journal_debats_capture_les_abstentions(tmp_path):
    """Une abstention ne devient jamais un trade : le journal des debats est sa SEULE trace."""
    st = _store(tmp_path)
    journal.record_debates(st, _debate_dict(direction="abstention", conviction=0.0),
                           mode="desk", shadow=False)
    assert journal.debates(st)[0]["direction"] == "abstention"


def test_journal_debats_vide_n_ecrit_rien(tmp_path):
    st = _store(tmp_path)
    journal.record_debates(st, {}, mode="desk", shadow=False)
    assert journal.debates(st) == []


def test_journal_debats_ne_leve_jamais():
    class _KO:
        def log_event(self, *a, **k):
            raise RuntimeError("disque plein")
    journal.record_debates(_KO(), _debate_dict(), mode="desk", shadow=False)   # pas d'exception


# ============================================================ 4. simulateur de rejeu
def _bars(spec):
    """spec = liste de (high, low, close), une bougie par jour apres T0."""
    return [{"time": T0 + timedelta(days=i + 1), "high": h, "low": b, "close": c}
            for i, (h, b, c) in enumerate(spec)]


def _op(direction="buy", entry=1.1000, sl=1.0950, tp=1.1150):
    return {"ts": T0.isoformat(), "symbol": "EURUSD", "direction": direction,
            "entry": entry, "sl": sl, "tp": tp}


def test_simulation_cible_touchee():
    issue = replay.simulate_open(_op(), _bars([(1.1050, 1.1000, 1.1040),
                                               (1.1160, 1.1030, 1.1155)]))
    assert issue.resultat == "tp" and issue.R == 3.0 and issue.bougies == 2


def test_simulation_stop_touche():
    issue = replay.simulate_open(_op(), _bars([(1.1010, 1.0940, 1.0950)]))
    assert issue.resultat == "sl" and issue.R == -1.0


def test_simulation_ambigue_tranche_pour_le_stop():
    """Stop ET cible dans la meme bougie : chemin inconnu -> hypothese pessimiste."""
    issue = replay.simulate_open(_op(), _bars([(1.1200, 1.0900, 1.1100)]))
    assert issue.resultat == "sl"


def test_simulation_vente():
    issue = replay.simulate_open(_op("sell", 1.1000, 1.1050, 1.0850),
                                 _bars([(1.1010, 1.0840, 1.0860)]))
    assert issue.resultat == "tp" and issue.R == 3.0


def test_simulation_encore_ouverte_valorise_le_latent():
    issue = replay.simulate_open(_op(), _bars([(1.1020, 1.0990, 1.1010),
                                               (1.1030, 1.0995, 1.1025)]))
    assert issue.resultat == "ouvert" and issue.R == 0.5      # +25 pips pour 50 de risque


def test_simulation_ignore_les_bougies_anterieures_a_la_decision():
    """Une bougie AVANT la decision ne peut pas avoir declenche le trade."""
    avant = [{"time": T0 - timedelta(days=1), "high": 1.1200, "low": 1.0900, "close": 1.1000}]
    assert replay.simulate_open(_op(), avant).resultat == "sans_donnees"


def test_simulation_refuse_un_stop_nul():
    assert replay.simulate_open(_op(sl=1.1000), _bars([(1.11, 1.09, 1.10)])).resultat == "invalide"


def test_provision_de_frais_degrade_le_resultat():
    issue = replay.simulate_open(_op(), _bars([(1.1160, 1.1030, 1.1155)]), cout_R=0.05)
    assert issue.R == 2.95


def test_metriques_expectancy_et_drawdown():
    rows = [{"resultat": "tp", "R": 2.0}, {"resultat": "sl", "R": -1.0},
            {"resultat": "sl", "R": -1.0}, {"resultat": "tp", "R": 3.0},
            {"resultat": "sans_donnees", "R": 0.0}]
    m = replay.metrics(rows)
    assert m["exploitables"] == 4 and m["ignores"] == 1
    assert m["somme_R"] == 3.0 and m["expectancy_R"] == 0.75
    assert m["winrate_pct"] == 50.0 and m["profit_factor"] == 2.5
    assert m["max_drawdown_R"] == -2.0                        # de +2 a 0
    assert replay.metrics([])["n"] == 0


def test_simulation_de_bout_en_bout_depuis_le_journal(tmp_path):
    st = _store(tmp_path)
    journal.record_cycle(st, _ctx(), [_action()], mode="desk", degraded=False, shadow=True)
    # le journal horodate en "maintenant" : on fournit des bougies posterieures
    bars = [{"time": datetime.now(timezone.utc) + timedelta(days=i + 1),
             "high": 1.13, "low": 1.115, "close": 1.125} for i in range(2)]
    rows = replay.simulate_opens(journal.planned_opens(st), lambda s: bars)
    assert len(rows) == 1 and rows[0]["resultat"] == "tp" and rows[0]["R"] == 2.0
    assert replay.metrics(rows)["expectancy_R"] == 2.0


# ============================================================ 5. mode ombre (bout en bout)
class _StubBrokerCycle:
    """Book vide, une cotation, des bougies : juste de quoi faire tourner un cycle."""
    connected = True

    def __init__(self):
        self.ordres = []

    def account(self):
        return {"equity": 100_000.0, "balance": 100_000.0, "currency": "USD",
                "free_margin": 90_000.0, "margin_used": 0.0, "margin_level": 0.0}

    def positions(self, own_only=None):
        return []

    def server_now(self):
        return datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)      # mardi midi

    def ensure_symbol(self, s):
        return True

    def symbol_spec(self, s):
        return {"digits": 5, "point": 1e-5, "min_lot": 0.01, "max_lot": 100.0,
                "lot_step": 0.01, "pip_value_per_lot": 10.0, "price_to_pips": 10_000.0,
                "spread_pips": 1.0, "stops_level_pips": 0.0}

    def candles(self, s, tf, n=300):
        import numpy as np
        import pandas as pd
        c = np.linspace(1.09, 1.11, n)
        return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC"),
                             "open": c, "high": c + 0.002, "low": c - 0.002, "close": c,
                             "tick_volume": np.full(n, 100)})

    def tick(self, s):
        return {"bid": 1.1050, "ask": 1.1052, "time": 0}

    def margin_required(self, symbol, lot, direction):
        return 1000.0 * lot

    def place_order(self, symbol, direction, lot, sl, tp, comment=""):
        self.ordres.append((symbol, direction, lot))
        return {"ok": True, "ticket": 1, "price": 1.1052, "slippage_pips": 0.0,
                "spread_pips": 1.0, "message": "OK"}

    def shutdown(self):
        pass


class _StubNews:
    calendar_ok = True

    def snapshot(self, symbols):
        return {"enabled": False, "per_symbol": {}, "blackout": {}}

    def blackout_for(self, s):
        return {"active": False}


class _AgentTest:
    """Cerveau simule : propose une ouverture executable (entree = ask, R:R 3)."""
    degraded, last_error = False, ""

    def decide(self, summary):
        return [{"type": "open", "strategy": "trend_follow", "symbol": "EURUSD",
                 "direction": "buy", "entry": 1.1052, "sl": 1.0952, "tp": 1.1352,
                 "confidence": 0.7, "rationale": "test"}]


def _cycle_orchestrateur(shadow: bool):
    import run as R
    from brain.memory import Memory
    o = R.Orchestrator()
    o.cfg = replace(o.cfg, eval=replace(o.cfg.eval, shadow=shadow))
    o.risk.cfg = o.cfg.ftmo
    o.broker, o.news, o.agent = _StubBrokerCycle(), _StubNews(), _AgentTest()
    o.mem = Memory()
    o.cycle()
    return o


def test_mode_ombre_journalise_le_plan_sans_rien_executer():
    o = _cycle_orchestrateur(shadow=True)
    assert o.broker.ordres == []                      # AUCUN ordre envoye au broker
    cycles = journal.cycles(o.mem.store)
    assert cycles and cycles[-1]["shadow"] is True
    assert cycles[-1]["actions"][0]["symbol"] == "EURUSD"


def test_hors_mode_ombre_le_meme_plan_est_bien_execute():
    """Contre-preuve : sans EVAL_SHADOW, ce plan passe le moteur et part au broker."""
    o = _cycle_orchestrateur(shadow=False)
    assert [s for s, _, _ in o.broker.ordres] == ["EURUSD"]
    cycles = journal.cycles(o.mem.store)
    assert cycles and cycles[-1]["shadow"] is False


def test_source_de_bougies_absente_n_explose_pas():
    def ko(symbol):
        raise RuntimeError("MT5 injoignable")
    rows = replay.simulate_opens([_op()], ko)
    assert rows[0]["resultat"] == "sans_donnees"
