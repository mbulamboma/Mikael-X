# -*- coding: utf-8 -*-
"""REFLEXION HYBRIDE — la note qualitative que les stats seules n'encodent pas.

Le desk a fait un choix fort : PAS de « lecon » ecrite librement par un LLM apres une
cloture (cf. brain/memory.py). Les stats calculees (post-mortem, scoreboard, cas
comparables) sont verifiables ; une lecon inventee ne l'est pas. On garde ce principe.

Ce module ajoute le CHAINON MANQUANT sans rouvrir la porte a l'affabulation : une note
courte, generee a la cloture, mais **contrainte de ne parler que des faits enregistres du
trade** (R realise, resultat, MFE/MAE, derive d'entree, signature de situation, rols qui ont
decide). Meme discipline que le filtre de preuves : ce qui ne renvoie a aucun fait est retire.

A quoi ca sert : « MFE +1.4R rendu jusqu'a -0.3R, entree en retard de 6 pips, en range
haussier RSI 71 » dit quelque chose qu'une expectancy moyenne ne dit pas. On retrouve ensuite,
au moment de decider, les notes des situations les PLUS PROCHES (memoire situationnelle,
desk/situation.py) et on les donne au Trader — a cote des chiffres, pas a leur place.

REPLI SANS LLM : si le modele est injoignable, la note est un resume DETERMINISTE des memes
faits. On ne saute jamais la reflexion pour cause de panne, et on n'invente jamais rien.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from desk.base import DeskAgent, DeskUnavailable
from desk import situation as SIT

log = logging.getLogger("desk.reflexion")

SYSTEM = """Tu ecris une REFLEXION POST-TRADE d'une phrase pour une entreprise de trading FTMO.
REGLE ABSOLUE : tu ne parles QUE des faits chiffres qu'on te donne (R, resultat, MFE/MAE,
derive d'entree, regime, RSI...). Interdiction d'inventer un chiffre, une cause macro ou un
evenement absent des faits. Pas de conseil general, pas de « il faudrait » : un CONSTAT
factuel sur CE trade, utile a relire dans une situation semblable.

