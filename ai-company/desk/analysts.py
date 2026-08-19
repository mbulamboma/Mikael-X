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

ANTI-SPECULATION : un point cle qui ne cite aucun chiffre du dossier est ECARTE
(cf. desk/preuves.py), et un brief qui n'en garde aucun est neutralise (biais neutre,
confiance 0). Un analyste qui n'a rien d'observable a dire ne doit pas peser sur une
decision : c'est exactement la que naissent les theses inventees que le debat, ensuite,
prend pour des faits.

PANNE : l'echec d'UN analyste retire son brief, rien de plus. Si le LLM est globalement
injoignable, c'est l'appel du Trader qui leve `DeskUnavailable` -> aucune ouverture ce
cycle, le book reste gere (contrat inchange).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from brain import tools as T
from desk.base import DeskAgent, DeskUnavailable
from desk import context as C
from desk import preuves as P
from desk import social as SOCIAL

#: outils LECTURE SEULE du catalogue (brain/tools.py) indexes par nom, pour composer un
#: sous-ensemble par metier. Les outils de PLAN (plan_open/close...) en sont volontairement
#: exclus : un analyste OBSERVE, il n'agit pas. Sans langchain, `@tool` est un no-op et les
#: outils restent de simples fonctions (d'ou le repli sur __name__).
def _nom_outil(t: Any) -> str:
    return getattr(t, "name", None) or getattr(t, "__name__", "") or ""


_TOOLS_BY_NAME = {_nom_outil(t): t for t in T.ALL_TOOLS
                  if not _nom_outil(t).startswith("plan_")}


def _outils(*noms: str) -> list:
    return [_TOOLS_BY_NAME[n] for n in noms if n in _TOOLS_BY_NAME]

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

Regles : appuie CHAQUE affirmation sur un chiffre PRESENT DANS TON DOSSIER (niveau, ATR,
RSI, spread, taux, date) et cite-le tel quel. Un point cle sans chiffre du dossier est
AUTOMATIQUEMENT SUPPRIME avant lecture — inutile d'ecrire ce que tu ne peux pas montrer, et
n'invente jamais un chiffre absent du dossier pour contourner ce filtre. Pas de recommandation
de taille de position (ce n'est ni ton role ni ton information) ; profil SWING (tenue plusieurs
jours), donc raisonne en journalier/hebdomadaire, pas en bruit intraday.

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
    model_role = "analyste"        # meme modele pour les 4 ; un `role` distinct par metier
    mission = ""
    outils: tuple = ()             # sous-ensemble d'outils lecture seule propre au metier

    def dossier(self, symbol: str, ctx: dict, live: Any, commun: dict) -> dict:
        raise NotImplementedError

    def brief(self, symbol: str, mandate: dict, commun: dict) -> dict:
        ctx = C.read()
        dossier = self.dossier(symbol, ctx, C.live(), commun)
        system = SYSTEM.format(titre=self.title, symbol=symbol, mission=self.mission,
                               account_size=self.cfg.ftmo.account_size)
        human = "\n\n".join([
            f"== TON DOSSIER SUR {symbol} ==\n" + C.fmt(dossier),
            "== CONSIGNES DU GERANT ==\n" + (mandate.get("consignes") or "(aucune)"),
        ])
        # POINT 2 : chemin TOOL-CAPABLE (opt-in + modele capable). L'analyste peut aller
        # chercher une donnee absente de son dossier pre-charge. Repli integral sur le mode
        # JSON si les outils echouent : un analyste ne doit jamais disparaitre pour ca.
        if self.cfg.desk.analystes_outils and self.outils and self.tools_available():
            try:
                return self._brief_outils(symbol, system, human, dossier)
            except DeskUnavailable as e:
                log.info("Analyste %s: outils indisponibles sur %s (%s) — repli JSON.",
                         self.title, symbol, e)
        data = self.ask_json(system, human + "\n\nRends UNIQUEMENT ton brief JSON.")
        return self._normalise(data, dossier)

    def _brief_outils(self, symbol: str, system: str, human: str, dossier: dict) -> dict:
        """Brief via boucle d'outils. Les OBSERVATIONS des outils sont ajoutees a la matiere
        de preuve : une valeur que l'analyste vient de lire compte comme sourcee."""
        consigne = (human + "\n\nTu peux appeler tes OUTILS pour completer ce dossier avec "
                    "des donnees manquantes (indicateurs, series macro, news...). Quand tu as "
                    "ce qu'il te faut, arrete-toi et rends UNIQUEMENT ton brief JSON.")
        texte, observations = self.run_tools(
            system, consigne, list(self.outils),
            max_iter=self.cfg.desk.analystes_outils_max_iter, return_steps=True)
        data = self._extract(texte)
        # les observations d'outils deviennent une source de preuve supplementaire
        return self._normalise(data, dossier, extra_sources=(" ".join(observations),))

    @staticmethod
    def _extract(texte: str) -> dict:
        from desk.base import extract_json
        return extract_json(texte)

    def _normalise(self, data: dict, dossier: dict | None = None,
                   extra_sources: tuple = ()) -> dict:
        """Un brief mal forme devient un brief NEUTRE : il ne doit ni disparaitre en
        silence, ni faire passer du bruit pour une conviction.

        Meme traitement pour un brief SANS PREUVE : les points cles qui ne citent aucune
        donnee du dossier sont ecartes (ils restent visibles dans `ecartes_sans_preuve`,
        pour qu'on puisse verifier ce que le filtre a retire), et s'il n'en reste aucun,
        le brief perd son biais et sa confiance. Il n'est pas supprime : le Trader doit
        voir qu'un analyste n'avait rien d'observable a dire."""
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
        ecartes: list[str] = []
        if dossier is not None and self.cfg.desk.exiger_preuves:
            # texte_ok : l'ACTUALITE et le SENTIMENT travaillent sur des titres et des
            # formulations, pas sur des nombres. Exiger un chiffre les neutralisait a
            # chaque cycle quelle que soit la qualite de leur travail.
            points, ecartes = P.filtrer(points, P.faits(dossier, *extra_sources),
                                        texte_ok=True)
            if not points:
                biais, confiance = "neutre", 0.0
        brief = {"analyste": self.title, "biais": biais, "confiance": round(confiance, 2),
                 "resume": str(data.get("resume") or "").strip()[:600],
                 "points_cles": points,
                 "invalidation": str(data.get("invalidation") or "").strip()[:300]}
        if ecartes:
            brief["ecartes_sans_preuve"] = ecartes[:5]
            log.info("Analyste %s: %d affirmation(s) sans preuve ecartee(s)%s.",
                     self.title, len(ecartes),
                     " — brief neutralise" if not points else "")
        return brief


