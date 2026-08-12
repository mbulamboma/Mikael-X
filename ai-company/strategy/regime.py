# -*- coding: utf-8 -*-
"""Classification du regime de marche a partir du snapshot technique.

Le regime dit "dans quel monde on est" -> il conditionne QUELLE strategie a le
plus de chances de marcher. C'est la premiere moitie du choix de strategie
(l'autre moitie = le track-record de chaque strategie, cf. scoreboard.py).

Regimes : trend_up, trend_down, range, high_vol.
"""
from __future__ import annotations


def classify(snap: dict) -> str:
    """Mappe un snapshot (data/market.snapshot) vers un label de regime."""
    if not snap or "error" in snap:
        return "unknown"

    atr_pct = snap.get("atr_pct_price", 0.0)
    trend = snap.get("trend_D_MA50_200", "")
    slope = snap.get("ema20_slope", "")
    above200 = snap.get("above_ema200", False)
    pos = snap.get("pos_in_range_pct", 50.0)

    # volatilite anormalement haute -> regime propre aux cassures
    if atr_pct >= 1.2:
        return "high_vol"

    trending = (trend == "haussiere" and slope == "montante" and above200) or \
               (trend == "baissiere" and slope == "descendante" and not above200)
    if trending:
        return "trend_up" if trend == "haussiere" else "trend_down"

    # ni tendance nette ni forte vol -> range (les extremes se rachetent)
    if 15 <= pos <= 85:
        return "range"
    return "trend_up" if trend == "haussiere" else "trend_down"
