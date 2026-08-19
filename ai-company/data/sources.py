# -*- coding: utf-8 -*-
"""COUCHE DE SOURCES EXTENSIBLE — brancher de nouvelles donnees sans toucher aux analystes.

Le desk lisait jusqu'ici : MT5 (prix/chart/couts), FRED (taux), calendrier web, DuckDuckGo
(web), myfxbook (sentiment retail). Ce module ajoute une couche PLUGGABLE ou chaque source
est un petit adaptateur a interface uniforme, et une agregation par CATEGORIE (a l'image de
TradingAgents : Marche / Social / News / Fondamentaux).

TROIS PRINCIPES, herites du reste du desk :
  1. OPT-IN. Une source ne s'active qu'avec sa cle (.env) ou son drapeau. Rien de branche par
     defaut : on n'ajoute pas une dependance reseau a l'insu de l'operateur.
  2. FAIL-CLOSED. Toute source qui tombe (reseau, quota, format) laisse un TROU (None/[]),
     jamais une exception : une source non fiable ne doit ni bloquer ni fausser un cycle.
  3. ASSAINI. Tout texte externe passe par data/sanitize.py (injection de prompt) : on
     n'expose que des agregats chiffres + de rares extraits neutralises.

CE QUI EST BRANCHE ICI (realiste, sans secret invente) :
  - RSS     : n'importe quel flux (Reuters, banques centrales...) via la stdlib -> news ;

CE QUI N'EST PAS BRANCHE, et pourquoi (honnetete) :
  - X/Twitter : plus d'API gratuite exploitable -> hors de portee sans budget ;
  - Yahoo Finance : pas d'API officielle libre, et pour le FX le broker MT5 fait deja foi ;
  - Bloomberg/Reuters : pas d'API libre -> passer par leurs flux RSS (adaptateur RSS ci-dessus).

Chaque adaptateur isole son unique appel HTTP dans `_get()`, ce qui rend les tests
DETERMINISTES et HORS-LIGNE (on injecte la charge utile, on ne touche jamais au reseau).
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from typing import Any, Optional

import requests

from data.sanitize import clean_snippet

log = logging.getLogger("data.sources")

# UA de navigateur : beaucoup de flux (Investing, FXStreet...) refusent un UA inconnu, comme
# le note deja data/web.py. On imite un navigateur pour ne pas se faire jeter en 403.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_TIMEOUT = 8

#: lexique minimal pour classer un post sans API de sentiment. Volontairement grossier et
#: LISIBLE : on assume que le signal social est faible et bruite (usage contrarien).
_BULL = ("long", "buy", "bull", "bullish", "moon", "calls", "up", "breakout", "rally")
_BEAR = ("short", "sell", "bear", "bearish", "puts", "down", "dump", "crash", "breakdown")

#: ALIAS DE SYMBOLE : (requete de recherche, termes a matcher). Sans ca, chercher/filtrer sur
#: « XAUUSD » rate tout : une news sur l'or dit « gold », « bullion », jamais « XAUUSD ». On
#: mappe donc chaque instrument vers ses mots reels. Etendre au besoin (nouvel instrument).
SYMBOL_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "XAUUSD": ("gold", ("xauusd", "xau", "gold", "bullion", "gold price", "precious metal")),
    "XAGUSD": ("silver", ("xagusd", "xag", "silver")),
    "XTIUSD": ("crude oil", ("xtiusd", "wti", "crude", "oil", "brent")),
    "USOIL":  ("crude oil", ("usoil", "wti", "crude", "oil")),
    "US30":   ("dow jones", ("us30", "dow", "djia", "dow jones")),
    "NAS100": ("nasdaq", ("nas100", "nasdaq", "ndx", "us tech")),
    "US500":  ("s&p 500", ("us500", "s&p", "spx", "s&p 500")),
    "GER40":  ("dax", ("ger40", "dax", "germany 40")),
    "BTCUSD": ("bitcoin", ("btcusd", "btc", "bitcoin")),
    "ETHUSD": ("ethereum", ("ethusd", "eth", "ethereum")),
}


def _alias(symbol: str) -> tuple[str, tuple[str, ...]]:
    """(requete, termes de match) pour un symbole. Repli FX : la paire + ses deux devises ;
    repli generique : le symbole lui-meme."""
    s = (symbol or "").upper()
    if s in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[s]
    if len(s) == 6 and s.isalpha():                       # paire FX EURUSD -> eur, usd
        return (s, (s.lower(), s[:3].lower(), s[3:].lower()))
    return (s, (s.lower(),)) if s else ("", ())


# --------------------------------------------------------------------------- socle HTTP
class Source:
    """Base d'un adaptateur. Sous-classe : definir `name`, `enabled`, et les methodes metier.
    `_get` est l'unique porte reseau (monkeypatchee dans les tests)."""
    name = "source"

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return False

    def _get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
        """GET texte brut. Leve en cas d'anomalie ; `_get_json` transforme en trou.

        `requests` et NON `urllib.request` : urllib s'appuie sur le magasin de
        certificats du systeme, qui manque sur beaucoup de VPS Windows fraichement
        installes -> CERTIFICATE_VERIFY_FAILED sur CHAQUE source, alors que le reste
        du projet (Bedrock, FRED, calendrier, web) passait deja par requests et son
        bundle certifi. Une source qui tombe pour une raison d'installation est le
        pire des trous : silencieuse et totale.
        """
        r = requests.get(url, params=params or None,
                         headers={"User-Agent": _UA, **(headers or {})},
                         timeout=_TIMEOUT)
        r.raise_for_status()
        return r.text

    def _get_json(self, url: str, params: Optional[dict] = None,
                  headers: Optional[dict] = None) -> Any:
        try:
            return json.loads(self._get(url, params, headers))
        except Exception as e:                    # reseau/quota/format : trou, pas d'exception
            log.info("%s: appel indisponible (%s).", self.name, e)
            return None


