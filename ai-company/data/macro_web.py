# -*- coding: utf-8 -*-
"""BIAIS MACRO PAR DEVISE — sans MT5, sans fichier, sans modele local.

Remplace `macro_features.csv` (produit par l'ancien `tools/macro_service.py`, lui-meme
nourri par l'indicateur `ExportCalendar.mq5`). Ce couple imposait : un terminal MT5
allume, un indicateur charge sur un graphique, un service horaire, et torch+transformers
pour FinBERT. Le tout pouvait rester silencieusement perime — c'est ce qui est arrive.

Ici, le biais macro est une FONCTION PURE de ce que le cycle a deja en main :
  1. le MOMENTUM DES TAUX par devise (FRED, cle deja presente) : des taux qui montent
     soutiennent une devise. C'est le socle, car c'est la seule matiere macro fraiche
     et gratuite disponible pour les 8 majeures ;
  2. les SURPRISES du calendrier (actual vs forecast) quand la source en fournit.
Aucun etat sur disque, donc rien qui puisse perimer en silence.

SUR LES SURPRISES, SOYONS PRECIS : le flux faireconomy (le calendrier du black-out) ne
publie QUE `forecast` et `previous`, jamais `actual` — il ne permet donc aucune surprise.
Les calendriers qui donnent l'actual sont payants (Finnhub, EODHD : 403 sur nos cles ; le
compte guest de Trading Economics est ferme). Le calcul de surprise reste implemente et
teste : il s'allume tout seul le jour ou une source d'`actual` est branchee, et vaut 0
d'ici la — plutot que de faire semblant.

CE QU'ON PERD, ET C'EST ASSUME : l'ancienne normalisation z-score par `event_id` avait
besoin de l'historique complet du calendrier (>=8 observations du meme indicateur, ~9 Mo).
Le flux web ne porte que deux semaines. On normalise donc la surprise en RELATIF
(`(actual - forecast) / max(|forecast|, |actual|)`), borne a [-1, +1]. Echelle moins fine,
mais du meme signe et disponible partout — et surtout jamais perimee. `couverture` et
`fiabilite` disent au LLM sur combien d'evenements le score repose : une valeur assise sur
un seul chiffre ne doit pas etre lue comme une conviction.
"""
from __future__ import annotations

import logging

log = logging.getLogger("data.macro_web")

#: XAU y figure : l'or n'a pas de taux directeur, mais il a des moteurs macro chiffres
#: (taux reels, dollar, inflation anticipee) — cf. NewsFeed.FRED_OR.
CCYS = ("EUR", "USD", "JPY", "GBP", "AUD", "NZD", "CAD", "CHF", "XAU")

# Indicateurs ou un chiffre PLUS HAUT est une MAUVAISE nouvelle pour la devise : la
# surprise brute doit etre retournee. Liste volontairement courte et sans ambiguite —
# mieux vaut ne rien inverser que d'inverser a tort (l'ancien service, lui, n'inversait
# jamais rien : toute hausse du chomage comptait comme une bonne surprise).
INVERSES = (
    "unemployment rate", "unemployment change", "unemployment claims",
    "jobless claims", "continuing claims", "claimant count",
)

SEUIL = 0.15          # au-dela : biais qualifie de positif / negatif


def _relative(actual: float, forecast: float) -> float:
    """Surprise relative bornee a [-1, +1]. La base prend le max des deux valeurs pour
    rester finie quand la prevision vaut 0 (un CPI prevu a 0.0 % sorti a 0.3 %)."""
    base = max(abs(float(forecast)), abs(float(actual)), 1e-9)
    return max(-1.0, min(1.0, (float(actual) - float(forecast)) / base))


def _inverse(nom: str) -> bool:
    n = str(nom).lower()
    return any(k in n for k in INVERSES)


def _fenetre(events: list[dict], ccy: str, heures: float) -> tuple[float, int]:
    """(surprise moyenne ponderee par l'importance, nb d'evenements) sur `heures`."""
    num = den = 0.0
    n = 0
    for e in events:
        if e.get("currency") != ccy:
            continue
        if e.get("actual") is None or e.get("forecast") is None:
            continue                     # evenement sans chiffre : aucune surprise a lire
        age = e.get("hours_ago")
        if age is not None and age > heures:
            continue
        imp = float(e.get("importance") or 1)
        s = _relative(e["actual"], e["forecast"])
        if _inverse(e.get("event", "")):
            s = -s
        num += s * imp
        den += imp
        n += 1
    return (round(num / den, 3) if den else 0.0), n


