# -*- coding: utf-8 -*-
"""CALENDRIER ECONOMIQUE WEB — remplace l'export MT5 (ExportCalendar.mq5) supprime.

Le black-out news FTMO (pas d'entree +/- N minutes autour d'une annonce a fort impact) a
besoin d'un calendrier. Il venait d'un indicateur MT5 qui ecrivait `calendar_history.csv` ;
cet indicateur n'existe plus. Ce module fournit la MEME matiere depuis le web, sans MT5.

SOURCE : le flux JSON de faireconomy (miroir du calendrier ForexFactory), libre et sans cle.
C'est exactement le calendrier sur lequel la regle news de FTMO est basee (High impact =
les annonces qui declenchent la restriction). On mappe :
    impact High -> 3, Medium -> 2, Low -> 1, Holiday -> ignore.

SORTIE : le couple `(recent, upcoming)` avec EXACTEMENT les champs qu'attend data/news.py, si
bien que le reste (black-out, surprises, agenda) fonctionne sans changement.

ROBUSTESSE : fail-closed. Toute panne (reseau, format) rend `([], [])` et laisse `news.py`
decider (log bruyant + option NEWS_FAIL_CLOSED=1 pour interdire d'entrer sans calendrier). On
recupere la semaine courante ET la suivante pour couvrir l'horizon `upcoming_hours`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger("data.calendar_web")

THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEXT_WEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_IMPACT = {"high": 3, "medium": 2, "low": 1}


def _num(x) -> Optional[float]:
    """Parse une valeur ForexFactory ('3.2%', '250K', '1.2M', '-0.4', '') en float. None si vide."""
    if x is None:
        return None
    s = str(x).strip().replace(",", "").replace("%", "")
    if not s or s in ("-", "N/A"):
        return None
    mult = 1.0
    if s and s[-1] in "KkMmBb":
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}[s[-1].lower()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _to_utc_naive(iso: str) -> Optional[datetime]:
    """ISO avec decalage ('2026-08-16T18:30:00-04:00') -> UTC naive (convention news.py)."""
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class WebCalendar:
    """Calendrier economique depuis faireconomy (ForexFactory). Interface alignee sur
    data/news.NewsFeed._calendar : `.events(now, ...) -> (recent, upcoming)`."""

    def __init__(self, timeout: int = 12, urls: tuple[str, ...] = (THIS_WEEK, NEXT_WEEK)):
        self.timeout = timeout
        self.urls = urls

    def _fetch(self) -> list[dict]:
        events: list[dict] = []
        for url in self.urls:
            try:
                r = requests.get(url, headers={"User-Agent": _UA}, timeout=self.timeout)
                data = r.json() if r.text.strip().startswith("[") else []
                if isinstance(data, list):
                    events += data
            except Exception as e:                # fail-closed : une semaine qui tombe n'annule pas l'autre
                log.info("calendrier web %s indisponible (%s).", url, e)
        return events

    def events(self, now: datetime, recent_hours: int, upcoming_hours: int,
               min_importance: int = 2) -> tuple[list[dict], list[dict]]:
        """(recent, upcoming) au format news.py. `now` peut etre aware ou naive UTC."""
        raw = self._fetch()
        if not raw:
            return [], []
        now_naive = now.replace(tzinfo=None) if now.tzinfo else now
        from datetime import timedelta
        lo = now_naive - timedelta(hours=recent_hours)
        hi = now_naive + timedelta(hours=upcoming_hours)
        recent: list[dict] = []
        upcoming: list[dict] = []
        for e in raw:
            imp = _IMPACT.get(str(e.get("impact", "")).strip().lower(), 0)
            if imp < min_importance:
                continue
            t = _to_utc_naive(e.get("date"))
            if t is None:
                continue
            ccy = str(e.get("country", "")).strip().upper()
            name = str(e.get("title", "")).strip() or "event"
            if lo < t <= now_naive:
                act, fc = _num(e.get("actual")), _num(e.get("forecast"))
                item = {"currency": ccy, "event": name, "importance": imp, "when": t.isoformat()}
                if act is not None and fc is not None:
                    item.update({"actual": act, "forecast": fc,
                                 "surprise": round(act - fc, 4)})
                recent.append(item)
            elif now_naive < t <= hi:
                upcoming.append({"currency": ccy, "event": name, "importance": imp,
                                 "when": t.isoformat(),
                                 "hours_until": round((t - now_naive).total_seconds() / 3600, 2)})
        recent.sort(key=lambda x: x["when"], reverse=True)
        upcoming.sort(key=lambda x: x["hours_until"])
        return recent, upcoming