def _classe_texte(texte: str) -> Optional[str]:
    """Sentiment heuristique d'un texte social. None si aucun signal directionnel."""
    bas = (texte or "").lower()
    b = sum(1 for m in _BULL if m in bas)
    s = sum(1 for m in _BEAR if m in bas)
    if b == s:
        return None
    return "bull" if b > s else "bear"


def score_news(items: list[dict]) -> dict:
    """LE SEUL CHIFFRE qui decrit l'actualite ELLE-MEME — pas le biais macro du Fondamental
    ni les echeances du calendrier. Sans lui, l'analyste ACTUALITE ne pouvait citer que le
    travail d'un autre : il n'avait aucune donnee propre a mettre sous une affirmation, donc
    le filtre de preuves le neutralisait quelle que soit la qualite de sa lecture.

    Chaque titre deja recupere (`news_extra`) est classe par le MEME lexique grossier que le
    sentiment social (`_classe_texte`) — assume comme tel, heuristique et non un modele. Le
    `score` est la part nette directionnelle des titres, dans [-1, +1] :
    (haussiers - baissiers) / titres. C'est ce nombre, present dans le dossier, que
    l'analyste peut enfin sourcer.

    PUR et DETERMINISTE (aucun reseau ici : on agrege une liste deja en main) -> testable
    hors-ligne. {} si aucun titre (fail-closed, comme le reste de la couche)."""
    items = items or []
    if not items:
        return {}
    signes = [_classe_texte(it.get("title", "")) for it in items]
    haussiers = signes.count("bull")
    baissiers = signes.count("bear")
    n = len(items)
    return {"titres": n, "haussiers": haussiers, "baissiers": baissiers,
            "neutres": n - haussiers - baissiers,
            "score": round((haussiers - baissiers) / n, 2),
            "methode": "heuristique lexicale (grossiere)"}


# --------------------------------------------------------------------------- RSS (libre)
class RSSNewsSource(Source):
    """News via n'importe quel flux RSS/Atom (Reuters, banques centrales, medias). Parse
    stdlib, aucun secret. Les flux se configurent dans SOURCES_RSS (URLs separees par des ,)."""
    name = "rss"

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "rss_feeds", None))

    def news_items(self, symbol: str, limit: int = 8) -> list[dict]:
        if not self.enabled:
            return []
        out: list[dict] = []
        _, termes = _alias(symbol)                # XAUUSD -> gold/bullion/xau/...
        for url in self.cfg.rss_feeds:
            xml = None
            try:
                xml = self._get(url)
            except Exception as e:
                log.info("rss %s indisponible (%s).", url, e)
            for item in _parse_rss(xml or ""):
                titre = clean_snippet(item.get("title", ""), 200)
                if not titre:
                    continue
                # filtre par ALIAS : un flux generaliste ne parle pas que de notre instrument.
                # Sans terme (symbole vide), on ne filtre pas.
                blob = (titre + " " + (item.get("summary") or "")).lower()
                if termes and not any(t in blob for t in termes):
                    continue
                out.append({"title": titre, "date": item.get("date"),
                            "source": item.get("source") or "rss"})
                if len(out) >= limit:
                    return out
        return out


