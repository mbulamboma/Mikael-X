# -*- coding: utf-8 -*-
"""LES 4 ANALYSTES — quatre regards INDEPENDANTS sur un candidat.

Technique, Fondamental, Sentiment, Actualite mondiale. Chacun rend un brief court et
structure que le Trader (et, en Phase 3, les debatteurs) recevront.

DEUX PARTIS PRIS, differents du cadre TradingAgents (cf. IMPLEMENTATION.md §7bis) :

1. INDEPENDANCE. Aucun analyste ne voit le brief des autres. Un analyste qui lit ses
   collegues s'aligne : on obtiendrait un consensus premature et quatre fois le meme
   avis, ce qui coute quatre appels pour la valeur d'un seul. La confrontation est le
   travail du debat (Phase 3) et du Trader, pas des analystes.

2. DOSSIERS PRE-CHARGES plutot que tool-calling. Le papier donne des outils a ses
   analystes (boucle ReAct) ; le modele actif (DeepSeek V3 sur Bedrock) ne sait pas
   appeler d'outils, et une boucle d'outils coute cher et derive. On construit donc,
   de facon DETERMINISTE, un dossier specialise par metier — l'analyste ne fait plus
   que raisonner dessus. Toute source indisponible laisse simplement un trou dans le
   dossier (`None`), jamais une exception.

COUT : 4 appels par candidat (borne par `DESK_MAX_CANDIDATS`), uniquement quand le
Gerant convoque le desk. Coupe-circuit : `DESK_USE_ANALYSTS=0`.

PANNE : l'echec d'UN analyste retire son brief, rien de plus. Si le LLM est globalement
injoignable, c'est l'appel du Trader qui leve `DeskUnavailable` -> aucune ouverture ce
cycle, le book reste gere (contrat inchange).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from desk.base import DeskAgent, DeskUnavailable
from desk import context as C

log = logging.getLogger("desk.analystes")

#: Toile de fond macro, chargee UNE fois par cycle (independante du symbole) : taux longs
#: et courts US, inflation, chomage, taux directeur. Reperes de regime, pas de signaux.
SERIES_MACRO = ("DGS10", "DGS2", "CPIAUCSL", "UNRATE", "FEDFUNDS")

SYSTEM = """Tu es l'ANALYSTE {titre} d'une entreprise de trading FTMO (compte {account_size:.0f}
USD). Tu analyses UN symbole : {symbol}.

Tu travailles SEUL : tu ne vois pas le travail des autres analystes, et c'est voulu — le desk
veut quatre regards INDEPENDANTS, pas un consensus premature. Reste dans TON domaine ; si le
dossier ne dit rien d'exploitable, dis-le et rends un biais neutre avec une confiance basse.
Un « je ne sais pas » honnete vaut mieux qu'une conviction inventee.

TA MISSION : {mission}

Regles : appuie CHAQUE affirmation sur un chiffre du dossier ; pas de generalites ; pas de
recommandation de taille de position (ce n'est ni ton role ni ton information) ; profil SWING
(tenue plusieurs jours), donc raisonne en journalier/hebdomadaire, pas en bruit intraday.

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"biais": "haussier|baissier|neutre",
  "confiance": 0.6,
  "resume": "2-3 phrases chiffrees, dans ton domaine uniquement",
  "points_cles": ["fait chiffre 1", "fait chiffre 2"],
  "invalidation": "ce qui prouverait que tu as tort"}}"""


def _safe(fn: Callable[[], Any], quoi: str) -> Any:
    """Appel de source de donnees tolerant a la panne : un trou dans le dossier ne doit
    jamais empecher l'analyse (ni le cycle)."""
    try:
        return fn()
    except Exception as e:                       # broker/API/reseau : degradation douce
        log.info("Source '%s' indisponible: %s", quoi, e)
        return None


class Analyste(DeskAgent):
    """Base des 4 analystes : meme squelette, un dossier et une mission par metier."""
    model_role = "analyste"        # meme modele pour les 4 ; lecons separees par `role`
    mission = ""

    def dossier(self, symbol: str, ctx: dict, live: Any, commun: dict) -> dict:
        raise NotImplementedError

    def brief(self, symbol: str, mandate: dict, lessons: str, commun: dict) -> dict:
        ctx = C.read()
        dossier = self.dossier(symbol, ctx, C.live(), commun)
        system = SYSTEM.format(titre=self.title, symbol=symbol, mission=self.mission,
                               account_size=self.cfg.ftmo.account_size)
        human = "\n\n".join([
            f"== TON DOSSIER SUR {symbol} ==\n" + C.fmt(dossier),
            "== CONSIGNES DU GERANT ==\n" + (mandate.get("consignes") or "(aucune)"),
            "== TES LECONS PASSEES ==\n" + (lessons or "(aucune)"),
        ])
        return self._normalise(self.ask_json(system, human + "\n\nRends UNIQUEMENT ton brief JSON."))

    def _normalise(self, data: dict) -> dict:
        """Un brief mal forme devient un brief NEUTRE : il ne doit ni disparaitre en
        silence, ni faire passer du bruit pour une conviction."""
        biais = str(data.get("biais") or "").strip().lower()
        if biais not in ("haussier", "baissier", "neutre"):
            biais = "neutre"
        try:
            confiance = min(1.0, max(0.0, float(data.get("confiance", 0.0))))
        except (TypeError, ValueError):
            confiance = 0.0
        brut = data.get("points_cles")
        # une CHAINE est iterable : sans ce garde-fou, "abc" deviendrait ['a','b','c']
        points = [str(p)[:200] for p in (brut if isinstance(brut, list) else [])
                  if isinstance(p, (str, int, float))][:5]
        return {"analyste": self.title, "biais": biais, "confiance": round(confiance, 2),
                "resume": str(data.get("resume") or "").strip()[:600],
                "points_cles": points,
                "invalidation": str(data.get("invalidation") or "").strip()[:300]}


