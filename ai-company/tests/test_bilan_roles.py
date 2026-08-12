# -*- coding: utf-8 -*-
"""Revue de performance par employe (desk/bilan_roles.py).

Le point le plus important teste ici n'est pas le calcul : c'est l'HONNETETE STATISTIQUE.
Sous `MIN_N` observations, le module doit REFUSER de conclure et le dire explicitement,
au lieu d'afficher une esperance qui ferait diriger l'entreprise d'apres du bruit.
"""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desk import bilan_roles as B


def _trade(R, direction="buy", biais_technique="haussier", conviction=0.8, tours=1,
           verdict="approve", posture="selectif", confidence=0.8):
    return {"symbol": "EURUSD", "strategy": "trend_follow", "direction": direction, "R": R,
            "dossier": {
                "mandat": {"posture": posture},
                "trader": {"confidence": confidence},
                "risque": {"verdict": verdict},
                "briefs": {"technique": {"biais": biais_technique, "confiance": 0.7}},
                "debat": {"tours": tours, "verdict": {"conviction": conviction}}}}


def test_sans_trade_trace_pas_de_bilan():
    assert B.bilan([]) == {}
    assert B.bilan([{"symbol": "X", "R": 1.0}]) == {}          # pas de dossier -> ignore
    assert "aucun trade tracé" in B.bloc_prompt([])


def test_analyste_aligne_vs_oppose():
    """Le technique est haussier : sur les achats il est aligne, sur les ventes il est contre."""
    trades = ([_trade(2.0) for _ in range(5)]                    # haussier + buy = aligne
              + [_trade(-1.0, direction="sell") for _ in range(5)])  # haussier + sell = oppose
    a = B.bilan(trades)["analystes"]["technique"]
    assert a["aligne"]["n"] == 5 and a["aligne"]["expectancy_R"] == 2.0
    assert a["oppose"]["n"] == 5 and a["oppose"]["expectancy_R"] == -1.0


def test_biais_neutre_n_est_compte_nulle_part():
    trades = [_trade(1.0, biais_technique="neutre") for _ in range(5)]
    assert "technique" not in (B.bilan(trades).get("analystes") or {})


def test_juge_par_conviction_et_debat_par_tours():
    trades = ([_trade(1.0, conviction=0.9) for _ in range(5)]
              + [_trade(-1.0, conviction=0.4, tours=2) for _ in range(5)])
    b = B.bilan(trades)
    assert b["juge"]["conviction>=0.7"]["expectancy_R"] == 1.0
    assert b["juge"]["conviction<0.7"]["expectancy_R"] == -1.0
    assert b["debat"]["2 tour(s)"]["n"] == 5


def test_college_du_risque_par_verdict():
    trades = ([_trade(2.0, verdict="approve") for _ in range(5)]
              + [_trade(0.5, verdict="reduce") for _ in range(5)])
    college = B.bilan(trades)["college_risque"]
    assert college["approve"]["expectancy_R"] == 2.0
    assert college["reduce"]["expectancy_R"] == 0.5


def test_gerant_par_posture_et_trader_par_confiance():
    trades = ([_trade(1.0, posture="agressif", confidence=0.9) for _ in range(5)]
              + [_trade(-0.5, posture="prudent", confidence=0.4) for _ in range(5)])
    b = B.bilan(trades)
    assert b["gerant_par_posture"]["agressif"]["expectancy_R"] == 1.0
    assert b["trader_par_confiance"]["confiance<0.7"]["expectancy_R"] == -0.5


# =========================================================== honnetete statistique
def test_echantillon_insuffisant_refuse_de_conclure():
    """3 trades : le chiffre existe, mais le bloc doit dire de ne rien en conclure."""
    trades = [_trade(3.0) for _ in range(3)]
    b = B.bilan(trades)
    assert b["analystes"]["technique"]["aligne"]["suffisant"] is False
    bloc = B.bloc_prompt(trades)
    assert "ECHANTILLON INSUFFISANT" in bloc
    assert "BRUIT" in bloc                         # avertissement global en tete
    assert "+3.000R" not in bloc                   # le chiffre flatteur n'est pas affiche


def test_echantillon_suffisant_affiche_les_chiffres():
    trades = [_trade(3.0) for _ in range(B.MIN_N)]
    bloc = B.bloc_prompt(trades)
    assert "ECHANTILLON INSUFFISANT" not in bloc.split("ANALYSTE TECHNIQUE")[1].split("oppose")[0]
    assert "expectancy=+3.000R" in bloc


def test_le_bloc_reste_lisible_avec_des_donnees_partielles():
    """Dossiers incomplets (trades d'une version anterieure) : pas d'exception, on saute."""
    trades = [{"symbol": "EURUSD", "direction": "buy", "R": 1.0, "dossier": {"mandat": {}}}]
    assert isinstance(B.bloc_prompt(trades), str)


# =========================================================== integration & CLI
def test_le_gerant_recoit_la_revue(monkeypatch, tmp_path):
    """Le DG doit savoir qui apporte du signal, pas seulement ce que le compte a fait."""
    from dataclasses import replace
    from config import AgentConfig
    from brain import tools as T
    from brain.memory import Memory
    from desk.base import DeskAgent
    from desk.desk import TradingDesk
    from store import Store, set_default_store
    import store as store_mod

    precedent = store_mod._DEFAULT
    try:
        set_default_store(Store(tmp_path / "revue.db", migrate=False))
        mem = Memory()
        for _ in range(B.MIN_N):
            mem.log_event("trade_closed", _trade(2.0))
        summary = {"equity": 100_000.0, "objectif_atteint": False, "perte_jour_pct": 0.1,
                   "positions_ouvertes": 0, "ouvertures_bloquees": False, "gate_raisons": []}
        T.bind_context({"EURUSD": {"symbol": "EURUSD"}}, {}, summary, [], "", "trend_up", {})
        T.bind_live(None)
        prompts = {}
        monkeypatch.setattr(DeskAgent, "ask_json",
                            lambda self, s, h: prompts.setdefault(self.role, h) and {})
        cfg = AgentConfig()
        cfg = replace(cfg, desk=replace(cfg.desk, use_analysts=False, use_debate=False,
                                        use_risk_debate=False))
        TradingDesk(cfg).decide(summary)
        assert "REVUE DE PERFORMANCE DES EMPLOYES" in prompts["gerant"]
        assert "ANALYSTE TECHNIQUE" in prompts["gerant"]
    finally:
        set_default_store(precedent)


def test_cli_roles_tourne_sans_mt5_ni_llm(capsys):
    """`--roles` est purement statistique : ni broker, ni appel facture."""
    from desk import replay
    assert replay.main(["--roles"]) == 0
    assert "REVUE DE PERFORMANCE" in capsys.readouterr().out
