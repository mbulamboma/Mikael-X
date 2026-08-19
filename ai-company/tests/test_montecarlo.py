# -*- coding: utf-8 -*-
"""SIMULATEUR MONTE-CARLO — probabilite de passage FTMO.

Verifie :
  - REPRODUCTIBILITE : meme graine -> meme resultat (exigence de validation) ;
  - le sens : bon edge + petit sizing -> P(reussite) haute, ~0 bust ; mauvais edge +
    gros sizing -> bust majoritaire ;
  - la construction du modele depuis les trades (empirique au-dela du seuil, sinon calibre) ;
  - les overrides de sensibilite (risque/trade, trades/jour) agissent ;
  - les probabilites forment bien une partition (somme = 1).
"""
import _isolation  # noqa: F401
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import AgentConfig
from risk.montecarlo import MonteCarlo, ReturnModel, SimConfig, simulate, _pct


FTMO = AgentConfig().ftmo


def _run(model, **sim):
    return MonteCarlo(FTMO, model, SimConfig(n_paths=4000, **sim)).run()


# ------------------------------------------------------------------ reproductibilite
def test_meme_graine_meme_resultat():
    m = ReturnModel(win_rate=0.45, win_R=2.0, loss_R=-1.0)
    a = _run(m, seed=7)
    b = _run(m, seed=7)
    assert a.p_reussite == b.p_reussite and a.equity_finale == b.equity_finale


def test_graines_differentes_resultats_proches_mais_distincts():
    m = ReturnModel(win_rate=0.45, win_R=2.0, loss_R=-1.0)
    a = _run(m, seed=1)
    b = _run(m, seed=2)
    assert a.p_reussite != b.p_reussite               # tirage different
    assert abs(a.p_reussite - b.p_reussite) < 0.1     # mais meme ordre de grandeur


# ------------------------------------------------------------------ le sens du modele
def test_bon_edge_petit_sizing_passe_souvent_sans_buster():
    r = _run(ReturnModel(win_rate=0.5, win_R=2.0, loss_R=-1.0), seed=3)
    assert r.p_reussite > 0.7
    assert r.p_bust_jour + r.p_bust_total < 0.05      # les buffers agent protegent


def test_mauvais_edge_gros_sizing_buste():
    r = _run(ReturnModel(win_rate=0.35, win_R=1.0, loss_R=-1.0), seed=3,
             risk_per_trade_override=3.0, max_trades_per_day_override=5)
    assert r.expectancy_R < 0
    assert r.p_bust_jour > 0.5                         # le sur-sizing detruit tout


def test_probabilites_partitionnent():
    r = _run(ReturnModel(win_rate=0.45), seed=9)
    total = r.p_reussite + r.p_bust_jour + r.p_bust_total + r.p_delai
    assert abs(total - 1.0) < 1e-9


# ------------------------------------------------------------------ modele depuis l'historique
def test_from_trades_empirique_au_dela_du_seuil():
    trades = [{"R": 1.5} for _ in range(15)] + [{"R": -1.0} for _ in range(15)]  # 30 >= 20
    m = ReturnModel.from_trades(trades, min_n=20)
    assert len(m.samples) == 30                       # mode empirique


def test_from_trades_calibre_si_trop_court():
    trades = [{"R": 2.0}, {"R": -1.0}, {"R": -1.0}, {"R": 2.0}]  # 4 < 20
    m = ReturnModel.from_trades(trades, min_n=20)
    assert m.samples == []                            # bascule parametrique
    assert 0.0 < m.win_rate < 1.0                     # calibre sur l'historique court


def test_from_trades_vide_donne_defauts():
    m = ReturnModel.from_trades([])
    assert m.samples == [] and m.win_rate == 0.45


def test_simulate_raccourci():
    r = simulate(FTMO, trades=[{"R": 2.0}, {"R": -1.0}] * 30, sim=SimConfig(n_paths=2000, seed=5))
    assert "empirique" in r.source_distribution
    assert 0.0 <= r.p_reussite <= 1.0


# ------------------------------------------------------------------ overrides & helpers
def test_override_risque_augmente_le_bust():
    m = ReturnModel(win_rate=0.4, win_R=1.5, loss_R=-1.0)
    prudent = _run(m, seed=4, risk_per_trade_override=1.0, max_trades_per_day_override=5)
    agressif = _run(m, seed=4, risk_per_trade_override=4.0, max_trades_per_day_override=5)
    assert (agressif.p_bust_jour + agressif.p_bust_total) > \
           (prudent.p_bust_jour + prudent.p_bust_total)


def test_percentile_interpolation():
    xs = [10, 20, 30, 40]
    assert _pct(xs, 0) == 10 and _pct(xs, 100) == 40
    assert _pct(xs, 50) == 25.0                       # entre 20 et 30


def test_rapport_texte_lisible():
    r = _run(ReturnModel(win_rate=0.45), seed=1)
    t = r.texte()
    assert "PROBABILITE DE PASSAGE FTMO" in t and "P(REUSSITE)" in t
