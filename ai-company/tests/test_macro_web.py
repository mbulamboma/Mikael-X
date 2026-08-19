# -*- coding: utf-8 -*-
"""BIAIS MACRO SANS MT5 (data/macro_web.py) — remplace macro_features.csv.

Tout est hors-ligne : on fabrique les evenements a la main, aucune requete.

Verifie :
  - la surprise est relative et bornee (une prevision a 0 ne fait pas exploser le score) ;
  - un indicateur inverse (chomage) retourne le signe : plus de chomeurs != bonne nouvelle ;
  - l'importance pondere (une annonce majeure pese plus qu'une mineure) ;
  - une devise sans evenement chiffre est ABSENTE (pas de faux neutre rassurant) ;
  - le momentum des taux FRED porte le biais de chaque devise, et se combine
    aux surprises quand il y en a ;
  - la couverture est exposee : un score assis sur un seul chiffre s'annonce comme faible ;
  - des donnees pourries ne levent jamais (un cycle ne casse pas pour une macro).
"""
import _isolation  # noqa: F401
import sys

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.macro_web import macro_bias, _relative

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _ev(ccy, nom, actual, forecast, importance=3, heures=2):
    quand = (NOW - timedelta(hours=heures)).replace(tzinfo=None)
    return {"currency": ccy, "event": nom, "importance": importance,
            "actual": actual, "forecast": forecast, "when": quand.isoformat()}


def test_surprise_relative_bornee():
    assert _relative(1.1, 1.0) == pytest.approx(0.1 / 1.1)  # base = max(|fc|, |act|)
    assert _relative(0.3, 0.0) == 1.0                       # prevision nulle : borne, pas d'infini
    assert _relative(-0.3, 0.0) == -1.0
    assert -1.0 <= _relative(50.0, 0.1) <= 1.0


def test_chiffre_meilleur_que_prevu_donne_un_biais_positif():
    out = macro_bias([_ev("EUR", "CPI y/y", 2.4, 2.0)], {}, NOW)
    assert out["EUR"]["biais"] > 0 and out["EUR"]["lecture"] == "positif"


def test_chomage_est_inverse():
    """Meme surprise arithmetique, sens oppose : +0.4 pt de chomage est une MAUVAISE
    nouvelle. L'ancien service, lui, comptait toute hausse comme positive."""
    hausse = macro_bias([_ev("USD", "Unemployment Rate", 4.4, 4.0)], {}, NOW)
    assert hausse["USD"]["biais"] < 0


def test_importance_pondere():
    fort = macro_bias([_ev("GBP", "CPI y/y", 3.0, 2.0, importance=3),
                       _ev("GBP", "Retail Sales m/m", 1.8, 2.0, importance=2)], {}, NOW)
    # la surprise positive a fort impact doit l'emporter sur la negative a impact moyen
    assert fort["GBP"]["biais"] > 0


def test_devise_sans_evenement_est_absente():
    out = macro_bias([_ev("EUR", "CPI y/y", 2.4, 2.0)], {}, NOW)
    assert "JPY" not in out and "CHF" not in out


