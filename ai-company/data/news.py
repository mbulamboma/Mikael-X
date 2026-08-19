# -*- coding: utf-8 -*-
"""Flux d'actualite pour le trader swing.

Agrege, avec degradation gracieuse (chaque source protegee), et met en cache :
  1. Calendrier economique MT5  -> `calendar_history.csv` (dossier MQL5\\Files).
     Donne les surprises recentes (actual-forecast) ET les events a venir a fort
     impact -> sert au "black-out" (ne pas entrer juste avant une grosse annonce).
  2. Reserve federale / taux    -> FRED (cle FRED_API) : Fed funds, 2 ans, 10 ans.
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

import requests

from config import NewsConfig
from store import default_store

log = logging.getLogger("news")
CACHE_KEY = "news_cache"          # etat persiste en SQLite (survit a un redemarrage)

CCYS = ["EUR", "USD", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF"]

def symbol_ccys(sym: str) -> list[str]:
    """Devises pertinentes pour un symbole. EURUSD -> [EUR, USD] ; XAUUSD -> [XAU, USD]."""
    s = sym.upper()
    if len(s) >= 6 and s[:6].isalpha():
        return [s[:3], s[3:6]]
    if any(k in s for k in ("US30", "NAS", "SPX", "US500", "NDX", "DJ")):
        return ["USD"]
    return [s]


class NewsFeed:
    def __init__(self, cfg: NewsConfig, store=None):
        self.cfg = cfg
        self.store = store or default_store()
        # Calendrier WEB (faireconomy/ForexFactory) : SEULE source du calendrier. Meme
        # donnee que la regle news FTMO (High impact = restriction). Aucun fichier local.
        self._web_cal = None
        if cfg.enabled:
            from data.calendar_web import WebCalendar
            # Le TTL du cache calendrier suit NEWS_CACHE_MIN : `get_macro_events`
            # court-circuite le cache de snapshot et taperait sinon le flux a
            # chaque appel d'outil (-> HTTP 429 -> black-out muet).
            self._web_cal = WebCalendar(cache_min=cfg.cache_min)
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
        fred = self._fred()
        macro = self._macro_features(recent, now)
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
                "blackout": blackout[sym],
            }
        return {"enabled": True, "as_of": now.isoformat(), "fred": fred,
                "per_symbol": per_symbol, "blackout": blackout}

    # ------------------------------------------------------------- calendrier (web)
    def _calendar(self, now: datetime) -> tuple[list, list]:
        """(recent, upcoming) depuis le calendrier web. Fail-closed et BRUYANT : une
        source vide ne doit jamais passer pour une protection active."""
        if self._web_cal is None:
            log.error("CALENDRIER ECONOMIQUE DESACTIVE — AUCUN BLACK-OUT NEWS ACTIF. "
                      "Mettre NEWS_FAIL_CLOSED=1 pour interdire toute entree sans calendrier.")
            self.calendar_ok = False
            return [], []
        recent, upcoming = self._web_calendar(now)
        # le calendrier ne fait autorite que s'il a REELLEMENT rendu des evenements.
        self.calendar_ok = bool(recent or upcoming)
        if not self.calendar_ok:
            log.error("CALENDRIER WEB VIDE — AUCUN BLACK-OUT NEWS ACTIF ce cycle. "
                      "Mettre NEWS_FAIL_CLOSED=1 pour interdire toute entree sans calendrier.")
        return recent, upcoming

    def _web_calendar(self, now: datetime) -> tuple[list, list]:
        """Calendrier web (faireconomy), meme sortie que le CSV. Fail-closed : ([],[]) si panne."""
        try:
            return self._web_cal.events(now, self.cfg.recent_hours,
                                        self.cfg.upcoming_hours, self.cfg.min_importance)
        except Exception as e:                    # une source web ne doit jamais casser un cycle
            log.warning("calendrier web indisponible (%s).", e)
            return [], []

    # ------------------------------------------------------------- biais macro
    def _macro_features(self, recent: list[dict], now: datetime) -> dict:
        """Biais macro par devise, calcule dans le cycle (cf. data/macro_web.py) a partir
        du momentum des taux (FRED) et des surprises du calendrier. Remplace l'ancien
        `macro_features.csv` (indicateur MT5 + service horaire + FinBERT)."""
        from data.macro_web import macro_bias
        return macro_bias(recent, self._fred_rates(), now)

    # Taux directeur / de reference par devise. Le choix privilegie la FRAICHEUR :
    # series QUOTIDIENNES pour USD/EUR/GBP, series mensuelles OCDE (10 ans) pour les
    # autres, faute de mieux gratuitement. `age_jours` dit au LLM ce qu'il lit.
    FRED_TAUX = {
        "USD": ("DGS2", "rendement 2 ans US"),
        "EUR": ("ECBDFR", "taux de depot BCE"),
        "GBP": ("IUDSOIA", "SONIA (taux au jour le jour GB)"),
        "JPY": ("IRLTLT01JPM156N", "10 ans Japon (mensuel)"),
        "CAD": ("IRLTLT01CAM156N", "10 ans Canada (mensuel)"),
        "AUD": ("IRLTLT01AUM156N", "10 ans Australie (mensuel)"),
        "NZD": ("IRLTLT01NZM156N", "10 ans Nouvelle-Zelande (mensuel)"),
        "CHF": ("IRLTLT01CHM156N", "10 ans Suisse (mensuel)"),
    }

    #: Moteurs du COURS DE L'OR. XAU n'a pas de taux directeur : son biais se construit
    #: sur trois series, avec un poids SIGNE et une echelle de normalisation calee sur
    #: l'amplitude d'un mouvement de 90 jours. Taux reels et dollar qui montent pesent
    #: sur l'or (poids negatif) ; l'inflation anticipee le soutient (poids positif).
    FRED_OR = {
        "DFII10":   (-0.5, 2.0, "taux reel 10 ans US (TIPS)"),
        "DTWEXBGS": (-0.3, 0.2, "dollar index large"),
        "T10YIE":   (+0.2, 3.0, "point mort d'inflation 10 ans"),
    }

    def _fred_obs(self, sid: str, limit: int = 200) -> list:
        """Observations FRED d'une serie (recentes d'abord). [] si indisponible."""
        try:
            r = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": sid, "api_key": self.cfg.fred_key,
                        "file_type": "json", "sort_order": "desc", "limit": limit},
                timeout=15)
            return [o for o in r.json().get("observations", [])
                    if o.get("value") not in (".", "", None)]
        except Exception as e:
            log.info("FRED %s indispo: %s", sid, e)
            return []

    def _fred_lot(self, ids: list[str]) -> dict:
        """Plusieurs series EN PARALLELE. En sequence, 14 series coutaient ~25 s a chaque
        reconstruction du snapshot — sur le chemin critique d'un cycle."""
        from concurrent.futures import ThreadPoolExecutor
        ids = list(dict.fromkeys(ids))
        with ThreadPoolExecutor(max_workers=min(6, len(ids) or 1)) as ex:
            return dict(zip(ids, ex.map(self._fred_obs, ids)))

    @staticmethod
    def _delta_90j(obs: list) -> tuple[float, dict] | None:
        """(variation sur ~90 jours, derniere observation). None si illisible."""
        if len(obs) < 2:
            return None
        dernier = obs[0]
        try:
            d_last = datetime.fromisoformat(dernier["date"]).replace(tzinfo=timezone.utc)
            cible = d_last - timedelta(days=90)
            ref = next((o for o in obs
                        if datetime.fromisoformat(o["date"]).replace(tzinfo=timezone.utc) <= cible),
                       obs[-1])
            return float(dernier["value"]) - float(ref["value"]), dernier
        except (TypeError, ValueError, KeyError):
            return None

    def _fred_rates(self) -> dict:
        """Momentum des taux par devise sur ~90 jours, borne dans [-1, +1] (tanh), PLUS le
        biais de l'or (FRED_OR). Des taux qui montent soutiennent une devise ; l'or, lui,
        suit l'inverse des taux REELS. Chaque serie est protegee separement — une devise
        indisponible manque, elle ne casse pas les autres."""
        if not self.cfg.fred_key:
            return {}
        import math
        lot = self._fred_lot([sid for sid, _ in self.FRED_TAUX.values()] + list(self.FRED_OR))
        out = {}
        for ccy, (sid, libelle) in self.FRED_TAUX.items():
            calc = self._delta_90j(lot.get(sid) or [])
            if calc is None:
                continue
            delta, dernier = calc
            d_last = datetime.fromisoformat(dernier["date"]).replace(tzinfo=timezone.utc)
            out[ccy] = {
                "momentum": round(math.tanh(delta), 3),   # +-1 pt de taux ~ +-0.76
                "libelle": libelle,
                "variation_points": round(delta, 3),
                "dernier": float(dernier["value"]),
                "date": dernier["date"],
                "age_jours": (datetime.now(timezone.utc) - d_last).days,
            }
        or_ = self._biais_or(lot)
        if or_:
            out["XAU"] = or_
        return out

    def _biais_or(self, lot: dict) -> dict:
        """Biais de l'or : somme ponderee et bornee des trois moteurs de FRED_OR.
        Rend {} si aucune des trois series n'est lisible (on n'invente pas un neutre)."""
        import math
        total, poids_vus, detail, ages = 0.0, 0.0, {}, []
        for sid, (poids, echelle, libelle) in self.FRED_OR.items():
            calc = self._delta_90j(lot.get(sid) or [])
            if calc is None:
                continue
            delta, dernier = calc
            contrib = poids * math.tanh(delta * echelle)
            total += contrib
            poids_vus += abs(poids)
            detail[libelle] = {"variation_90j": round(delta, 3),
                               "dernier": float(dernier["value"]),
                               "effet_sur_l_or": round(contrib, 3)}
            try:
                d = datetime.fromisoformat(dernier["date"]).replace(tzinfo=timezone.utc)
                ages.append((datetime.now(timezone.utc) - d).days)
            except (TypeError, ValueError):
                pass
        if not detail:
            return {}
        return {"momentum": round(max(-1.0, min(1.0, total / max(poids_vus, 1e-9))), 3),
                "libelle": "or : taux reels + dollar + inflation anticipee (FRED)",
                "detail": detail,
                "age_jours": max(ages) if ages else None}

    def _fred(self) -> dict:
        """Photo des taux US pour le dossier : directeur, 2 ans, 10 ans."""
        if not self.cfg.fred_key:
            return {}
        series = {"fed_funds": "DFF", "yield_2y": "DGS2", "yield_10y": "DGS10"}
        lot = self._fred_lot(list(series.values()))
        out = {}
        for label, sid in series.items():
            obs = [float(o["value"]) for o in (lot.get(sid) or [])[:10]]
            if obs:
                out[label] = {"last": obs[0], "prev": obs[-1],
                              "momentum": round(obs[0] - obs[-1], 3)}
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
