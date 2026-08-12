# -*- coding: utf-8 -*-
"""Helpers de contexte pour les employes du desk.

Ils lisent le dossier de cycle deja prepare par l'orchestrateur (brain.tools) et le
mettent en forme pour les prompts. Aucun acces broker direct : tout passe par le
`provider` live (l'orchestrateur) deja branche via brain.tools.bind_live.
"""
from __future__ import annotations

import json
from typing import Any

from brain import tools as T


def read() -> dict:
    """Contexte de cycle en lecture seule (account, positions, snapshots, charts,
    news, strategies, postmortem, lessons)."""
    return T.cycle_context()


def fmt(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def live() -> Any:
    """Fournisseur d'acces marche branche par l'orchestrateur (`tools.bind_live`).
    None hors cycle (ou en rejeu hors-ligne) : les appelants doivent le tolerer."""
    return T._live()


def candidate_symbols(ctx: dict) -> list[str]:
    """Symboles pre-scannes ce cycle (point de depart des candidats du Gerant)."""
    return list((ctx.get("snapshots") or {}).keys())


def positions_brief(positions: list[dict]) -> list[dict]:
    """Vue compacte des positions ouvertes pour un prompt (pas tout le detail)."""
    out = []
    for p in positions or []:
        out.append({
            "ticket": p.get("ticket"), "symbol": p.get("symbol"),
            "direction": p.get("direction"), "strategy": p.get("strategy"),
            "floating_R": p.get("floating_R"), "floating_pnl": p.get("floating_pnl"),
            "dist_sl_pips": p.get("dist_sl_pips"), "dist_tp_pips": p.get("dist_tp_pips"),
            "mfe_R": p.get("mfe_R"), "mae_R": p.get("mae_R"), "age_h": p.get("age_h"),
            "trailing": bool(p.get("trailing")),
        })
    return out


def position_by_ticket(ctx: dict, ticket: int) -> dict:
    for p in ctx.get("positions") or []:
        if p.get("ticket") == ticket:
            return p
    return {}


def symbol_dossier(sym: str, ctx: dict, live: Any = None) -> dict:
    """Dossier cible d'un symbole (pour les analystes / le Trader focalise) : snapshot,
    chart, news du symbole, couts. Charge a la demande ce qui manque via le provider."""
    sym = (sym or "").strip().upper()
    snaps = ctx.get("snapshots") or {}
    charts = ctx.get("charts") or {}
    news = ctx.get("news") or {}
    dossier: dict[str, Any] = {"symbol": sym}
    dossier["scan"] = snaps.get(sym)
    dossier["chart"] = charts.get(sym)
    if live is not None:
        if dossier["scan"] is None:
            try:
                dossier["scan"] = live.market(sym)
            except Exception:
                pass
        if dossier["chart"] is None:
            try:
                dossier["chart"] = live.chart(sym, [], 10)
            except Exception:
                pass
        try:
            dossier["couts"] = live.costs_view(sym)
        except Exception:
            dossier["couts"] = None
    per = (news.get("per_symbol") or {}) if isinstance(news, dict) else {}
    dossier["news"] = per.get(sym)
    bo = (news.get("blackout") or {}) if isinstance(news, dict) else {}
    dossier["blackout"] = bo.get(sym)
    return dossier