# --------------------------------------------------------------------------- FXSSI (libre)
class FXSSISource(Source):
    """SENTIMENT RETAIL positionnel (long/short %) — l'alternative a myfxbook, SANS compte.

    myfxbook exige des identifiants ; Dukascopy et la page myfxbook publique repondent 403.
    FXSSI expose le ratio acheteurs/vendeurs par symbole sur une page publique (dont XAUUSD).
    On extrait le SEUL chiffre utile (part longue) par un regex cible sur le symbole — pas de
    scrap massif. Best-effort et FAIL-CLOSED : si la page change ou tombe, on rend {}.

    C'est un signal de POSITIONNEMENT (usage contrarien), pas une opinion : exactement ce que
    l'analyste Sentiment attend en `sentiment_retail`."""
    name = "fxssi"
    URL = "https://fxssi.com/tools/current-ratio"

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "fxssi_enabled", False))

    def retail_sentiment(self, symbol: str) -> dict:
        if not self.enabled:
            return {}
        sym = (symbol or "").upper()
        try:
            html = self._get(self.URL)
        except Exception as e:
            log.info("fxssi indisponible (%s).", e)
            return {}
        # <div class="symbol">XAUUSD</div> ... <div class="ratio-bar-left" ... width: 65%
        import re
        m = re.search(r'class="symbol">\s*' + re.escape(sym) +
                      r'\s*</div>.*?ratio-bar-left"[^>]*width:\s*(\d+(?:\.\d+)?)%',
                      html, re.S)
        if not m:
            return {}
        longs = float(m.group(1))
        shorts = round(100.0 - longs, 1)
        return {"symbol": sym, "long_pct": round(longs, 1), "short_pct": shorts,
                # biais net contrarien : quand la foule est massivement longue, le risque est
                # du cote long. On expose le chiffre, l'analyste (contrarien) l'interprete.
                "foule_nette": round((longs - shorts) / 100.0, 2),
                "source": "fxssi (retail, best-effort)"}


# --------------------------------------------------------------------------- agregateur
class Sources:
    """Facade : construit les adaptateurs actifs et fusionne leurs sorties par categorie.
    C'est CE que branche l'orchestrateur sur le provider live."""

    def __init__(self, cfg):
        self.cfg = cfg
        # Finnhub et EODHD retires le 2026-08-19 : 403 sur les endpoints utiles (leurs
        # donnees societe/social sont orientees ACTIONS, et leur calendrier est payant).
        # Une cle qui ne rend rien est pire qu'une source absente : elle laisse croire
        # que la colonne est couverte. Les seams restent ouverts pour une vraie source.
        self._social = []
        self._news = [RSSNewsSource(cfg)]
        self._fond = []
        self._retail = [FXSSISource(cfg)]

    def actives(self) -> list[str]:
        vus, out = set(), []
        for s in self._social + self._news + self._fond + self._retail:
            if s.enabled and s.name not in vus:
                vus.add(s.name)
                out.append(s.name)
        return out

    def social_sentiment(self, symbol: str) -> dict:
        """Forme consommee par desk/social.py : {'items': [...]}. Fusion des sources sociales
        actives ; {} si aucune (le desk n'affiche alors pas de sentiment social)."""
        items: list[dict] = []
        for s in self._social:
            try:
                items += s.social_items(symbol) or []
            except Exception as e:                # fail-closed par source
                log.info("%s social en echec (%s).", s.name, e)
        return {"items": items} if items else {}

    def news_extra(self, symbol: str, limit: int = 8) -> list[dict]:
        out: list[dict] = []
        for s in self._news:
            try:
                out += s.news_items(symbol, limit) or []
            except Exception as e:
                log.info("%s news en echec (%s).", s.name, e)
        return out[:limit]

    def fundamentals(self, symbol: str) -> dict:
        """Fondamentaux d'un titre (profil, metriques, inities). {} pour une paire FX (aucune
        societe) ou si aucune source active."""
        for s in self._fond:
            try:
                f = s.fundamentals(symbol)
                if f:
                    return f
            except Exception as e:
                log.info("%s fondamentaux en echec (%s).", s.name, e)
        return {}

    def retail_sentiment(self, symbol: str) -> dict:
        """Positionnement retail long/short (FXSSI) — alternative a myfxbook, sans compte.
        {} si aucune source active ou si le symbole est absent (fail-closed)."""
        for s in self._retail:
            try:
                r = s.retail_sentiment(symbol)
                if r:
                    return r
            except Exception as e:
                log.info("%s retail en echec (%s).", s.name, e)
        return {}


# --------------------------------------------------------------------------- helpers
def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", {}, [])}


def _insider_net(inities: dict) -> Optional[int]:
    """Solde net des transactions d'inities (achats - ventes en nombre d'operations) : un
    chiffre, jamais la liste brute."""
    data = (inities or {}).get("data") or []
    if not data:
        return None
    net = 0
    for t in data:
        chg = _num(t.get("change"))
        if chg is None:
            continue
        net += 1 if chg > 0 else (-1 if chg < 0 else 0)
    return net


def _parse_rss(xml: str) -> list[dict]:
    """Parse RSS OU Atom en une liste {title, summary, date, source}. [] si illisible."""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out: list[dict] = []
    # RSS 2.0 : channel/item ; Atom : feed/entry (avec espace de noms)
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        titre = summary = date = ""
        for child in item:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "title":
                titre = (child.text or "").strip()
            elif ctag in ("description", "summary"):
                summary = (child.text or "").strip()
            elif ctag in ("pubDate", "updated", "published"):
                date = (child.text or "").strip()
        if titre:
            out.append({"title": titre, "summary": summary, "date": date, "source": "rss"})
    return out
