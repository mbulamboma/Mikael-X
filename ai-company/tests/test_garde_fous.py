"""Correctifs de la revue de risque : filet deterministe, magic, jour serveur,
correlation, calendrier indisponible, tool calling par famille de modele."""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import FTMOConfig, SafeModeConfig, BedrockConfig
from risk.ftmo import FTMOEngine, AccountState, TradeProposal, TradeCosts, currencies_of
from brain.autopilot import SafePilot
from brain import tools as T


# ---------------------------------------------------------------- A1 : filet sans IA
class _Broker:
    connected = True

    def __init__(self, pos=None, etrangeres=None):
        self.pos = pos if pos is not None else []
        self.etr = etrangeres or []
        self.closes, self.modifs = [], []

    def account(self):
        return {"equity": 96_500.0, "balance": 96_500.0, "currency": "USD",
                "free_margin": 90_000.0}

    def positions(self, own_only=None):
        mine = [dict(p, a_nous=True) for p in self.pos]
        if own_only is False:
            return mine + [dict(p, a_nous=False) for p in self.etr]
        return mine

    def server_now(self):
        return datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    def ensure_symbol(self, s):
        return True

    def symbol_spec(self, s):
        return {"digits": 2 if s == "XAUUSD" else 5, "price_to_pips": 10_000.0,
                "pip_value_per_lot": 10.0, "min_lot": 0.01, "max_lot": 100.0,
                "lot_step": 0.01, "spread_pips": 1.0, "stops_level_pips": 0.0}

    def candles(self, s, tf, n=300):
        import numpy as np
        import pandas as pd
        c = np.linspace(1.10, 1.11, n)
        return pd.DataFrame({"time": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
                             "open": c, "high": c + 0.002, "low": c - 0.002, "close": c,
                             "tick_volume": np.full(n, 100)})

    def tick(self, s):
        return {"bid": 1.1050, "ask": 1.1052}

    def close_position(self, ticket, fraction=1.0, comment=""):
        self.closes.append(ticket)
        self.pos = [p for p in self.pos if p["ticket"] != ticket]
        return {"ok": True, "full": True, "message": "close OK"}

    def modify_position(self, ticket, sl=None, tp=None):
        self.modifs.append((ticket, sl))
        return {"ok": True, "message": "modify OK"}


class _Mem:
    def __init__(self):
        self.events, self.meta, self.session, self.lessons = [], {}, {}, []

    def log_event(self, kind, payload):
        self.events.append((kind, payload))

    def load_meta(self):
        return dict(self.meta)

    def save_meta(self, d):
        self.meta = dict(d)

    def load_session(self):
        return dict(self.session)

    def save_session(self, d):
        self.session = dict(d)

    def add_lesson(self, *a, **k):
        self.lessons.append(a)


def _orchestrateur(pos=None, etrangeres=None):
    import run as R
    o = R.Orchestrator()
    o.broker, o.mem = _Broker(pos, etrangeres), _Mem()
    return o


def _pos(ticket=1, symbol="EURUSD", sl=1.0950, floating=-200.0):
    return {"ticket": ticket, "symbol": symbol, "direction": "buy", "volume": 0.5,
            "entry": 1.1000, "sl": sl, "tp": 1.1200, "floating": floating,
            "open_time": datetime.now(timezone.utc)}


def test_perte_du_jour_ferme_tout_meme_si_l_ia_va_bien():
    """A1 : le filet ne depend plus de la panne du LLM."""
    o = _orchestrateur([_pos(1), _pos(2, "XAUUSD")])
    o.mem.session = {"initial_balance": 100_000.0, "day_start_balance": 100_000.0,
                     "trades_today": 0, "trading_days": []}
    summary = {"perte_jour_pct": 3.5, "pnl_total_pct": -3.5, "equity": 96_500.0}
    urgence = o._protect(o.broker.positions(), [], summary, {}, o.mem.session, o.broker.account())
    assert urgence is True
    assert sorted(o.broker.closes) == [1, 2]
    assert "urgence_ftmo" in [k for k, _ in o.mem.events]


def test_positions_d_un_autre_ea_jamais_fermees_mais_signalees():
    """A2 : on ne touche qu'a nos positions (magic)."""
    o = _orchestrateur([_pos(1)], etrangeres=[_pos(99, "GBPUSD")])
    o.mem.session = {"initial_balance": 100_000.0, "day_start_balance": 100_000.0}
    etr = [p for p in o.broker.positions(own_only=False) if not p["a_nous"]]
    assert [p["ticket"] for p in etr] == [99]
    o._protect(o.broker.positions(), etr, {"perte_jour_pct": 3.9, "pnl_total_pct": -3.9},
               {}, o.mem.session, o.broker.account())
    assert o.broker.closes == [1]                      # la 99 n'est PAS fermee


