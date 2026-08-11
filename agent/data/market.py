# -*- coding: utf-8 -*-
"""Lecture de marche + indicateurs techniques -> snapshot compact pour le LLM.

Le but n'est pas de decider a la place du LLM, mais de lui donner un contexte
CHIFFRE et honnete (pas de look-ahead) : tendance, momentum, volatilite (ATR),
niveaux Donchian, position dans le range. Le LLM raisonne dessus comme un trader.
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _atr(df: pd.DataFrame, n: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def snapshot(symbol: str, df: pd.DataFrame, spec: dict) -> dict:
    """Resume l'etat technique du symbole en un dict serialisable."""
    if df is None or len(df) < 60:
        return {"symbol": symbol, "error": "pas assez de bougies"}

    close = df["close"]
    price = float(close.iloc[-1])
    ema20, ema50, ema200 = _ema(close, 20), _ema(close, 50), _ema(close, 200)
    atr = _atr(df, 14)
    don_hi = float(df["high"].rolling(20).max().iloc[-2])   # -2 = clos, anti look-ahead
    don_lo = float(df["low"].rolling(20).min().iloc[-2])
    rng = max(don_hi - don_lo, 1e-9)

    trend = "haussiere" if ema50.iloc[-1] > ema200.iloc[-1] else "baissiere"
    slope = "montante" if ema20.iloc[-1] > ema20.iloc[-5] else "descendante"

    return {
        "symbol": symbol,
        "price": round(price, spec["digits"]),
        "trend_D_MA50_200": trend,
        "ema20_slope": slope,
        "above_ema200": bool(price > ema200.iloc[-1]),
        "rsi14": round(_rsi(close), 1),
        "atr14": round(atr, spec["digits"]),
        "atr_pct_price": round(atr / price * 100, 3),
        "donchian20_high": round(don_hi, spec["digits"]),
        "donchian20_low": round(don_lo, spec["digits"]),
        "pos_in_range_pct": round((price - don_lo) / rng * 100, 1),  # 0=bas, 100=haut
        "ret_5b_pct": round((price / close.iloc[-6] - 1) * 100, 3),
        "ret_20b_pct": round((price / close.iloc[-21] - 1) * 100, 3),
    }
