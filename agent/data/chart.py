# -*- coding: utf-8 -*-
"""Lecture de chart "comme un trader" — multi-timeframe + structure de prix.

Le snapshot technique (data/market.py) donne les indicateurs du timeframe
principal. Ici on ajoute ce qu'un humain LIT sur le graphique :
  - contexte multi-TF (W1 biais, D1 structure, H4 timing),
  - derniers chandeliers (OHLC) -> le LLM "voit" le price action,
  - points de swing (hauts/bas pivots),
  - niveaux cles (support / resistance, Donchian).

Tout est serialisable -> injecte au LLM via l'outil get_chart.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from data.market import snapshot


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def tf_brief(df: pd.DataFrame, digits: int) -> dict:
    """Resume court d'un timeframe : tendance, pente, RSI, dernier prix."""
    if df is None or len(df) < 60:
        return {"error": "peu de donnees"}
    c = df["close"]
    ema50, ema200 = _ema(c, 50), _ema(c, 200)
    return {
        "close": round(float(c.iloc[-1]), digits),
        "trend": "haussiere" if ema50.iloc[-1] > ema200.iloc[-1] else "baissiere",
        "ema20_slope": "montante" if _ema(c, 20).iloc[-1] > _ema(c, 20).iloc[-5] else "descendante",
        "rsi14": round(_rsi(c), 1),
    }


def recent_candles(df: pd.DataFrame, digits: int, n: int = 10) -> list[dict]:
    """Les n derniers chandeliers (OHLC) — le price action brut."""
    if df is None or df.empty:
        return []
    tail = df.tail(n)
    out = []
    for _, r in tail.iterrows():
        out.append({
            "t": str(r["time"])[:16] if "time" in r else "",
            "o": round(float(r["open"]), digits), "h": round(float(r["high"]), digits),
            "l": round(float(r["low"]), digits), "c": round(float(r["close"]), digits),
        })
    return out


def swing_points(df: pd.DataFrame, digits: int, left: int = 3, right: int = 3) -> dict:
    """Dernier swing haut et swing bas confirmes (pivots fractals)."""
    if df is None or len(df) < left + right + 5:
        return {}
    h, l = df["high"].values, df["low"].values
    sh = sl = None
    for i in range(len(df) - right - 1, left, -1):
        if sh is None and h[i] == max(h[i - left:i + right + 1]):
            sh = round(float(h[i]), digits)
        if sl is None and l[i] == min(l[i - left:i + right + 1]):
            sl = round(float(l[i]), digits)
        if sh and sl:
            break
    return {"swing_high": sh, "swing_low": sl}


def key_levels(df: pd.DataFrame, digits: int) -> dict:
    """Niveaux cles : Donchian 20 (clos) + extremes recents."""
    if df is None or len(df) < 25:
        return {}
    return {
        "donchian20_high": round(float(df["high"].rolling(20).max().iloc[-2]), digits),
        "donchian20_low": round(float(df["low"].rolling(20).min().iloc[-2]), digits),
        "high_60": round(float(df["high"].tail(60).max()), digits),
        "low_60": round(float(df["low"].tail(60).min()), digits),
    }


def main_timeframe(tfs: list[str]) -> str | None:
    """Timeframe de STRUCTURE dans une liste ordonnee du plus grand au plus petit :
    le 2e s'il y en a 3 ou plus (le 1er sert de biais), sinon le 1er."""
    if not tfs:
        return None
    return tfs[1] if len(tfs) >= 3 else tfs[0]


def read(symbol: str, tf_frames: dict[str, pd.DataFrame], spec: dict,
         candles: int = 10, main_tf: str | None = None) -> dict:
    """Assemble la lecture complete du chart pour un symbole.

    tf_frames : dict ORDONNE {timeframe: df}, du plus grand au plus petit. Defaut
    {"W1","D1","H4"} (profil swing), mais l'agent peut demander la combinaison qu'il
    veut (ex {"D1","H4","H1"}). Le timeframe de STRUCTURE porte le snapshot, les swings
    et les niveaux ; le plus grand donne le biais, le plus petit le timing."""
    digits = spec.get("digits", 5)
    main = main_tf if main_tf in tf_frames else main_timeframe(list(tf_frames.keys()))
    dfm = tf_frames.get(main)
    context = {}
    for i, (tf, df) in enumerate(tf_frames.items()):
        role = "structure" if tf == main else ("biais" if i == 0 else "timing")
        context[tf] = {"role": role, **tf_brief(df, digits)}
    return {
        "symbol": symbol,
        "timeframe_principal": main,
        "snapshot": snapshot(symbol, dfm, spec),
        "context": context,
        "swings": swing_points(dfm, digits),
        "levels": key_levels(dfm, digits),
        "last_candles": recent_candles(dfm, digits, candles),
    }