class AnalysteTechnique(Analyste):
    role, title = "technique", "TECHNIQUE"
    outils = tuple(_outils("get_market", "get_chart", "compute_indicator"))
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
    outils = tuple(_outils("get_fred_series", "get_macro_events", "get_trading_costs"))
    mission = ("juger le fond macro-economique des DEUX devises du symbole : ecart de taux "
               "et sa direction, inflation, emploi, trajectoire des banques centrales. Un "
               "differentiel de taux qui s'ecarte porte une paire ; un cycle qui se retourne "
               "la retourne. Ignore le graphique.")

    def dossier(self, symbol, ctx, live, commun):
        d = {"symbole": symbol, "devises": _devises(symbol),
             "series_macro": commun.get("macro"),
             "evenements_a_venir": commun.get("evenements"),
             "couts_de_portage": (C.symbol_dossier(symbol, ctx, live) or {}).get("couts")}
        # sources pluggables : fondamentaux d'un TITRE (profil, metriques, inities). Vide pour
        # une paire FX (aucune societe derriere). Opt-in + fail-closed (cf. data/sources.py).
        if live is not None and self.cfg.sources.inject_fundamentals:
            fond = _safe(lambda: live.fundamentals(symbol), "fondamentaux")
            if fond:
                d["fondamentaux"] = fond
        return d


class AnalysteSentiment(Analyste):
    role, title = "sentiment", "SENTIMENT"
    outils = tuple(_outils("get_retail_sentiment", "get_market"))
    mission = ("evaluer le POSITIONNEMENT et l'humeur du marche, avec un oeil CONTRARIEN : "
               "quand une ecrasante majorite de particuliers est du meme cote, le risque "
               "est de leur cote. Croise avec l'extension du prix. Ne fais pas d'analyse "
               "technique ni macro.")

    def dossier(self, symbol, ctx, live, commun):
        snap = (ctx.get("snapshots") or {}).get(symbol)
        retail = _safe(lambda: live.retail_sentiment(symbol), "sentiment retail") \
            if live is not None else None
        d = {"symbole": symbol, "sentiment_retail": retail, "scan_prix": snap}
        # Point 4 : sentiment social OPTIONNEL, assaini et reduit a des agregats chiffres
        # (cf. desk/social.py). Desactive par defaut — c'est du texte non fiable.
        d.update(SOCIAL.dossier_fragment(symbol, live, self.cfg.desk.sentiment_social))
        return d


class AnalysteActualite(Analyste):
    role, title = "actualite", "ACTUALITE MONDIALE"
    outils = tuple(_outils("get_news", "get_macro_events"))
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
        # sources pluggables : titres RSS, en plus du calendrier economique.
        if live is not None and self.cfg.sources.inject_news:
            extra = _safe(lambda: live.news_extra(symbol, 6), "news supplementaires")
            if extra:
                d["news_sources"] = extra
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

    def briefs(self, symbols: list[str], mandate: dict) -> dict:
        """{symbole: {rol: brief}} — l'echec d'un analyste retire SON brief, rien d'autre."""
        if not symbols:
            return {}
        commun = self._commun()
        out: dict[str, dict] = {}
        for symbol in symbols:
            briefs = {}
            for membre in self.membres:
                try:
                    briefs[membre.role] = membre.brief(symbol, mandate, commun)
                except DeskUnavailable as e:
                    log.warning("Analyste %s indisponible sur %s (%s) — brief manquant.",
                                membre.title, symbol, e)
            if briefs:
                out[symbol] = briefs
                log.info("Briefs %s: %s", symbol,
                         {r: f"{b['biais']}/{b['confiance']}" for r, b in briefs.items()})
        return out