class AnalysteTechnique(Analyste):
    role, title = "technique", "TECHNIQUE"
    mission = ("lire la STRUCTURE du graphique : tendance et regime (W1/D1/H4), niveaux "
               "reels (swings, Donchian, plus haut/plus bas), position du prix dans sa "
               "volatilite (ATR), et ou passerait une invalidation propre. Donne des NIVEAUX "
               "chiffres, pas des impressions.")

    def dossier(self, symbol, ctx, live, commun):
        d = C.symbol_dossier(symbol, ctx, live)
        if live is not None:
            d["atr_D1"] = _safe(lambda: live.indicator(symbol, "D1", "atr", {"period": 14}),
                                "atr")
            d["rsi_D1"] = _safe(lambda: live.indicator(symbol, "D1", "rsi", {"period": 14}),
                                "rsi")
        d["regimes"] = ctx.get("strategies")
        d.pop("news", None)                    # l'actualite est le domaine d'un autre
        d.pop("blackout", None)
        return d


class AnalysteFondamental(Analyste):
    role, title = "fondamental", "FONDAMENTAL"
    mission = ("juger le fond macro-economique des DEUX devises du symbole : ecart de taux "
               "et sa direction, inflation, emploi, trajectoire des banques centrales. Un "
               "differentiel de taux qui s'ecarte porte une paire ; un cycle qui se retourne "
               "la retourne. Ignore le graphique.")

    def dossier(self, symbol, ctx, live, commun):
        return {"symbole": symbol, "devises": _devises(symbol),
                "series_macro": commun.get("macro"),
                "evenements_a_venir": commun.get("evenements"),
                "couts_de_portage": (C.symbol_dossier(symbol, ctx, live) or {}).get("couts")}


class AnalysteSentiment(Analyste):
    role, title = "sentiment", "SENTIMENT"
    mission = ("evaluer le POSITIONNEMENT et l'humeur du marche, avec un oeil CONTRARIEN : "
               "quand une ecrasante majorite de particuliers est du meme cote, le risque "
               "est de leur cote. Croise avec l'extension du prix. Ne fais pas d'analyse "
               "technique ni macro.")

    def dossier(self, symbol, ctx, live, commun):
        snap = (ctx.get("snapshots") or {}).get(symbol)
        retail = _safe(lambda: live.retail_sentiment(symbol), "sentiment retail") \
            if live is not None else None
        return {"symbole": symbol, "sentiment_retail": retail, "scan_prix": snap}


class AnalysteActualite(Analyste):
    role, title = "actualite", "ACTUALITE MONDIALE"
    mission = ("reperer ce qui, dans l'actualite et la geopolitique, peut deplacer ce "
               "symbole a l'echelle de quelques jours : annonces a fort impact, decisions "
               "de banques centrales, tensions, flux de refuge. Distingue le BRUIT "
               "quotidien d'un evenement qui change le regime.")

    def dossier(self, symbol, ctx, live, commun):
        news = ctx.get("news") or {}
        per = (news.get("per_symbol") or {}) if isinstance(news, dict) else {}
        bo = (news.get("blackout") or {}) if isinstance(news, dict) else {}
        d = {"symbole": symbol, "news_du_symbole": per.get(symbol),
             "blackout": bo.get(symbol), "evenements_a_venir": commun.get("evenements")}
        if live is not None and not d["news_du_symbole"]:
            d["recherche"] = _safe(lambda: live.news_search(symbol, 48), "recherche news")
        return d


def _devises(symbol: str) -> list[str]:
    from risk.ftmo import currencies_of
    return list(currencies_of(symbol or ""))


class Analystes:
    """L'equipe. Construit le dossier COMMUN une fois par cycle, puis interroge les
    quatre analystes sur chaque candidat."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.membres: list[Analyste] = [AnalysteTechnique(cfg), AnalysteFondamental(cfg),
                                        AnalysteSentiment(cfg), AnalysteActualite(cfg)]

    def _commun(self) -> dict:
        """Toile de fond macro, identique pour tous les symboles : chargee UNE fois."""
        live = C.live()
        if live is None:
            return {}
        macro = {}
        for serie in SERIES_MACRO:
            valeur = _safe(lambda s=serie: live.fred_series(s, 8), f"FRED {serie}")
            if valeur:
                macro[serie] = valeur
        return {"macro": macro or None,
                "evenements": _safe(lambda: live.major_events(72), "evenements macro")}

    def briefs(self, symbols: list[str], mandate: dict,
               lessons_for: Optional[Callable[[list[str]], str]] = None) -> dict:
        """{symbole: {rol: brief}} — l'echec d'un analyste retire SON brief, rien d'autre."""
        if not symbols:
            return {}
        commun = self._commun()
        out: dict[str, dict] = {}
        for symbol in symbols:
            briefs = {}
            for membre in self.membres:
                lessons = lessons_for([membre.role]) if lessons_for else ""
                try:
                    briefs[membre.role] = membre.brief(symbol, mandate, lessons, commun)
                except DeskUnavailable as e:
                    log.warning("Analyste %s indisponible sur %s (%s) — brief manquant.",
                                membre.title, symbol, e)
            if briefs:
                out[symbol] = briefs
                log.info("Briefs %s: %s", symbol,
                         {r: f"{b['biais']}/{b['confiance']}" for r, b in briefs.items()})
        return out
