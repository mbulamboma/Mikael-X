# -*- coding: utf-8 -*-
"""MEMOIRE SITUATIONNELLE — « qu'est-il arrive les dernieres fois, DANS CE CAS-LA ? »

Jusqu'ici l'agent relisait ses 12 lecons les plus recentes/pertinentes par symbole. C'est
mieux que rien, mais ca ne repond pas a la seule question qui compte au moment de decider :
*dans une configuration comme celle d'aujourd'hui, qu'est-ce qui s'est reellement passe ?*

On calcule donc une SIGNATURE deterministe de la situation (regime, volatilite relative,
position dans le range, RSI, momentum, session, news proche, direction envisagee), et on
retrouve les trades passes dont la signature est la plus proche, avec leur R realise.

Pourquoi pas d'embeddings : aucun service externe, aucun cout par appel, aucune derive de
modele — et surtout des dimensions qu'on peut EXPLIQUER. Une distance ponderee sur sept
variables de marche est lisible, deterministe et rejouable ; un vecteur de 1536 nombres ne
l'est pas. Le prix a payer est de choisir les variables a la main : c'est fait ci-dessous,
et c'est discutable — donc modifiable.

Ce module ne fait AUCUN appel LLM et ne touche a rien : il lit des dicts, il rend un bloc
de texte pour un prompt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

#: Variables continues : (cle de signature, echelle de normalisation). L'echelle est
#: l'ecart « typique » au-dela duquel deux situations n'ont plus grand-chose en commun.
ECHELLES = {
    "atr_pct": 0.5,        # volatilite en % du prix (ATR14/prix)
    "rsi": 30.0,           # 0-100
    "pos_range": 40.0,     # position dans le canal Donchian 20 (0 = bas, 100 = haut)
    "momentum_20": 3.0,    # rendement 20 bougies, en %
}
#: Variables categorielles : cout ajoute quand elles different.
POIDS = {"regime": 1.0, "direction": 0.8, "session": 0.3, "news_proche": 0.3}

ROLES_CONNUS = {"gerant", "technique", "fondamental", "sentiment", "actualite",
                "bull", "bear", "juge", "trader", "agressif", "neutre", "prudent", "suivi"}


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def session_de(now: Optional[datetime] = None) -> str:
    """Session de marche (heure UTC). Le comportement d'une paire n'est pas le meme a
    l'ouverture de Londres qu'en seance asiatique."""
    h = (now or datetime.now(timezone.utc)).hour
    if 0 <= h < 7:
        return "asie"
    if 7 <= h < 13:
        return "europe"
    if 13 <= h < 21:
        return "us"
    return "asie"


def signature(symbol: str, snap: dict, regime: str = "", direction: str = "",
              news_proche: bool = False, now: Optional[datetime] = None) -> dict:
    """Signature deterministe d'une situation de marche. Les champs absents restent None :
    la distance les ignore plutot que de deviner."""
    snap = snap or {}
    return {
        "symbol": (symbol or "").upper(),
        "regime": (regime or "").lower() or None,
        "direction": (direction or "").lower() or None,
        "session": session_de(now),
        "news_proche": bool(news_proche),
        "atr_pct": _f(snap.get("atr_pct_price")),
        "rsi": _f(snap.get("rsi14")),
        "pos_range": _f(snap.get("pos_in_range_pct")),
        "momentum_20": _f(snap.get("ret_20b_pct")),
    }


def distance(a: dict, b: dict) -> Optional[float]:
    """Distance entre deux situations. None si elles n'ont AUCUNE variable comparable —
    on prefere ne rien dire plutot que de rapprocher deux inconnues."""
    if not a or not b:
        return None
    total, comparees = 0.0, 0
    for cle, echelle in ECHELLES.items():
        x, y = a.get(cle), b.get(cle)
        if x is None or y is None:
            continue
        total += min(2.0, abs(x - y) / echelle)      # borne : une variable ne domine pas tout
        comparees += 1
    for cle, poids in POIDS.items():
        x, y = a.get(cle), b.get(cle)
        if x is None or y is None:
            continue
        total += 0.0 if x == y else poids
        comparees += 1
    if not comparees:
        return None
    # meme symbole = leger bonus de proximite (un EURUSD ressemble plus a un EURUSD)
    if a.get("symbol") and a.get("symbol") == b.get("symbol"):
        total -= 0.25
    return round(max(0.0, total), 3)


def similaires(sig: dict, trades: list[dict], k: int = 5) -> list[dict]:
    """Les k trades passes les plus proches, du plus proche au plus lointain.

    `trades` = evenements `trade_closed` ; seuls ceux qui portent une signature
    (`dossier.situation`, ecrite depuis la Phase 4) sont comparables."""
    notes = []
    for t in trades or []:
        autre = ((t.get("dossier") or {}).get("situation")) if isinstance(t, dict) else None
        d = distance(sig, autre) if autre else None
        if d is None:
            continue
        notes.append((d, t))
    notes.sort(key=lambda x: x[0])
    return [{**t, "_distance": d} for d, t in notes[:k]]


def bloc_prompt(sig: dict, trades: list[dict], k: int = 5) -> str:
    """Bloc lisible a injecter dans un prompt : les cas passes les plus proches + leur
    resultat, et le bilan agrege de ces cas."""
    proches = similaires(sig, trades, k)
    if not proches:
        return "(aucun cas passe comparable — configuration inedite pour l'agent)"
    lignes = []
    Rs = []
    for t in proches:
        R = _f(t.get("R"))
        if R is not None:
            Rs.append(R)
        s = t.get("dossier", {}).get("situation", {})
        lignes.append(
            f"- {t.get('symbol')} {s.get('direction') or '?'} [{t.get('strategy')}] "
            f"regime={s.get('regime')} atr={s.get('atr_pct')}% rsi={s.get('rsi')} "
            f"pos_range={s.get('pos_range')}% -> {t.get('result') or '?'} "
            f"R={R if R is not None else '?'} (proximite {t['_distance']})")
    if Rs:
        gagnants = sum(1 for r in Rs if r > 0)
        lignes.append(f"BILAN DE CES {len(Rs)} CAS : {gagnants} gagnant(s), "
                      f"expectancy {sum(Rs) / len(Rs):+.2f}R, cumul {sum(Rs):+.2f}R")
    return "\n".join(lignes)