def test_le_watchdog_protege_sans_llm():
    """A3 : la surveillance tourne entre deux cycles LLM."""
    o = _orchestrateur([_pos(1, floating=-3000.0)])
    o.mem.session = {"initial_balance": 100_000.0, "day_start_balance": 100_000.0,
                     "trades_today": 0, "trading_days": []}
    o.watchdog()
    assert o.broker.closes == [1]                      # perte jour 3.5% > 3% (75% de 4%)
    assert o.agent is None                             # aucun LLM sollicite


def test_mise_a_plat_avant_le_week_end():
    """A9 : option de fermeture avant la coupure hebdomadaire."""
    import os
    import importlib
    os.environ["AGENT_WEEKEND_FLATTEN"] = "1"
    import config
    importlib.reload(config)
    try:
        o = _orchestrateur([_pos(1)])
        o.cfg = config.CFG
        o.broker.server_now = lambda: datetime(2026, 8, 14, 21, 0, tzinfo=timezone.utc)
        o._server_now = None
        assert o._flatten_weekend_due() is True
        o._protect(o.broker.positions(), [], {"perte_jour_pct": 0.1, "pnl_total_pct": 1.0},
                   {}, {}, o.broker.account())
        assert o.broker.closes == [1]
    finally:
        os.environ.pop("AGENT_WEEKEND_FLATTEN")
        importlib.reload(config)


# ---------------------------------------------------------------- A4 : jour serveur
def test_la_journee_suit_l_horloge_du_serveur():
    import run as R
    o = _orchestrateur()
    o.broker.server_now = lambda: datetime(2026, 8, 12, 0, 30, tzinfo=timezone.utc)
    o._server_now = None
    assert R._today_key(o.server_now()) == "2026-08-12"
    # 23h30 UTC = deja le lendemain cote serveur (UTC+2/+3) : c'est ce jour-la qui compte
    o.broker.server_now = lambda: datetime(2026, 8, 12, 1, 30, tzinfo=timezone.utc)
    o._server_now = None
    assert R._today_key(o.server_now()) == "2026-08-12"


# ---------------------------------------------------------------- A7 : digits reels
def test_stop_d_urgence_arrondi_avec_les_digits_du_broker():
    pilot = SafePilot(SafeModeConfig(), FTMOConfig())
    or_pos = {"ticket": 7, "symbol": "XAUUSD", "direction": "buy", "entry": 2400.00,
              "sl": 0.0, "tp": 0.0, "price_now": 2410.0, "floating_R": None, "age_h": 5.0}
    acts = pilot.protective_actions([or_pos], {"XAUUSD": 20.0},
                                    spec_of=lambda s: {"digits": 2})
    sl = acts[0]["sl"]
    assert acts[0]["type"] == "modify"
    assert sl == round(2400.0 - 1.5 * 20.0, 2) and len(str(sl).split(".")[1]) <= 2


# ---------------------------------------------------------------- A8 : correlation
def test_plafond_de_risque_par_devise():
    e = FTMOEngine(FTMOConfig())
    acc = AccountState(equity=100_000.0, balance=100_000.0, day_start_balance=100_000.0,
                       initial_balance=100_000.0, open_positions=2,
                       open_risk_by_symbol={"EURUSD": 1000.0, "GBPUSD": 1000.0},
                       open_risk_by_currency={"USD": 2000.0, "EUR": 1000.0, "GBP": 1000.0},
                       trades_today=1)
    p = TradeProposal(symbol="AUDUSD", direction="buy", entry=0.6600, sl=0.6550, tp=0.6750)
    d = e.validate(p, acc, pip_value_per_lot=10.0, price_to_pips=10_000.0, min_lot=0.01,
                   max_lot=100.0, lot_step=0.01,
                   costs=TradeCosts(max_risk_per_currency_pct=2.0))
    assert not d.approved and "USD" in d.reasons[0]


def test_devises_d_un_symbole():
    assert currencies_of("EURUSD") == ["EUR", "USD"]
    assert currencies_of("XAUUSD") == ["XAU", "USD"]
    assert "USD" in currencies_of("US30") and "EQUITY" in currencies_of("US30")


# ---------------------------------------------------------------- A10 : jours minimum
def test_objectif_atteint_ne_bloque_pas_avant_le_minimum_de_jours():
    e = FTMOEngine(FTMOConfig())
    base = dict(equity=111_000.0, balance=111_000.0, day_start_balance=111_000.0,
                initial_balance=100_000.0, open_positions=0, open_risk_by_symbol={},
                trades_today=0)
    peu = AccountState(**base, trading_days=2)
    assert not any("OBJECTIF" in b for b in e.portfolio_gate(peu))   # critere non rempli
    assez = AccountState(**base, trading_days=4)
    assert any("OBJECTIF" in b for b in e.portfolio_gate(assez))     # la, on preserve


