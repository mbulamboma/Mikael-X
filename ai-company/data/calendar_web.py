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
decider (log bruyant + option NEWS_FAIL_CLOSED=1 pour interdire d'entrer sans calendrier).

HORIZON — LIMITE CONNUE ET ASSUMEE : faireconomy ne publie QUE la semaine courante.
`ff_calendar_nextweek.json`, `_lastweek`, `_thismonth` repondent tous 404 (verifie le
2026-08-19). Le BLACK-OUT (+/- NEWS_BLACKOUT_MIN, 60 min par defaut) n'en souffre pas :
quand une annonce entre dans cette fenetre, elle est forcement dans la semaine en cours.
En revanche l'AGENDA `upcoming_hours` se tronque en fin de semaine — un jeudi, on ne voit
pas le lundi suivant. Ne pas "reparer" ca en inventant une URL : il n'y en a pas.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger("data.calendar_web")

THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
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

    def __init__(self, timeout: int = 12, urls: tuple[str, ...] = (THIS_WEEK,),
                 cache_min: int = 30, peremption_min: int = 360):
        self.timeout = timeout
        self.urls = urls
        self.cache_min = cache_min              # fenetre pendant laquelle on ne re-interroge pas
        self.peremption_min = peremption_min    # au-dela, un cache n'est plus une protection
        self._cache: list[dict] = []
        self._cache_ts: float = 0.0

    def _fetch(self) -> list[dict]:
        """Evenements bruts, avec CACHE. Le cache n'est pas une optimisation : sans lui,
        `get_macro_events` (outil que les analystes tool-capables appellent en boucle)
        martele le flux, qui repond HTTP 429 — et le black-out news tombe pour la seule
        raison qu'on a trop demande. Sur 429 ou panne, on REJOUE le dernier bon jeu tant
        qu'il n'est pas perime : un calendrier d'il y a une heure protege ; rien du tout
        ne protege pas. Passe la peremption, on rend vide et news.py crie."""
        age_min = (time.time() - self._cache_ts) / 60.0
        if self._cache and age_min < self.cache_min:
            return self._cache

        events: list[dict] = []
        panne = ""
        for url in self.urls:
            try:
                r = requests.get(url, headers={"User-Agent": _UA}, timeout=self.timeout)
                corps = r.text.strip()
                if not corps.startswith("["):
                    # Reponse non-JSON : rate limit, page d'erreur, blocage, portail captif.
                    # SURTOUT NE PAS l'avaler en silence : le seul symptome serait un
                    # black-out news vide, c'est-a-dire un garde-fou FTMO qui ne protege
                    # plus rien sans dire pourquoi. On dit le code et le debut du corps.
                    panne = f"HTTP {r.status_code} ({r.headers.get('Content-Type', '?')})"
                    log.error("calendrier web %s : reponse non-JSON (%s) — debut: %.80s",
                              url, panne, corps)
                    continue
                data = r.json()
                if isinstance(data, list):
                    if not data:
                        log.warning("calendrier web %s : JSON valide mais AUCUN evenement.", url)
                    events += data
            except Exception as e:            # fail-closed : une source qui tombe n'annule pas l'autre
                panne = str(e)
                log.error("calendrier web %s indisponible (%s).", url, e)

        if events:
            self._cache, self._cache_ts = events, time.time()
            return events
        if self._cache and age_min < self.peremption_min:
            log.warning("calendrier web en panne (%s) — REJEU du cache (%.0f min). "
                        "Le black-out reste actif sur ces donnees.", panne or "vide", age_min)
            return self._cache
        return []

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