def _heures_ecoulees(events: list[dict], now) -> list[dict]:
    """Ajoute `hours_ago` a partir de `when` (UTC naive, format de calendar_web)."""
    from datetime import datetime
    now_naive = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
    out = []
    for e in events:
        e = dict(e)
        try:
            t = datetime.fromisoformat(str(e.get("when")))
            e["hours_ago"] = max(0.0, (now_naive - t).total_seconds() / 3600.0)
        except (TypeError, ValueError):
            e["hours_ago"] = None
        out.append(e)
    return out


def macro_bias(recent: list[dict], taux: dict | None, now, ccys=CCYS) -> dict:
    """Biais macro par devise.

    `recent` = evenements passes de `calendar_web.events()` (surprises si `actual` present).
    `taux`   = momentum des taux par devise, fourni par `NewsFeed._fred_rates()` :
               {"EUR": {"momentum": 0.12, "libelle": ..., "age_jours": 1}, ...}.
    Rend par devise : surprise24/72, couverture, `biais` composite [-1, +1], sa `lecture`
    en clair et sa `fiabilite`. Ne leve jamais : une macro absente vaut mieux qu'un cycle
    casse, mais une devise sans matiere est OMISE plutot que rendue faussement neutre.
    """
    try:
        ev = _heures_ecoulees(recent or [], now)
    except Exception as e:                        # jamais bloquant
        log.info("biais macro indisponible: %s", e)
        return {}
    taux = taux or {}
    out: dict[str, dict] = {}
    for c in ccys:
        s24, n24 = _fenetre(ev, c, 24)
        s72, n72 = _fenetre(ev, c, 72)
        tx = taux.get(c) or {}
        mom = tx.get("momentum")
        mom = float(mom) if isinstance(mom, (int, float)) else None
        if n72 == 0 and mom is None:
            continue                              # rien a dire : on n'invente pas un neutre
        # Somme bornee plutot que moyenne : chaque terme est deja dans [-1, +1], et une
        # moyenne ferait BAISSER un biais deja positif a l'arrivee d'une surprise positive
        # plus faible — l'inverse de ce qu'on veut lire.
        surprise = 0.6 * s24 + 0.4 * s72
        biais = round(max(-1.0, min(1.0, (mom or 0.0) + surprise)), 3)
        lecture = ("positif" if biais > SEUIL else
                   "negatif" if biais < -SEUIL else "neutre")
        fiche = {"biais": biais, "lecture": lecture,
                 "surprise24": s24, "surprise72": s72,
                 "couverture": {"evenements_24h": n24, "evenements_72h": n72},
                 "fiabilite": _fiabilite(n72, tx),
                 "source": "taux FRED" + (" + surprises du calendrier" if n72 else "")}
        if mom is not None:
            fiche["taux"] = {"momentum": mom, "serie": tx.get("libelle"),
                             "age_jours": tx.get("age_jours")}
            # Le DETAIL chiffre (variation 90j de chaque moteur) doit remonter tel quel :
            # le filtre de preuves du desk n'accepte que des valeurs CITABLES. Un analyste
            # ne peut rien sourcer avec "momentum -0.295" ; il peut sourcer "taux reel
            # 10 ans +0.67 pt sur 90 jours".
            if tx.get("detail"):
                fiche["taux"]["detail"] = tx["detail"]
            if tx.get("variation_points") is not None:
                fiche["taux"]["variation_points"] = tx["variation_points"]
        out[c] = fiche
    return out


def _fiabilite(n72: int, tx: dict) -> str:
    """Un score assis sur un taux vieux de deux mois et zero evenement ne vaut pas
    un score assis sur un taux du jour et cinq publications : on le dit."""
    age = tx.get("age_jours")
    vieux = isinstance(age, (int, float)) and age > 45
    if n72 >= 5 and not vieux:
        return "correcte"
    if n72 >= 2 or (tx and not vieux):
        return "moyenne"
    return "faible"
