# -*- coding: utf-8 -*-
"""JOURNAL DES CYCLES — la matiere premiere de toute mesure.

Sans trace des ENTREES d'un cycle, on ne peut ni rejouer une decision, ni comparer
deux cerveaux (solo vs desk) sur les memes donnees, ni savoir pourquoi un trade a ete
pris. Ce module enregistre, a chaque cycle :

  - le DOSSIER complet vu par le cerveau (compte/FTMO, positions, scan, charts, news,
    strategies, bilan, lecons) — exactement ce que `tools.bind_context` a publie ;
  - le PLAN rendu (actions), le mode (`solo`/`desk`), l'etat degrade, le mode ombre.

Deux consommateurs :
  - `desk/replay.py` : rejeu hors-ligne (re-decider sur les memes donnees, simuler
    l'issue des trades proposes) ;
  - le post-mortem / l'attribution par role (le dossier de decision d'un trade).

VOLUME : ces enregistrements sont gros (charts + scan). Ils sont donc **en rotation**
(`EVAL_JOURNAL_KEEP`) et ne touchent JAMAIS au journal metier (ordres, vetos,
clotures), qui lui n'est jamais purge.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger("desk.journal")

CYCLE_KIND = "cycle_dossier"
#: au-dela de cette taille, on sacrifie les charts (le plus volumineux) pour garder
#: le reste du dossier plutot que de gonfler la base sans limite.
MAX_PAYLOAD_BYTES = 400_000


def clip(value: Any, limit: int = 400) -> Any:
    """Tronque une chaine trop longue (les dossiers de decision servent a relire, pas
    a rejouer un prompt entier)."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def record_cycle(store, ctx: dict, actions: list[dict], *, mode: str, degraded: bool,
                 shadow: bool, server_now: str = "", keep: int = 0) -> None:
    """Enregistre le dossier d'entree + le plan d'un cycle. Ne leve jamais : une
    panne de journalisation ne doit pas empecher de trader."""
    try:
        payload = {
            "mode": mode, "degraded": bool(degraded), "shadow": bool(shadow),
            "server_now": server_now,
            "account": ctx.get("account") or {},
            "positions": ctx.get("positions") or [],
            "snapshots": ctx.get("snapshots") or {},
            "charts": ctx.get("charts") or {},
            "news": ctx.get("news") or {},
            "strategies": ctx.get("strategies") or "",
            "postmortem": ctx.get("postmortem") or "",
            "lessons": ctx.get("lessons") or "",
            "actions": actions or [],
        }
        if len(json.dumps(payload, ensure_ascii=False, default=str)) > MAX_PAYLOAD_BYTES:
            payload["charts"] = {}
            payload["charts_omis"] = True
        store.log_event(CYCLE_KIND, payload)
        if keep:
            store.trim_events(CYCLE_KIND, keep)
    except Exception as e:                      # journalisation = confort, jamais critique
        log.warning("Journalisation du cycle impossible (ignore): %s", e)


def cycles(store, limit: Optional[int] = None, shadow_only: bool = False) -> list[dict]:
    """Dossiers de cycle enregistres, du plus ancien au plus recent."""
    rows = store.events(kind=CYCLE_KIND, limit=limit)
    return [r for r in rows if not shadow_only or r.get("shadow")]


def planned_opens(store, limit: Optional[int] = None,
                  shadow_only: bool = False) -> list[dict]:
    """Toutes les OUVERTURES proposees par le cerveau, horodatees.

    C'est ce que le simulateur de `replay.py` transforme en R realise. Attention :
    ce sont les propositions AVANT moteur FTMO — comparer leur issue a celle des
    trades reellement executes montre ce que le veto deterministe a fait gagner
    (ou couter)."""
    out = []
    for c in cycles(store, limit=limit, shadow_only=shadow_only):
        for a in c.get("actions") or []:
            if not isinstance(a, dict) or str(a.get("type", "")).lower() != "open":
                continue
            out.append({
                "ts": c.get("ts"), "mode": c.get("mode"), "shadow": bool(c.get("shadow")),
                "symbol": str(a.get("symbol", "")).upper(),
                "direction": str(a.get("direction", "")).lower(),
                "strategy": a.get("strategy"), "confidence": a.get("confidence"),
                "entry": a.get("entry"), "sl": a.get("sl"), "tp": a.get("tp"),
            })
    return out


def executed_opens(store, limit: Optional[int] = None) -> list[dict]:
    """Ouvertures REELLEMENT envoyees au broker (journal metier `order_sent`)."""
    out = []
    for e in store.events(kind="order_sent", limit=limit):
        prop = e.get("proposal") or {}
        res = e.get("result") or {}
        if not res.get("ok"):
            continue
        out.append({
            "ts": e.get("ts"), "symbol": str(prop.get("symbol", "")).upper(),
            "direction": str(prop.get("direction", "")).lower(),
            "strategy": e.get("strategy"), "confidence": prop.get("confidence"),
            "entry": res.get("price") or prop.get("entry"),
            "sl": prop.get("sl"), "tp": prop.get("tp"),
            "lot": e.get("lot"), "rr_net": e.get("rr_net"),
        })
    return out