Reponds UNIQUEMENT par un objet JSON : {{"note": "un constat factuel, <= 240 caracteres"}}"""


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _faits(trade: dict) -> dict:
    """Extrait les faits chiffres relus par la note. Aucun jugement, que du mesure."""
    dossier = trade.get("dossier") or {}
    sit = dossier.get("situation") or {}
    faits = {
        "symbol": trade.get("symbol"),
        "direction": trade.get("direction") or sit.get("direction"),
        "strategy": trade.get("strategy"),
        "R": _f(trade.get("R")),
        "resultat": trade.get("result"),
        "mfe_R": _f(trade.get("mfe_R")),
        "mae_R": _f(trade.get("mae_R")),
        "derive_entree_pips": _f(trade.get("derive_entree_pips")),
        "duree_h": _f(trade.get("duree_h")),
        "regime": sit.get("regime"),
        "rsi": _f(sit.get("rsi")),
        "pos_range": _f(sit.get("pos_range")),
        "atr_pct": _f(sit.get("atr_pct")),
    }
    return {k: v for k, v in faits.items() if v is not None and v != ""}


#: chiffres/mots que la note LLM a le droit d'employer, derives des faits. Toute note qui ne
#: cite AUCUN de ces reperes est jugee non sourcee et remplacee par la note deterministe.
def _ancres(faits: dict) -> list[str]:
    ancres: list[str] = []
    for cle in ("symbol", "direction", "strategy", "resultat", "regime"):
        v = faits.get(cle)
        if v:
            ancres.append(str(v).lower())
    for cle in ("R", "mfe_R", "mae_R", "rsi", "pos_range", "atr_pct", "derive_entree_pips"):
        v = faits.get(cle)
        if v is not None:
            # un nombre est cite meme approche : on garde la partie entiere comme ancre
            ancres.append(str(int(abs(v))) if abs(v) >= 1 else f"{v:.1f}".lstrip("-"))
    return [a for a in ancres if a]


def _sourcee(note: str, ancres: list[str]) -> bool:
    """La note renvoie-t-elle a au moins un fait ? Meme logique que le filtre de preuves."""
    bas = (note or "").lower()
    return any(a and a in bas for a in ancres)


def note_deterministe(faits: dict) -> str:
    """Resume factuel sans LLM : toujours vrai, jamais invente."""
    bits = []
    if faits.get("symbol"):
        bits.append(f"{faits['symbol']} {faits.get('direction') or ''}".strip())
    if faits.get("strategy"):
        bits.append(f"[{faits['strategy']}]")
    if faits.get("resultat") is not None:
        bits.append(f"{faits['resultat']} R={faits.get('R')}")
    if faits.get("mfe_R") is not None and faits.get("mae_R") is not None:
        bits.append(f"MFE {faits['mfe_R']}R / MAE {faits['mae_R']}R")
    if faits.get("derive_entree_pips"):
        bits.append(f"derive entree {faits['derive_entree_pips']} pips")
    if faits.get("regime"):
        bits.append(f"regime {faits['regime']}")
    if faits.get("rsi") is not None:
        bits.append(f"RSI {faits['rsi']:.0f}")
    return " ".join(b for b in bits if b)[:240]


class Reflecteur(DeskAgent):
    """Ecrit la note LLM contrainte. Rol dedie -> trace et modele configurables comme les
    autres employes ; classe 'fort' par defaut (raisonnement rare)."""
    role = "reflexion"

    def note(self, faits: dict) -> str:
        data = self.ask_json(SYSTEM, "== FAITS DU TRADE ==\n" + _fmt(faits)
                             + "\n\nRends UNIQUEMENT ta reflexion JSON.")
        return str(data.get("note") or "").strip()[:240]


def _fmt(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)


def ecrire(cfg, trade: dict, reflecteur: Optional["Reflecteur"] = None) -> dict:
    """Construit l'enregistrement de reflexion d'un trade cloture. Rend un dict pret a
    journaliser (kind `reflexion`). Ne leve jamais : la panne LLM -> note deterministe."""
    faits = _faits(trade)
    note = ""
    if cfg.desk.reflexion_enabled and faits:
        try:
            r = reflecteur or Reflecteur(cfg)
            candidate = r.note(faits)
            if candidate and _sourcee(candidate, _ancres(faits)):
                note = candidate
            elif candidate:
                log.info("Reflexion non sourcee sur %s -> note deterministe (%s).",
                         trade.get("symbol"), candidate[:120])
        except DeskUnavailable as e:
            log.info("Reflecteur injoignable (%s) — note deterministe.", e)
    if not note:
        note = note_deterministe(faits)
    return {
        "symbol": trade.get("symbol"),
        "strategy": trade.get("strategy"),
        "R": faits.get("R"),
        "result": trade.get("result"),
        "note": note,
        # on RECOPIE la signature de situation pour que la reflexion soit retrouvable par
        # proximite exactement comme un trade (reutilise desk/situation.similaires).
        "dossier": {"situation": (trade.get("dossier") or {}).get("situation") or {}},
    }


def bloc_prompt(sig: dict, reflexions: list[dict], k: int = 3) -> str:
    """Bloc a injecter au Trader : les notes des situations passees les plus PROCHES.
    '' si aucune reflexion comparable (le Trader ne voit alors que les stats)."""
    proches = SIT.similaires(sig, reflexions, k)
    proches = [r for r in proches if (r.get("note") or "").strip()]
    if not proches:
        return ""
    lignes = ["REFLEXIONS DES CAS LES PLUS PROCHES (constats factuels, a ponderer) :"]
    for r in proches:
        R = r.get("R")
        lignes.append(f"- {r.get('symbol')} R={R if R is not None else '?'} "
                      f"(proximite {r.get('_distance')}) : {r.get('note')}")
    return "\n".join(lignes)
