# -*- coding: utf-8 -*-
"""Flux d'actualite pour le trader swing.

Agrege, avec degradation gracieuse (chaque source protegee), et met en cache :
  1. Calendrier economique MT5  -> `calendar_history.csv` (dossier MQL5\\Files).
     Donne les surprises recentes (actual-forecast) ET les events a venir a fort
     impact -> sert au "black-out" (ne pas entrer juste avant une grosse annonce).
  2. Reserve federale / taux    -> FRED (cle FRED_API) : Fed funds, 2 ans, 10 ans.
  3. Actualites generales        -> GDELT (gratuit, sans cle) : titres 48h/devise,
     dont la sentiment est jugee par le LLM lui-meme.
  4. Brain macro par devise      -> `macro_features.csv` (tools/macro_service.py),
     s'il est present : score composite calendrier+taux+news deja calcule.

ANTI-LOOKAHEAD : on ne lit que du deja-publie ; les events "a venir" servent
uniquement de garde-fou (black-out), jamais de signal directionnel triche.
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from config import NewsConfig
from store import default_store

log = logging.getLogger("news")
CACHE_KEY = "news_cache"          # etat persiste en SQLite (survit a un redemarrage)

CCYS = ["EUR", "USD", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF"]

# requetes GDELT par devise (banques centrales + devise) — reutilise l'univers macro
GDELT_Q = {
    "EUR": '(ECB OR "European Central Bank" OR eurozone)',
    "USD": '("Federal Reserve" OR "US inflation" OR "US economy")',
    "JPY": '("Bank of Japan" OR "japanese yen")',
    "GBP": '("Bank of England" OR "pound sterling")',
    "AUD": '("Reserve Bank of Australia" OR "australian dollar")',
    "NZD": '(RBNZ OR "new zealand dollar")',
    "CAD": '("Bank of Canada" OR "canadian dollar")',
    "CHF": '("Swiss National Bank" OR "swiss franc")',
    "XAU": '(gold OR "gold price" OR bullion)',
}


def _last_sunday(year: int, month: int) -> datetime:
    """Dernier dimanche du mois (bascule d'heure europeenne, a 01:00 UTC)."""
    d = datetime(year, month, 31, 1, tzinfo=timezone.utc)
    while d.month != month:
        d -= timedelta(days=1)
    while d.weekday() != 6:                    # 6 = dimanche
        d -= timedelta(days=1)
    return d


def _eet_offset(t) -> int:
    """Decalage du serveur MT5 (EET/EEST) par rapport a UTC : +3 en heure d'ete
    (dernier dimanche de mars -> dernier dimanche d'octobre), +2 sinon."""
    try:
        ts = pd.Timestamp(t).to_pydatetime()
    except Exception:
        return 2
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return 3 if _last_sunday(ts.year, 3) <= ts < _last_sunday(ts.year, 10) else 2


def symbol_ccys(sym: str) -> list[str]:
    """Devises pertinentes pour un symbole. EURUSD -> [EUR, USD] ; XAUUSD -> [XAU, USD]."""
    s = sym.upper()
    if len(s) >= 6 and s[:6].isalpha():
        return [s[:3], s[3:6]]
    if any(k in s for k in ("US30", "NAS", "SPX", "US500", "NDX", "DJ")):
        return ["USD"]
    return [s]


def _resolve_mt5_files(cfg: NewsConfig) -> Path | None:
    """Dossier MQL5\\Files du terminal (idem logique macro_service.py)."""
    import os
    if cfg.mt5_files and Path(cfg.mt5_files).is_dir():
        return Path(cfg.mt5_files)
    base = Path(os.environ.get("APPDATA", "")) / "MetaQuotes" / "Terminal"
    if not base.is_dir():
        return None
    cands = [p for p in base.glob("*/MQL5/Files") if p.is_dir()]
    if not cands:
        return None
    # Marqueur du terminal "de travail" : les fichiers que produisent
    # tools/ExportCalendar.mq5 et tools/macro_service.py. Le repli
    # MIKAEL_* couvre les installations ou un ancien EA tourne encore.
    def _connu(p: Path) -> bool:
        return (any((p / f).exists()
                    for f in ("calendar_history.csv", "macro_features.csv"))
                or any(p.glob("MIKAEL_*")))
    return max(cands, key=lambda p: (_connu(p), p.stat().st_mtime))


class NewsFeed:
    def __init__(self, cfg: NewsConfig, store=None):
        self.cfg = cfg
        self.store = store or default_store()
        self.files = _resolve_mt5_files(cfg) if cfg.enabled else None
        # Calendrier WEB (faireconomy/ForexFactory) : remplace l'export MT5 supprime. Utilise
        # quand aucun calendar_history.csv n'est present. Meme donnee que la regle news FTMO.
        self._web_cal = None
        if cfg.enabled and getattr(cfg, "use_web_calendar", True):
            from data.calendar_web import WebCalendar
            self._web_cal = WebCalendar()
        # False des qu'un cycle constate l'absence de calendrier -> le black-out ne
        # protege plus rien (l'orchestrateur peut alors interdire les entrees).
        self.calendar_ok = True

    # ------------------------------------------------------------- cache
    def snapshot(self, symbols: list[str]) -> dict:
        """Renvoie le snapshot news (cache TTL). Vide si news desactivees."""
        if not self.cfg.enabled:
            return {"enabled": False, "per_symbol": {}, "blackout": {}}
        cached = self._read_cache()
        if cached is not None:
            return cached
        snap = self._build(symbols)
        self._write_cache(snap)
        return snap

    def for_symbols(self, symbols: list[str]) -> dict:
        """Snapshot news pour des symboles ARBITRAIRES (l'agent choisit ses paires).

        Reutilise le cache du cycle et ne construit que ce qui manque, puis fusionne
        le resultat dans le cache -> le black-out reste disponible a l'execution.
        """
        if not self.cfg.enabled:
            return {"enabled": False, "per_symbol": {}, "blackout": {}}
        base = self._read_cache() or {"enabled": True, "per_symbol": {}, "blackout": {}, "fred": {}}
        missing = [s for s in symbols if s not in base.get("per_symbol", {})]
        if missing:
            fresh = self._build(missing)
            base.setdefault("per_symbol", {}).update(fresh.get("per_symbol", {}))
            base.setdefault("blackout", {}).update(fresh.get("blackout", {}))
            base["fred"] = base.get("fred") or fresh.get("fred", {})
            base["enabled"] = True
            self._write_cache(base, ts=base.get("_ts"))
        return base

    def blackout_for(self, symbol: str) -> dict:
        """Etat du black-out d'un symbole, meme hors univers de scan."""
        if not self.cfg.enabled:
            return {"active": False, "reason": "", "in_hours": None}
        snap = self.for_symbols([symbol])
        return snap.get("blackout", {}).get(symbol, {"active": False, "reason": "", "in_hours": None})

    # ------------------------------------------------------------- grands evenements
    def major_events(self, hours: int | None = None) -> dict:
        """Les GROS evenements macro (toutes devises) : surprises recentes + agenda a venir.
        Sert a l'agent pour anticiper ce qui peut bouger l'ensemble des marches."""
        if not self.cfg.enabled:
            return {"enabled": False}
        now = datetime.now(timezone.utc)
        recent, upcoming = self._calendar(now)
        horizon = hours if hours else self.cfg.upcoming_hours
        strong_recent = [e for e in recent if e["importance"] >= 3][:12]
        strong_next = [e for e in upcoming if e["importance"] >= 3 and e["hours_until"] <= horizon][:12]
        cached = self._read_cache() or {}                  # evite de re-interroger FRED
        return {"enabled": True, "as_of": now.isoformat(), "horizon_h": horizon,
                "surprises_recentes": strong_recent, "agenda_fort_impact": strong_next,
                "taux_fred": cached.get("fred") or self._fred()}

    def search(self, query: str, hours: int = 48, limit: int = 8) -> dict:
        """Recherche libre d'actualite (GDELT) : l'agent enquete sur un theme/actif
        (ex "gold", "ECB rate decision", "oil supply"). Titres bruts, a lui de juger."""
        if not (self.cfg.enabled and self.cfg.use_gdelt):
            return {"enabled": False, "articles": []}
        try:
            r = requests.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={"query": str(query)[:200], "mode": "ArtList",
                        "maxrecords": max(1, min(int(limit), 20)), "format": "json",
                        "timespan": f"{max(1, min(int(hours), 168))}h",
                        "sourcelang": "english"}, timeout=15)
            arts = r.json().get("articles", []) if r.text.strip().startswith("{") else []
        except Exception as e:
            log.info("GDELT recherche '%s' indispo: %s", query, e)
            return {"enabled": True, "articles": [], "error": str(e)}
        return {"enabled": True, "query": query, "hours": hours,
                "articles": [{"titre": a.get("title", "").strip(),
                              "source": a.get("domain", ""), "date": a.get("seendate", "")}
                             for a in arts if a.get("title")]}

    def _read_cache(self) -> dict | None:
        data = self.store.kv_get(CACHE_KEY)
        if isinstance(data, dict) and time.time() - data.get("_ts", 0) < self.cfg.cache_min * 60:
            return data
        return None

    def _write_cache(self, snap: dict, ts: float | None = None):
        snap["_ts"] = ts or time.time()          # ts fourni = on ne rallonge pas le TTL
        self.store.kv_set(CACHE_KEY, snap)

    # ------------------------------------------------------------- construction
    def _build(self, symbols: list[str]) -> dict:
        now = datetime.now(timezone.utc)
        recent, upcoming = self._calendar(now)
        macro = self._macro_features()
        fred = self._fred()
        wanted = sorted({c for sym in symbols for c in symbol_ccys(sym)})
        headlines = self._gdelt(wanted) if self.cfg.use_gdelt else {}

        per_symbol, blackout = {}, {}
        for sym in symbols:
            ccys = symbol_ccys(sym)
            ev_soon = [e for e in upcoming if e["currency"] in ccys
                       and e["hours_until"] <= self.cfg.blackout_min / 60.0]
            bo = bool(ev_soon)
            blackout[sym] = {"active": bo,
                             "reason": ev_soon[0]["event"] if bo else "",
                             "in_hours": round(ev_soon[0]["hours_until"], 1) if bo else None}
            per_symbol[sym] = {
                "currencies": ccys,
                "macro_bias": {c: macro.get(c) for c in ccys if c in macro},
                "recent_events": [e for e in recent if e["currency"] in ccys][:5],
                "upcoming_events": [e for e in upcoming if e["currency"] in ccys][:5],
                "headlines": {c: headlines.get(c, []) for c in ccys if c in headlines},
                "blackout": blackout[sym],
            }
        return {"enabled": True, "as_of": now.isoformat(), "fred": fred,
                "per_symbol": per_symbol, "blackout": blackout}

    # ------------------------------------------------------------- calendrier MT5
    def _calendar(self, now: datetime) -> tuple[list, list]:
        recent: list[dict] = []
        upcoming: list[dict] = []
        # 1) CSV MT5 s'il existe encore (compat) ; 2) sinon calendrier WEB (faireconomy).
        path = self.files / "calendar_history.csv" if self.files else None
        if path is None or not path.exists():
            if self._web_cal is not None:
                r, u = self._web_calendar(now)
                # le web fait autorite seulement s'il a REELLEMENT rendu des evenements :
                # une source vide ne doit pas faire croire a une protection active.
                self.calendar_ok = bool(r or u)
                if not self.calendar_ok:
                    log.error("CALENDRIER WEB VIDE — AUCUN BLACK-OUT NEWS ACTIF ce cycle. "
                              "Mettre NEWS_FAIL_CLOSED=1 pour interdire toute entree sans calendrier.")
                return r, u
            # ni CSV ni calendrier web : danger, on le crie fort.
            log.error("CALENDRIER ECONOMIQUE ABSENT — AUCUN BLACK-OUT NEWS ACTIF. "
                      "Activer le calendrier web (NEWS_WEB_CALENDAR=1) ou mettre "
                      "NEWS_FAIL_CLOSED=1 pour interdire toute entree sans calendrier.")
            self.calendar_ok = False
            return recent, upcoming
        self.calendar_ok = True
        try:
            df = pd.read_csv(path, sep=";", encoding="cp1252", engine="python",
                             on_bad_lines="skip")
        except Exception as e:
            log.warning("lecture calendrier impossible: %s", e)
            return recent, upcoming
        cols = {c.lower(): c for c in df.columns}
        c_time = cols.get("time"); c_ccy = cols.get("currency")
        c_imp = cols.get("importance"); c_act = cols.get("actual")
        c_fc = cols.get("forecast")
        c_ev = cols.get("event") or cols.get("event_id")
        if not (c_time and c_ccy and c_imp):
            return recent, upcoming
        df[c_imp] = pd.to_numeric(df[c_imp], errors="coerce")
        df["_t"] = pd.to_datetime(df[c_time], format="%Y.%m.%d %H:%M", errors="coerce")
        df = df.dropna(subset=["_t", c_imp])
        # Heure serveur MT5 = EET l'hiver (UTC+2), EEST l'ete (UTC+3). Un decalage d'une
        # heure ferait rater une fenetre de black-out : on applique le VRAI decalage.
        df["_utc"] = df["_t"] - df["_t"].map(lambda t: pd.Timedelta(hours=_eet_offset(t)))
        now_naive = now.replace(tzinfo=None)
        lo = now_naive - timedelta(hours=self.cfg.recent_hours)
        hi = now_naive + timedelta(hours=self.cfg.upcoming_hours)
        for _, r in df.iterrows():
            imp = int(r[c_imp])
            if imp < self.cfg.min_importance:
                continue
            t = r["_utc"]
            ccy = str(r[c_ccy]).strip().upper()
            name = str(r[c_ev]).strip() if c_ev else "event"
            act = pd.to_numeric(r.get(c_act), errors="coerce") if c_act else None
            fc = pd.to_numeric(r.get(c_fc), errors="coerce") if c_fc else None
            if lo < t <= now_naive and pd.notna(act) and pd.notna(fc):
                recent.append({"currency": ccy, "event": name, "importance": imp,
                               "actual": float(act), "forecast": float(fc),
                               "surprise": round(float(act) - float(fc), 4),
                               "when": t.isoformat()})
            elif now_naive < t <= hi:
                upcoming.append({"currency": ccy, "event": name, "importance": imp,
                                 "when": t.isoformat(),
                                 "hours_until": round((t - now_naive).total_seconds() / 3600, 2)})
        recent.sort(key=lambda e: e["when"], reverse=True)
        upcoming.sort(key=lambda e: e["hours_until"])
        return recent, upcoming

    def _web_calendar(self, now: datetime) -> tuple[list, list]:
        """Calendrier web (faireconomy), meme sortie que le CSV. Fail-closed : ([],[]) si panne."""
        try:
            return self._web_cal.events(now, self.cfg.recent_hours,
                                        self.cfg.upcoming_hours, self.cfg.min_importance)
        except Exception as e:                    # une source web ne doit jamais casser un cycle
            log.warning("calendrier web indisponible (%s).", e)
            return [], []

    # ------------------------------------------------------------- brain macro
    def _macro_features(self) -> dict:
        if self.files is None:
            return {}
        path = self.files / "macro_features.csv"
        if not path.exists():
            return {}
        try:
            df = pd.read_csv(path)
        except Exception:
            return {}
        # trouve la colonne devise
        ccy_col = None
        for c in df.columns:
            vals = df[c].astype(str).str.upper()
            if vals.isin(CCYS).any():
                ccy_col = c
                break
        if ccy_col is None:
            return {}
        out = {}
        for _, r in df.iterrows():
            c = str(r[ccy_col]).strip().upper()
            if c in CCYS:
                out[c] = {k: (round(float(v), 3) if isinstance(v, (int, float)) else v)
                          for k, v in r.items() if k != ccy_col}
        return out

    # ------------------------------------------------------------- FRED (Fed)
    def _fred(self) -> dict:
        key = self.cfg.fred_key
        if not key:
            return {}
        series = {"fed_funds": "DFF", "yield_2y": "DGS2", "yield_10y": "DGS10"}
        out = {}
        for label, sid in series.items():
            try:
                r = requests.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={"series_id": sid, "api_key": key, "file_type": "json",
                            "sort_order": "desc", "limit": 10}, timeout=15)
                obs = [float(o["value"]) for o in r.json().get("observations", [])
                       if o["value"] not in (".", "")]
                if obs:
                    out[label] = {"last": obs[0], "prev": obs[-1],
                                  "momentum": round(obs[0] - obs[-1], 3)}
            except Exception as e:
                log.info("FRED %s indispo: %s", sid, e)
        return out

    def fred_series(self, series_id: str, limit: int = 12) -> dict:
        """N'IMPORTE QUELLE serie FRED, au choix de l'agent (analyse macro d'expert) :
        CPIAUCSL (inflation US), UNRATE (chomage), GDPC1 (PIB), DTWEXBGS (dollar index),
        T10Y2Y (pente 10a-2a), DGS10, VIXCLS, DCOILWTICO... Renvoie les dernieres
        observations + la variation."""
        key = self.cfg.fred_key
        if not key:
            return {"error": "cle FRED_API absente du .env"}
        sid = str(series_id).strip().upper()[:32]
        n = max(2, min(int(limit), 60))
        try:
            r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                             params={"series_id": sid, "api_key": key, "file_type": "json",
                                     "sort_order": "desc", "limit": n}, timeout=15)
            obs = [{"date": o["date"], "valeur": float(o["value"])}
                   for o in r.json().get("observations", []) if o["value"] not in (".", "")]
        except Exception as e:
            return {"error": f"FRED {sid} indisponible: {e}"}
        if not obs:
            return {"error": f"aucune donnee pour la serie {sid} (verifie l'identifiant)"}
        return {"serie": sid, "dernier": obs[0], "observations": obs,
                "variation": round(obs[0]["valeur"] - obs[-1]["valeur"], 4),
                "sur_n_observations": len(obs)}

    # ------------------------------------------------------------- GDELT news
    def _gdelt(self, ccys: list[str]) -> dict:
        out = {}
        for c in ccys:
            q = GDELT_Q.get(c)
            if not q:
                continue
            try:
                r = requests.get(
                    "https://api.gdeltproject.org/api/v2/doc/doc",
                    params={"query": q, "mode": "ArtList", "maxrecords": 5,
                            "format": "json", "timespan": "48h",
                            "sourcelang": "english"}, timeout=15)
                arts = r.json().get("articles", []) if r.text.strip().startswith("{") else []
                titles = [a.get("title", "").strip() for a in arts if a.get("title")]
                if titles:
                    out[c] = titles[:5]
            except Exception as e:
                log.info("GDELT %s indispo: %s", c, e)
        return out
