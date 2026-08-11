# -*- coding: utf-8 -*-
"""Boite a outils d'INDICATEURS calculables A LA DEMANDE par l'agent.

Le snapshot (data/market.py) et la lecture de chart (data/chart.py) donnent un
contexte fixe. Ici, l'agent demande LUI-MEME l'indicateur qu'il veut, sur le
symbole, le timeframe et la periode de son choix (outil `compute_indicator`) :
c'est ce qui lui permet de changer de methode d'analyse selon la strategie
choisie (tendance -> ADX/EMA, range -> Bollinger/stochastique, volatilite ->
ATR/Keltner, etc.).

Chaque fonction renvoie un dict serialisable :
    {"value": {...derniere(s) valeur(s)...}, "series": [...5 dernieres...], "read": "lecture"}
`read` est une phrase courte d'interpretation — le LLM garde la decision.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ primitives


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rma(s: pd.Series, n: int) -> pd.Series:
    """Moyenne de Wilder (RSI, ATR, ADX)."""
    return s.ewm(alpha=1.0 / max(n, 1), adjust=False).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    return pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)


def _tail(s: pd.Series, digits: int, n: int = 5) -> list:
    out = []
    for v in s.tail(n).tolist():
        out.append(None if v is None or (isinstance(v, float) and not np.isfinite(v))
                   else round(float(v), digits))
    return out


def _r(x, digits: int):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, digits) if np.isfinite(v) else None


# ------------------------------------------------------------------ indicateurs


def sma(df, digits, period: int = 20, **_):
    s = df["close"].rolling(period).mean()
    last, price = s.iloc[-1], df["close"].iloc[-1]
    return {"value": {"sma": _r(last, digits), "price": _r(price, digits)},
            "series": _tail(s, digits),
            "read": f"prix {'au-dessus' if price > last else 'sous'} la SMA{period}"}


def ema(df, digits, period: int = 20, **_):
    s = _ema(df["close"], period)
    last, price = s.iloc[-1], df["close"].iloc[-1]
    slope = "montante" if len(s) > 5 and s.iloc[-1] > s.iloc[-5] else "descendante"
    return {"value": {"ema": _r(last, digits), "price": _r(price, digits), "pente": slope},
            "series": _tail(s, digits),
            "read": f"prix {'au-dessus' if price > last else 'sous'} l'EMA{period}, pente {slope}"}


def rsi(df, digits, period: int = 14, **_):
    d = df["close"].diff()
    up = _rma(d.clip(lower=0), period)
    dn = _rma((-d.clip(upper=0)), period)
    rs = up / dn.replace(0, np.nan)
    s = 100 - 100 / (1 + rs)
    last = float(s.iloc[-1])
    zone = "surachat" if last >= 70 else "survente" if last <= 30 else "neutre"
    return {"value": {"rsi": _r(last, 1), "zone": zone}, "series": _tail(s, 1),
            "read": f"RSI{period} = {last:.1f} ({zone})"}


def atr(df, digits, period: int = 14, **_):
    s = _rma(_true_range(df), period)
    last = float(s.iloc[-1])
    price = float(df["close"].iloc[-1])
    return {"value": {"atr": _r(last, digits), "atr_pct_price": _r(last / price * 100, 3)},
            "series": _tail(s, digits),
            "read": f"volatilite ATR{period} = {last:.{digits}f} ({last / price * 100:.2f} % du prix) "
                    f"— utile pour dimensionner stop et trailing"}


def macd(df, digits, fast: int = 12, slow: int = 26, signal: int = 9, **_):
    line = _ema(df["close"], fast) - _ema(df["close"], slow)
    sig = _ema(line, signal)
    hist = line - sig
    cross = "haussier" if hist.iloc[-1] > 0 else "baissier"
    turned = len(hist) > 1 and np.sign(hist.iloc[-1]) != np.sign(hist.iloc[-2])
    return {"value": {"macd": _r(line.iloc[-1], digits + 1), "signal": _r(sig.iloc[-1], digits + 1),
                      "histogramme": _r(hist.iloc[-1], digits + 1), "biais": cross,
                      "croisement_recent": bool(turned)},
            "series": _tail(hist, digits + 1),
            "read": f"MACD {cross}" + (" avec croisement sur la derniere bougie" if turned else "")}


def bbands(df, digits, period: int = 20, mult: float = 2.0, **_):
    ma = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std(ddof=0)
    up, lo = ma + mult * sd, ma - mult * sd
    price = float(df["close"].iloc[-1])
    width = float((up.iloc[-1] - lo.iloc[-1]) / ma.iloc[-1] * 100)
    pos = (price - lo.iloc[-1]) / max(up.iloc[-1] - lo.iloc[-1], 1e-12) * 100
    squeeze = bool(width <= (up - lo).div(ma).mul(100).rolling(100).quantile(0.2).iloc[-1]) \
        if len(df) > 120 else False
    return {"value": {"haute": _r(up.iloc[-1], digits), "moyenne": _r(ma.iloc[-1], digits),
                      "basse": _r(lo.iloc[-1], digits), "largeur_pct": _r(width, 2),
                      "position_pct": _r(pos, 1), "squeeze": squeeze},
            "series": _tail(up - lo, digits),
            "read": f"prix a {pos:.0f} % de la bande (0=basse, 100=haute)"
                    + (", compression (squeeze) -> cassure possible" if squeeze else "")}


def stoch(df, digits, period: int = 14, smooth: int = 3, **_):
    lo = df["low"].rolling(period).min()
    hi = df["high"].rolling(period).max()
    k = (df["close"] - lo) / (hi - lo).replace(0, np.nan) * 100
    k = k.rolling(smooth).mean()
    d = k.rolling(smooth).mean()
    last = float(k.iloc[-1])
    zone = "surachat" if last >= 80 else "survente" if last <= 20 else "neutre"
    return {"value": {"K": _r(last, 1), "D": _r(d.iloc[-1], 1), "zone": zone},
            "series": _tail(k, 1), "read": f"stochastique {last:.0f} ({zone})"}


def adx(df, digits, period: int = 14, **_):
    up_move = df["high"].diff()
    dn_move = -df["low"].diff()
    plus_dm = up_move.where((up_move > dn_move) & (up_move > 0), 0.0)
    minus_dm = dn_move.where((dn_move > up_move) & (dn_move > 0), 0.0)
    tr = _rma(_true_range(df), period)
    plus_di = 100 * _rma(plus_dm, period) / tr.replace(0, np.nan)
    minus_di = 100 * _rma(minus_dm, period) / tr.replace(0, np.nan)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    s = _rma(dx, period)
    last = float(s.iloc[-1])
    force = "tendance forte" if last >= 25 else "tendance faible / range" if last < 20 else "tendance naissante"
    sens = "haussiere" if plus_di.iloc[-1] > minus_di.iloc[-1] else "baissiere"
    return {"value": {"adx": _r(last, 1), "plus_di": _r(plus_di.iloc[-1], 1),
                      "minus_di": _r(minus_di.iloc[-1], 1), "force": force, "sens": sens},
            "series": _tail(s, 1),
            "read": f"ADX {last:.1f} — {force}, direction {sens}"}


def cci(df, digits, period: int = 20, **_):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(period).mean()
    md = (tp - ma).abs().rolling(period).mean()
    s = (tp - ma) / (0.015 * md.replace(0, np.nan))
    last = float(s.iloc[-1])
    return {"value": {"cci": _r(last, 1)}, "series": _tail(s, 1),
            "read": f"CCI {last:.0f} ({'exces haussier' if last > 100 else 'exces baissier' if last < -100 else 'neutre'})"}


def roc(df, digits, period: int = 10, **_):
    s = (df["close"] / df["close"].shift(period) - 1) * 100
    last = float(s.iloc[-1])
    return {"value": {"roc_pct": _r(last, 3)}, "series": _tail(s, 3),
            "read": f"variation {last:+.2f} % sur {period} bougies"}


def donchian(df, digits, period: int = 20, **_):
    hi = df["high"].rolling(period).max().shift(1)   # shift(1) = canal CLOS, anti look-ahead
    lo = df["low"].rolling(period).min().shift(1)
    price = float(df["close"].iloc[-1])
    rng = max(float(hi.iloc[-1] - lo.iloc[-1]), 1e-12)
    pos = (price - float(lo.iloc[-1])) / rng * 100
    return {"value": {"haut": _r(hi.iloc[-1], digits), "bas": _r(lo.iloc[-1], digits),
                      "milieu": _r((hi.iloc[-1] + lo.iloc[-1]) / 2, digits),
                      "position_pct": _r(pos, 1)},
            "series": _tail(hi, digits),
            "read": f"prix a {pos:.0f} % du canal Donchian{period} (cassure haute >100, basse <0)"}


def keltner(df, digits, period: int = 20, mult: float = 2.0, **_):
    ma = _ema(df["close"], period)
    rng = _rma(_true_range(df), period)
    up, lo = ma + mult * rng, ma - mult * rng
    price = float(df["close"].iloc[-1])
    return {"value": {"haute": _r(up.iloc[-1], digits), "moyenne": _r(ma.iloc[-1], digits),
                      "basse": _r(lo.iloc[-1], digits)},
            "series": _tail(up - lo, digits),
            "read": "prix hors canal Keltner (impulsion)" if price > up.iloc[-1] or price < lo.iloc[-1]
                    else "prix dans le canal Keltner"}


def supertrend(df, digits, period: int = 10, mult: float = 3.0, **_):
    hl2 = (df["high"] + df["low"]) / 2
    rng = _rma(_true_range(df), period)
    upper, lower = hl2 + mult * rng, hl2 - mult * rng
    close = df["close"].to_numpy()
    up_v, lo_v = upper.to_numpy(copy=True), lower.to_numpy(copy=True)
    trend = np.ones(len(df))
    line = np.full(len(df), np.nan)
    for i in range(1, len(df)):
        up_v[i] = min(up_v[i], up_v[i - 1]) if close[i - 1] <= up_v[i - 1] else up_v[i]
        lo_v[i] = max(lo_v[i], lo_v[i - 1]) if close[i - 1] >= lo_v[i - 1] else lo_v[i]
        if close[i] > up_v[i - 1]:
            trend[i] = 1
        elif close[i] < lo_v[i - 1]:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1]
        line[i] = lo_v[i] if trend[i] > 0 else up_v[i]
    s = pd.Series(line, index=df.index)
    sens = "haussier" if trend[-1] > 0 else "baissier"
    return {"value": {"supertrend": _r(s.iloc[-1], digits), "sens": sens},
            "series": _tail(s, digits),
            "read": f"Supertrend {sens} — ligne a {s.iloc[-1]:.{digits}f} "
                    f"(utilisable comme stop suiveur)"}


def vwap(df, digits, period: int = 20, **_):
    vol = df["tick_volume"] if "tick_volume" in df else pd.Series(1.0, index=df.index)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    s = (tp * vol).rolling(period).sum() / vol.rolling(period).sum().replace(0, np.nan)
    price = float(df["close"].iloc[-1])
    return {"value": {"vwap": _r(s.iloc[-1], digits), "price": _r(price, digits)},
            "series": _tail(s, digits),
            "read": f"prix {'au-dessus' if price > s.iloc[-1] else 'sous'} le VWAP{period}"}


def obv(df, digits, **_):
    vol = df["tick_volume"] if "tick_volume" in df else pd.Series(1.0, index=df.index)
    s = (np.sign(df["close"].diff().fillna(0)) * vol).cumsum()
    slope = "accumulation" if len(s) > 5 and s.iloc[-1] > s.iloc[-5] else "distribution"
    return {"value": {"obv": _r(s.iloc[-1], 0), "tendance": slope}, "series": _tail(s, 0),
            "read": f"volume cumule en {slope}"}


def ichimoku(df, digits, **_):
    conv = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
    base = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    span_a = ((conv + base) / 2).shift(26)
    span_b = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2).shift(26)
    price = float(df["close"].iloc[-1])
    top = max(float(span_a.iloc[-1]), float(span_b.iloc[-1]))
    bot = min(float(span_a.iloc[-1]), float(span_b.iloc[-1]))
    place = "au-dessus du nuage" if price > top else "sous le nuage" if price < bot else "dans le nuage"
    return {"value": {"tenkan": _r(conv.iloc[-1], digits), "kijun": _r(base.iloc[-1], digits),
                      "span_a": _r(span_a.iloc[-1], digits), "span_b": _r(span_b.iloc[-1], digits),
                      "position": place},
            "series": _tail(base, digits),
            "read": f"prix {place} (Kijun {base.iloc[-1]:.{digits}f} = support/resistance dynamique)"}


REGISTRY = {
    "sma": sma, "ema": ema, "rsi": rsi, "atr": atr, "macd": macd, "bbands": bbands,
    "bollinger": bbands, "stoch": stoch, "stochastic": stoch, "adx": adx, "cci": cci,
    "roc": roc, "momentum": roc, "donchian": donchian, "keltner": keltner,
    "supertrend": supertrend, "vwap": vwap, "obv": obv, "ichimoku": ichimoku,
}

MIN_BARS = {"ichimoku": 78, "adx": 60, "supertrend": 40}


def available() -> list[str]:
    return sorted(set(REGISTRY.keys()))


def compute(name: str, df: pd.DataFrame, digits: int = 5, **params) -> dict:
    """Calcule l'indicateur `name` sur `df`. Renvoie un dict serialisable (ou {"error": ...})."""
    key = str(name).strip().lower()
    fn = REGISTRY.get(key)
    if fn is None:
        return {"error": f"indicateur inconnu '{name}'", "disponibles": available()}
    if df is None or df.empty:
        return {"error": "pas de donnees pour ce symbole/timeframe"}
    need = max(MIN_BARS.get(key, 0), int(params.get("period", 0) or 0) * 3, 30)
    if len(df) < need:
        return {"error": f"pas assez de bougies ({len(df)} < {need}) pour {key}"}
    clean = {k: v for k, v in params.items() if v is not None}
    try:
        out = fn(df, int(digits), **clean)
    except Exception as e:                      # jamais planter le cycle pour un indicateur
        return {"error": f"calcul {key} impossible: {e}"}
    out["indicator"] = key
    out["params"] = clean
    return out
