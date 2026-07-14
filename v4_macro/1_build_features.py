# -*- coding: utf-8 -*-
r"""
V4 MACRO — dataset features+labels pour le modele EA-A.

Grille = bougies H4 fermees des 8 paires (feed FTMO, 2015-06 -> present).
A chaque bougie H4 (open t, close t+4h), decision a la CLOTURE :

FEATURES (info strictement disponible a t+4h) :
  prix      : rsi14, atr_norm, trend200 (close/sma200-1), emax (ema8-ema21 en ATR),
              hour, dow, sym_c
  calendrier: surprise ponderee par importance, z-score EXPANSIF par event_id
              (la normalisation n'utilise QUE les surprises passees du meme
              event — un z sur std pleine-histoire serait du lookahead),
              fenetres 24h/72h, par devise -> paire = base - quote
  FRED      : momentum 5j du taux 2 ans US (DGS2) + pente 10a-2a, decales
              d'1 jour ouvre (delai de publication), signes par cote USD

LABELS (triple-barriere IDENTIQUE a l'EA / V3) :
  entree = open du 1er H1 apres la cloture H4 ; SL pips par paire, TP=1.70xSL,
  time-stop 168 H1 ; egalite intra-bougie = SL prioritaire.
  label +1 = buy gagne / -1 = sell gagne / 0 = chop.

Sortie : v4_macro/dataset.parquet (+ caches h4/h1 parquet).
Splits (train ≤2023 / val 2024-25 / HOLDOUT 2026 scelle) -> geres par 2_train.py.
"""
import os
import datetime as dt
import numpy as np
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
CACHE = ROOT / "data"; CACHE.mkdir(exist_ok=True)
TERM_FILES = Path(r"C:\Users\mbula\AppData\Roaming\MetaQuotes\Terminal"
                  r"\D0E8209F77C8CF37AD8BF550E51FF075\MQL5\Files")
load_dotenv(ROOT.parent / ".env")
FRED_KEY = os.environ.get("FRED_API", "").strip()

SYMBOLS = ["AUDJPY","AUDUSD","EURJPY","EURUSD","GBPJPY","GBPUSD","NZDUSD","USDJPY"]
SL_PIPS = {"AUDJPY":38,"AUDUSD":28,"EURJPY":48,"EURUSD":32,
           "GBPJPY":59,"GBPUSD":42,"NZDUSD":28,"USDJPY":39}
SYM_C   = {s:i for i,s in enumerate(SYMBOLS)}
CCYS    = ["EUR","USD","JPY","GBP","AUD","NZD","CAD","CHF"]
RR, TIME_STOP = 1.70, 168
T0, T1 = dt.datetime(2015,6,1), dt.datetime(2026,7,15)

# ------------------------------------------------------------- 1. bougies MT5
def fetch_rates():
    """H4 (grille de decision) + H1 (resolution des barrieres), feed FTMO.
    Caches en parquet : relancer le script ne re-telecharge pas."""
    fh4, fh1 = CACHE/"h4.parquet", CACHE/"h1.parquet"
    if fh4.exists() and fh1.exists():
        return pd.read_parquet(fh4), pd.read_parquet(fh1)
    import MetaTrader5 as mt5
    assert mt5.initialize(), mt5.last_error()
    ai = mt5.account_info()
    assert "FTMO" in ai.server, f"feed inattendu: {ai.server}"
    out = {}
    for tf, name in [(mt5.TIMEFRAME_H4,"h4"), (mt5.TIMEFRAME_H1,"h1")]:
        frames = []
        for s in SYMBOLS:
            mt5.symbol_select(s, True)
            r = mt5.copy_rates_range(s, tf, T0, T1)
            assert r is not None and len(r), f"{s} {name}: vide"
            df = pd.DataFrame(r)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df["symbol"] = s
            frames.append(df[["symbol","time","open","high","low","close"]])
            print(f"  {s} {name}: {len(df)} bougies  {df.time.min().date()} -> {df.time.max().date()}")
        out[name] = pd.concat(frames, ignore_index=True)
    mt5.shutdown()
    out["h4"].to_parquet(fh4); out["h1"].to_parquet(fh1)
    return out["h4"], out["h1"]