def test_evenement_sans_chiffre_ne_compte_pas():
    muet = {"currency": "AUD", "event": "RBA Gov Speaks", "importance": 3,
            "when": (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()}
    assert "AUD" not in macro_bias([muet], {}, NOW)


def test_momentum_des_taux_porte_le_biais_de_chaque_devise():
    """Des taux qui montent soutiennent une devise. C'est le socle du biais depuis que
    macro_features.csv a disparu : il vaut meme sans le moindre evenement au calendrier."""
    taux = {"USD": {"momentum": 0.6, "libelle": "2 ans US", "age_jours": 1},
            "JPY": {"momentum": -0.4, "libelle": "10 ans Japon", "age_jours": 3}}
    out = macro_bias([], taux, NOW)
    assert out["USD"]["lecture"] == "positif" and out["JPY"]["lecture"] == "negatif"
    assert out["USD"]["taux"]["serie"] == "2 ans US"
    assert "EUR" not in out                        # sans taux ni evenement : omise


def test_taux_et_surprise_se_combinent():
    taux = {"EUR": {"momentum": 0.4, "libelle": "depot BCE", "age_jours": 1}}
    seul = macro_bias([], taux, NOW)["EUR"]["biais"]
    avec = macro_bias([_ev("EUR", "CPI y/y", 2.4, 2.0)], taux, NOW)["EUR"]["biais"]
    assert avec > seul                             # la surprise positive renforce le biais


def test_serie_de_taux_perimee_degrade_la_fiabilite():
    frais = {"AUD": {"momentum": 0.5, "libelle": "10 ans", "age_jours": 2}}
    vieux = {"AUD": {"momentum": 0.5, "libelle": "10 ans", "age_jours": 80}}
    assert macro_bias([], frais, NOW)["AUD"]["fiabilite"] == "moyenne"
    assert macro_bias([], vieux, NOW)["AUD"]["fiabilite"] == "faible"


def test_couverture_et_fiabilite_sont_annoncees():
    out = macro_bias([_ev("CAD", "CPI m/m", 0.4, 0.2)], {}, NOW)
    assert out["CAD"]["couverture"]["evenements_24h"] == 1
    assert out["CAD"]["fiabilite"] == "faible"     # un seul chiffre : ne pas y croire


def test_hors_fenetre_72h_ignore():
    vieux = _ev("NZD", "CPI q/q", 1.0, 0.2, heures=200)
    assert "NZD" not in macro_bias([vieux], {}, NOW)


def test_donnees_pourries_ne_levent_jamais():
    assert macro_bias(None, None, NOW) == {}
    assert macro_bias([{"currency": "EUR"}], {}, NOW) == {}
    assert macro_bias([{"currency": "EUR", "actual": "x", "forecast": None,
                        "when": "pas une date"}], {}, NOW) == {}


# ------------------------------------------------------------------ calendrier : cache & 429
class _Reponse:
    """Reponse HTTP minimale (le vrai flux repond du HTML en 429)."""
    def __init__(self, text, status=200, ctype="application/json"):
        self.text, self.status_code, self.headers = text, status, {"Content-Type": ctype}

    def json(self):
        import json as _j
        return _j.loads(self.text)


def _calendrier(monkeypatch, reponses):
    """WebCalendar dont chaque appel HTTP consomme la reponse suivante de `reponses`."""
    from data import calendar_web as C
    appels = {"n": 0}

    def faux_get(url, **kw):
        r = reponses[min(appels["n"], len(reponses) - 1)]
        appels["n"] += 1
        return r
    monkeypatch.setattr(C.requests, "get", faux_get)
    return C.WebCalendar(), appels


def test_calendrier_ne_retape_pas_le_flux_dans_la_fenetre_de_cache(monkeypatch):
    """get_macro_events est un OUTIL : sans cache, les analystes martelent le flux
    jusqu'au HTTP 429 — et le black-out news tombe pour cause de gourmandise."""
    payload = '[{"title": "CPI", "country": "USD", "date": "2026-08-19T12:30:00+00:00", "impact": "High"}]'
    cal, appels = _calendrier(monkeypatch, [_Reponse(payload)])
    for _ in range(5):
        cal._fetch()
    assert appels["n"] == 1


def test_calendrier_rejoue_le_cache_sur_429(monkeypatch):
    payload = '[{"title": "CPI", "country": "USD", "date": "2026-08-19T12:30:00+00:00", "impact": "High"}]'
    cal, _ = _calendrier(monkeypatch, [_Reponse(payload), _Reponse("<html>429</html>", 429, "text/html")])
    assert len(cal._fetch()) == 1
    cal.cache_min = 0                      # force la re-interrogation -> tombe sur le 429
    assert len(cal._fetch()) == 1          # le dernier bon jeu protege encore


def test_calendrier_perime_ne_protege_plus_et_rend_vide(monkeypatch):
    payload = '[{"title": "CPI", "country": "USD", "date": "2026-08-19T12:30:00+00:00", "impact": "High"}]'
    cal, _ = _calendrier(monkeypatch, [_Reponse(payload), _Reponse("<html>429</html>", 429, "text/html")])
    cal._fetch()
    cal.cache_min = 0
    cal.peremption_min = 0                 # cache trop vieux : ce n'est plus une protection
    assert cal._fetch() == []              # -> calendar_ok=False -> log ERROR dans news.py