# ---------------------------------------------------------------- A5 : calendrier absent
def test_calendrier_indisponible_coupe_le_black_out_visiblement():
    """Si le calendrier web ne rend rien, `calendar_ok` doit tomber a False : le
    black-out ne protege plus, et l'orchestrateur doit pouvoir le savoir. Le CSV MT5
    n'existe plus — c'est justement lui qui masquait la panne en restant present et perime."""
    from data.news import NewsFeed
    from config import NewsConfig
    f = NewsFeed.__new__(NewsFeed)
    f.cfg = NewsConfig()
    f.calendar_ok = True
    f._web_cal = type("Vide", (), {"events": lambda *a, **k: ([], [])})()
    assert f._calendar(datetime.now(timezone.utc)) == ([], [])
    assert f.calendar_ok is False


# ---------------------------------------------------------------- tool calling par famille
def test_mode_json_selectionne_pour_deepseek():
    b = BedrockConfig(model_id="us.deepseek.r1-v1:0")
    assert b.supports_tools is False
    assert BedrockConfig(model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0").supports_tools


def test_cle_api_bedrock_exportee_pour_boto3():
    import os
    os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
    BedrockConfig(api_key="cle-de-test")
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "cle-de-test"
    os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)


def test_plan_json_valide_par_les_memes_controles():
    T.bind_context({}, {}, {}, [], "", {})
    plan = {"analyse": "test", "actions": [
        {"type": "open", "strategy": "trend_follow", "symbol": "EURUSD", "direction": "buy",
         "entry": 1.10, "sl": 1.09, "tp": 1.13, "confidence": 0.7},
        {"type": "open", "symbol": "GBPUSD", "direction": "buy",                # incoherent
         "entry": 1.25, "sl": 1.27, "tp": 1.30},
        {"type": "close", "ticket": 5, "fraction": 0.5},
        {"type": "trail", "ticket": 5, "atr_mult": 2.0, "timeframe": "h4"},
        {"type": "n_importe_quoi", "ticket": 9},
        "pas un dict",
    ]}
    actions = T.apply_plan(plan)
    kinds = [a["type"] for a in actions]
    assert kinds == ["open", "close", "trail"]            # l'incoherent est ecarte
    assert actions[0]["symbol"] == "EURUSD"
    assert actions[2]["timeframe"] == "H4"


def test_extraction_json_tolerante_au_bavardage():
    from brain.agent import _extract_json
    brut = ('Voici mon raisonnement...\n```json\n{"analyse": "ok", "actions": []}\n```\n'
            'Fin.')
    assert _extract_json(brut) == {"analyse": "ok", "actions": []}
    assert _extract_json("aucun json ici") == {}
    assert _extract_json('{"a": {"b": 1}} et du bruit')["a"] == {"b": 1}


def test_dossier_de_contexte_complet_pour_le_mode_sans_outils():
    T.bind_context({"EURUSD": {"price": 1.1}}, {"EURUSD": {"levels": {}}},
                   {"equity": 100000}, [{"ticket": 1}], "regime trend_up",
                   {"enabled": True, "blackout": {"EURUSD": {"active": False}}},
                   postmortem="defaut: gains rendus")
    d = T.context_digest()
    for attendu in ("COMPTE", "POSITIONS", "MARCHES", "CHART EURUSD", "STRATEGIES",
                    "DEFAUTS RECURRENTS", "NEWS"):
        assert attendu in d
    assert "LECON" not in d                     # le dossier ne contient que des faits
    assert "gains rendus" in d and "trend_up" in d


# ------------------------------------------------ echec par INACTIVITE (pas que le drawdown)
from run import _alertes_ftmo


def _info(**kw):
    base = dict(objectif_atteint=False, jours_restants=25, jours_trades_manquants=0,
                jours_depuis_dernier_trade=0, limite_inactivite_jours=30)
    base.update(kw)
    return base


def test_aucune_alerte_quand_minimum_tenu_et_compte_actif():
    assert _alertes_ftmo(_info(jours_trades_manquants=0, jours_depuis_dernier_trade=1)) == []


def test_alerte_critique_minimum_jours_hors_datteinte():
    a = _alertes_ftmo(_info(jours_restants=3, jours_trades_manquants=4))
    assert a and a[0].startswith("CRITIQUE")


def test_alerte_risque_minimum_jours_serre():
    a = _alertes_ftmo(_info(jours_restants=5, jours_trades_manquants=4))
    assert a and a[0].startswith("RISQUE") and "jours de trading" in a[0]


def test_alerte_inactivite_proche_limite():
    a = _alertes_ftmo(_info(jours_depuis_dernier_trade=27, limite_inactivite_jours=30))
    assert any("inactif" in x for x in a)


def test_objectif_atteint_ne_declenche_pas_lalerte_jours():
    # objectif fait : on n'exige plus de forcer des jours de trading supplementaires.
    assert _alertes_ftmo(_info(objectif_atteint=True, jours_restants=1,
                               jours_trades_manquants=4)) == []