# --------------------------------------------------- 2. surprises calendrier
def calendar_events():
    """Evenements avec surprise z-normalisee EXPANSIVE (min 8 obs passees).
    Les temps du calendrier MT5 = heure SERVEUR, comme les bougies -> aucune
    conversion de fuseau necessaire (meme horloge des deux cotes)."""
    df = pd.read_csv(TERM_FILES/"calendar_history.csv", sep=";", encoding="cp1252",
                     usecols=range(9), na_values=[""], engine="python",
                     on_bad_lines="skip")
    for col in ("actual","forecast","importance"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M", errors="coerce")
    ev = df.dropna(subset=["time","actual","forecast","importance"]).copy()
    ev = ev[ev["currency"].isin(CCYS)].sort_values("time").reset_index(drop=True)
    ev["surp"] = ev["actual"] - ev["forecast"]
    # z-score EXPANSIF par event_id : std des surprises STRICTEMENT anterieures
    g = ev.groupby("event_id")["surp"]
    ev["std_past"] = g.transform(lambda s: s.expanding().std(ddof=0).shift(1))
    ev["n_past"]   = g.cumcount()
    ev["z"] = np.where((ev["n_past"]>=8) & (ev["std_past"]>0),
                       (ev["surp"]/ev["std_past"]).clip(-4,4), np.nan)
    ev = ev.dropna(subset=["z"])
    print(f"  calendrier: {len(ev)} surprises z-normalisees "
          f"({ev.time.min().date()} -> {ev.time.max().date()})")
    return ev[["time","currency","importance","z"]]

def ccy_surprise_at(times: pd.Series, ev: pd.DataFrame, hours: int) -> pd.DataFrame:
    """Pour chaque instant t (cloture H4) : moyenne ponderee par importance des
    z de CHAQUE devise sur [t-hours, t]. Vectorise par cumsum + searchsorted."""
    out = pd.DataFrame(index=times.index)
    tvals = times.to_numpy()
    for c in CCYS:
        e = ev[ev["currency"]==c]
        et = e["time"].to_numpy()
        wz = (e["z"]*e["importance"]).to_numpy()
        w  = e["importance"].to_numpy()
        cwz = np.concatenate([[0.0], np.cumsum(wz)])
        cw  = np.concatenate([[0.0], np.cumsum(w)])
        hi = np.searchsorted(et, tvals, side="right")       # events <= t
        lo = np.searchsorted(et, tvals - np.timedelta64(hours,"h"), side="right")
        sw = cw[hi]-cw[lo]
        out[c] = np.where(sw>0, (cwz[hi]-cwz[lo])/np.where(sw>0,sw,1), 0.0)
    return out

# --------------------------------------------------------------------- 3. FRED
def fred_series(series_id: str) -> pd.Series:
    import requests
    r = requests.get("https://api.stlouisfed.org/fred/series/observations",
        params={"series_id":series_id,"api_key":FRED_KEY,"file_type":"json",
                "observation_start":"2015-01-01"}, timeout=30)
    obs = r.json()["observations"]
    s = pd.Series({pd.Timestamp(o["date"]): float(o["value"])
                   for o in obs if o["value"] not in (".","")}).sort_index()
    return s

def fred_features() -> pd.DataFrame:
    """dgs2_mom5 (variation 5j du 2 ans US) et curve_mom5 (pente 10a-2a),
    DECALES d'1 jour ouvre : la valeur du jour J n'est publiee que J+1."""
    f = CACHE/"fred.parquet"
    if f.exists(): return pd.read_parquet(f)
    dgs2, dgs10 = fred_series("DGS2"), fred_series("DGS10")
    d = pd.DataFrame({"dgs2":dgs2,"dgs10":dgs10}).dropna()
    d["dgs2_mom5"]  = d["dgs2"].diff(5)
    d["curve_mom5"] = (d["dgs10"]-d["dgs2"]).diff(5)
    d = d.shift(1, freq="B")[["dgs2_mom5","curve_mom5"]].dropna()  # delai publication
    d.to_parquet(f)
    print(f"  FRED: {len(d)} jours ({d.index.min().date()} -> {d.index.max().date()})")
    return d

# ----------------------------------------------------------- 4. features prix
def price_features(g: pd.DataFrame) -> pd.DataFrame:
    c = g["close"]
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rsi = 100 - 100/(1+gain/loss.replace(0,np.nan))
    tr = pd.concat([g["high"]-g["low"],
                    (g["high"]-c.shift()).abs(),
                    (g["low"] -c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    sma200 = c.rolling(200).mean()
    ema8, ema21 = c.ewm(span=8, adjust=False).mean(), c.ewm(span=21, adjust=False).mean()
    return pd.DataFrame({
        "rsi": rsi, "atrn": atr/c, "trend200": c/sma200 - 1,
        "emax": (ema8-ema21)/atr,
        "hour": g["time"].dt.hour, "dow": g["time"].dt.dayofweek,
    })

# -------------------------------------------------- 5. labels triple-barriere
def label_h1(g: pd.DataFrame, sym: str):
    """Labels V3 sur la grille H1 : entree open(i+1), fenetres forward 168.
    Retourne (label, r_buy, r_sell) — R REEL des deux cotes, time-stop inclus
    (sortie au close de entry+168 H1, en multiples de SL), pour que la
    validation mesure la vraie distribution et pas une approximation."""
    pip = 0.01 if sym.endswith("JPY") else 0.0001
    sl_d, tp_d = SL_PIPS[sym]*pip, RR*SL_PIPS[sym]*pip
    o,h,l,c = (g[k].to_numpy(np.float64) for k in ("open","high","low","close"))
    n = len(g)
    entry = np.roll(o,-1); entry[-1]=np.nan
    pad = np.full(TIME_STOP, np.nan)
    Hf = np.lib.stride_tricks.sliding_window_view(np.concatenate([h[1:],pad]), TIME_STOP)[:n]
    Lf = np.lib.stride_tricks.sliding_window_view(np.concatenate([l[1:],pad]), TIME_STOP)[:n]
    def first_idx(cond):
        return np.where(cond.any(axis=1), cond.argmax(axis=1), TIME_STOP+1)
    e = entry[:,None]
    buy_tp,  buy_sl  = first_idx(Hf>=e+tp_d), first_idx(Lf<=e-sl_d)
    sell_tp, sell_sl = first_idx(Lf<=e-tp_d), first_idx(Hf>=e+sl_d)
    lab = np.zeros(n, np.int8)
    lab[(buy_tp <buy_sl ) & (buy_tp <TIME_STOP)] =  1   # egalite -> SL prioritaire
    lab[(sell_tp<sell_sl) & (sell_tp<TIME_STOP)] = -1

    # close a l'expiration du time-stop (dernier H1 de la fenetre forward)
    idx_exit = np.minimum(np.arange(n)+TIME_STOP, n-1)
    exit_px = c[idx_exit]
    ts_r_buy  = np.clip((exit_px-entry)/sl_d, -1, RR)   # borne par les barrieres
    ts_r_sell = np.clip((entry-exit_px)/sl_d, -1, RR)

    r_buy = np.where((buy_tp<buy_sl)&(buy_tp<TIME_STOP), RR,
             np.where((buy_sl<=buy_tp)&(buy_sl<TIME_STOP), -1.0, ts_r_buy))
    r_sell= np.where((sell_tp<sell_sl)&(sell_tp<TIME_STOP), RR,
             np.where((sell_sl<=sell_tp)&(sell_sl<TIME_STOP), -1.0, ts_r_sell))
    return lab, r_buy, r_sell

# --------------------------------------------------------------------- build
def main():
    print("1) bougies MT5 (cache data/)...")
    h4, h1 = fetch_rates()
    print("2) calendrier...")
    ev = calendar_events()
    print("3) FRED...")
    fred = fred_features()

    print("4) assemblage par symbole...")
    rows = []
    for sym in SYMBOLS:
        g4 = h4[h4.symbol==sym].sort_values("time").reset_index(drop=True)
        g1 = h1[h1.symbol==sym].sort_values("time").reset_index(drop=True)

        d = price_features(g4)
        d["symbol"], d["sym_c"], d["time"] = sym, SYM_C[sym], g4["time"]
        d["close_t"] = g4["time"] + pd.Timedelta(hours=4)      # cloture = decision

        # calendrier : surprises par devise a la cloture, paire = base - quote
        for hrs, tag in [(24,"s24"),(72,"s72")]:
            cs = ccy_surprise_at(d["close_t"], ev, hrs)
            d[f"cal_{tag}"] = cs[sym[:3]].to_numpy() - cs[sym[3:]].to_numpy()

        # FRED : as-of a la date de decision, signe par le cote USD de la paire
        fr = fred.reindex(fred.index.union(pd.DatetimeIndex(d["close_t"].dt.normalize().unique()))
                          ).ffill().reindex(d["close_t"].dt.normalize()).reset_index(drop=True)
        usd_side = 1.0 if sym[:3]=="USD" else (-1.0 if sym[3:]=="USD" else 0.0)
        d["fred_dgs2"]  = fr["dgs2_mom5"].to_numpy()  * usd_side
        d["fred_curve"] = fr["curve_mom5"].to_numpy() * usd_side

        # labels : H1 dont l'open == derniere heure de la bougie H4
        # (decision a close_t ; entree = open du H1 suivant = open(i+1) du H1 i)
        lab, r_buy, r_sell = label_h1(g1,sym)
        h1_idx = d["close_t"] - pd.Timedelta(hours=1)
        d["label"]  = pd.Series(lab,    index=g1["time"]).reindex(h1_idx).to_numpy()
        d["r_buy"]  = pd.Series(r_buy,  index=g1["time"]).reindex(h1_idx).to_numpy()
        d["r_sell"] = pd.Series(r_sell, index=g1["time"]).reindex(h1_idx).to_numpy()

        rows.append(d)
    ds = pd.concat(rows, ignore_index=True).dropna(subset=["label","rsi","trend200"])
    ds["label"] = ds["label"].astype(np.int8)

    out = ROOT/"dataset.parquet"
    ds.to_parquet(out)
    print(f"\ndataset: {len(ds)} lignes  {ds.time.min().date()} -> {ds.time.max().date()}")
    print(ds.groupby("label").size().rename("n").to_string())
    print(f"couverture calendrier: cal_s72 != 0 sur "
          f"{(ds.cal_s72!=0).mean()*100:.1f}% des lignes")
    print(f"-> {out}")

if __name__ == "__main__":
    main()
