# -*- coding: utf-8 -*-
"""Remplacants des sources supprimees/gatees :
  - CALENDRIER WEB (faireconomy/ForexFactory) a la place de l'export MT5 ExportCalendar.mq5 ;
  - SENTIMENT RETAIL FXSSI (long/short) a la place de myfxbook (qui exige des identifiants).

Tout HORS-LIGNE : on injecte la charge utile (_fetch / _get), jamais de reseau.
"""
import _isolation  # noqa: F401
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import SourcesConfig
from data import calendar_web as CW
from data.sources import FXSSISource, Sources


# =========================================================== CALENDRIER WEB
def _events_bruts(now):
    from datetime import timedelta
    def iso(dt):  # UTC -> ISO avec offset +00:00 (le parseur gere le decalage)
        return dt.replace(tzinfo=timezone.utc).isoformat()
    return [
        {"title": "FOMC Minutes", "country": "USD", "impact": "High",
         "date": iso(now + timedelta(hours=14)), "forecast": "", "previous": ""},
        {"title": "Unemployment Claims", "country": "USD", "impact": "Medium",
         "date": iso(now + timedelta(hours=33)), "forecast": "230K", "previous": "228K"},
        {"title": "CPI y/y", "country": "USD", "impact": "High",
         "date": iso(now - timedelta(hours=5)), "forecast": "3.1%", "actual": "3.4%"},
        {"title": "Some Low Event", "country": "NZD", "impact": "Low",
         "date": iso(now + timedelta(hours=2)), "forecast": "", "previous": ""},
    ]


def test_calendrier_split_recent_upcoming_et_impact(monkeypatch):
    now = datetime.now(timezone.utc)
    cal = CW.WebCalendar()
    monkeypatch.setattr(CW.WebCalendar, "_fetch", lambda self: _events_bruts(now))
    recent, upcoming = cal.events(now, recent_hours=72, upcoming_hours=48, min_importance=2)
    # Low NZD filtre (impact 1 < 2) ; CPI passe en recent ; FOMC + Claims en upcoming
    assert [e["event"] for e in recent] == ["CPI y/y"]
    assert {e["event"] for e in upcoming} == {"FOMC Minutes", "Unemployment Claims"}
    fomc = next(e for e in upcoming if e["event"] == "FOMC Minutes")
    assert fomc["importance"] == 3 and 13 < fomc["hours_until"] < 15 and fomc["currency"] == "USD"


def test_calendrier_recent_calcule_la_surprise(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(CW.WebCalendar, "_fetch", lambda self: _events_bruts(now))
    recent, _ = CW.WebCalendar().events(now, 72, 48, 2)
    cpi = recent[0]
    assert cpi["actual"] == 3.4 and cpi["forecast"] == 3.1
    assert cpi["surprise"] == 0.3           # 3.4 - 3.1, % parses


def test_calendrier_vide_si_source_tombe(monkeypatch):
    monkeypatch.setattr(CW.WebCalendar, "_fetch", lambda self: [])
    assert CW.WebCalendar().events(datetime.now(timezone.utc), 72, 48, 2) == ([], [])


def test_parse_nombres_ff():
    assert CW._num("3.2%") == 3.2 and CW._num("250K") == 250000.0
    assert CW._num("1.2M") == 1_200_000.0 and CW._num("-0.4") == -0.4
    assert CW._num("") is None and CW._num("N/A") is None


def test_blackout_xauusd_declenche_par_usd(monkeypatch):
    # un event USD a fort impact dans <60min doit couvrir XAUUSD (USD dans [XAU,USD])
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    ev = [{"title": "FOMC", "country": "USD", "impact": "High",
           "date": (now + timedelta(minutes=30)).isoformat()}]
    monkeypatch.setattr(CW.WebCalendar, "_fetch", lambda self: ev)
    _, upcoming = CW.WebCalendar().events(now, 72, 48, 2)
    soon = [e for e in upcoming if e["currency"] in ("XAU", "USD") and e["hours_until"] <= 1.0]
    assert soon and soon[0]["event"] == "FOMC"


# =========================================================== SENTIMENT FXSSI
_HTML = ('<div class="line" data-avg="34.8"><div class="symbol">XAUUSD</div>'
         '<div class="ratio"><div class="ratio-bar-left" style="width: 65%;">65% </div>'
         '<div class="ratio-bar-right" style="width: 35%;">35%</div></div></div>'
         '<div class="line"><div class="symbol">EURUSD</div>'
         '<div class="ratio"><div class="ratio-bar-left" style="width: 36%;">36%</div></div></div>')


def _cfg(**kw):
    return replace(SourcesConfig(), **kw)


def test_fxssi_extrait_long_short(monkeypatch):
    monkeypatch.setattr(FXSSISource, "_get", lambda self, url, params=None, headers=None: _HTML)
    r = FXSSISource(_cfg(fxssi_enabled=True)).retail_sentiment("XAUUSD")
    assert r["long_pct"] == 65.0 and r["short_pct"] == 35.0
    assert r["foule_nette"] == 0.3          # foule nette longue -> signal contrarien baissier


def test_fxssi_symbole_absent_rend_vide(monkeypatch):
    monkeypatch.setattr(FXSSISource, "_get", lambda self, url, params=None, headers=None: _HTML)
    assert FXSSISource(_cfg(fxssi_enabled=True)).retail_sentiment("GBPJPY") == {}


def test_fxssi_desactive_rend_vide():
    assert FXSSISource(_cfg(fxssi_enabled=False)).retail_sentiment("XAUUSD") == {}


def test_fxssi_panne_rend_vide(monkeypatch):
    def boom(self, url, params=None, headers=None):
        raise RuntimeError("page down")
    monkeypatch.setattr(FXSSISource, "_get", boom)
    assert FXSSISource(_cfg(fxssi_enabled=True)).retail_sentiment("XAUUSD") == {}


def test_agregateur_retail_sentiment(monkeypatch):
    monkeypatch.setattr(FXSSISource, "_get", lambda self, url, params=None, headers=None: _HTML)
    out = Sources(_cfg(fxssi_enabled=True)).retail_sentiment("XAUUSD")
    assert out["long_pct"] == 65.0 and "fxssi" in out["source"]
