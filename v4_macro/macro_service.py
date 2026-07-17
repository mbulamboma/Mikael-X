# -*- coding: utf-8 -*-
r"""
macro_service.py — le "cerveau data" unique des EA MIKAEL (v4).

Sources -> scores PAR DEVISE (8 majeures) -> MQL5\Files\macro_features.csv
  1. Calendrier MT5 (calendar_history.csv)  : surprises normalisees 72h
  2. FRED (cle .env FRED_API)               : momentum taux 2 ans US
  3. GDELT (gratuit, sans cle)              : titres de news 72h par devise
  4. Alpha Vantage (--av, cle .env)         : complement news (quota 25/j !)
  5. FinBERT (ProsusAI/finbert, local CPU)  : titre -> score [-1,+1], cache

ANTI-LOOKAHEAD : le service n'ecrit que des donnees deja publiees a l'instant
du run ; chaque run est aussi APPENDE dans history/ -> c'est le dataset
forward honnete qui servira a valider (jamais de re-calcul retroactif).

Usage :
  python macro_service.py            # un run complet -> ecrit le CSV
  python macro_service.py --loop 30  # boucle toutes les 30 min
  python macro_service.py --no-news  # sans GDELT/FinBERT (calendrier+FRED)
  python macro_service.py --av       # ajoute Alpha Vantage (economise le quota)
"""
import argparse, hashlib, json, os, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT      = Path(__file__).resolve().parent
TRADING   = ROOT.parent

def _resolve_mt5_files():
    """Dossier MQL5\\Files du terminal MT5.
    1) $MT5_FILES si defini et existant.
    2) sinon on scanne %APPDATA%\\MetaQuotes\\Terminal\\<id>\\MQL5\\Files :
       on prefere le terminal qui contient deja des fichiers MIKAEL_* (le live),
       sinon le plus recemment modifie. Rend le service portable (VPS = autre id
       que la machine de dev) et evite l'erreur 'non-existent directory'."""
    env = os.environ.get("MT5_FILES", "").strip()
    if env and Path(env).is_dir():
        return Path(env)
    base = Path(os.environ.get("APPDATA", r"C:\Users\%s\AppData\Roaming"
                               % os.environ.get("USERNAME", ""))) / "MetaQuotes" / "Terminal"
    cands = [p for p in base.glob("*/MQL5/Files") if p.is_dir()] if base.is_dir() else []
    if not cands:
        raise RuntimeError("Aucun dossier MT5 MQL5\\Files trouve sous %s "
                           "-- definir MT5_FILES." % base)
    def score(p):
        has_mikael = any(p.glob("MIKAEL_*"))
        return (has_mikael, p.stat().st_mtime)
    return max(cands, key=score)

TERM_FILES= _resolve_mt5_files()
OUT_CSV   = TERM_FILES / "macro_features.csv"
HIST_DIR  = ROOT / "history";  HIST_DIR.mkdir(exist_ok=True)
CACHE_DIR = ROOT / "cache";    CACHE_DIR.mkdir(exist_ok=True)
CACHE_FILE= CACHE_DIR / "finbert_cache.json"

load_dotenv(TRADING / ".env")
FRED_KEY = os.environ.get("FRED_API", "").strip()
AV_KEY   = os.environ.get("ALPHA_VANTAGE_API", "").strip()

CCYS = ["EUR","USD","JPY","GBP","AUD","NZD","CAD","CHF"]

# requetes GDELT par devise (anglais uniquement, banques centrales + devise)
# regle GDELT : mots SEULS sans guillemets, phrases multi-mots avec guillemets
GDELT_Q = {
 "EUR": '(ECB OR "European Central Bank" OR eurozone)',
 "USD": '("Federal Reserve" OR "US inflation" OR "US economy")',
 "JPY": '("Bank of Japan" OR "japanese yen")',
 "GBP": '("Bank of England" OR "pound sterling")',
 "AUD": '("Reserve Bank of Australia" OR "australian dollar")',
 "NZD": '(RBNZ OR "new zealand dollar")',
 "CAD": '("Bank of Canada" OR "canadian dollar")',
 "CHF": '("Swiss National Bank" OR "swiss franc")',
}

def log(msg): print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)

# ---------------------------------------------------------------- calendrier
def calendar_surprises(now_utc: datetime, hours: int = 72) -> dict:
    """Surprise normalisee par devise sur 72h (0.0 si rien / fichier absent).
    surprise brute = actual - forecast ; normalisee par l'ecart-type HISTORIQUE
    des surprises du MEME event_id (>=8 obs), ponderee par l'importance (1-3).
    Le temps du calendrier MT5 est en heure SERVEUR (EET, ~UTC+2/3) : marge
    prise en travaillant en fenetres larges (72h)."""
    path = TERM_FILES / "calendar_history.csv"
    out = {c: 0.0 for c in CCYS}
    if not path.exists():
        log("calendrier ABSENT — surprises=0 (re-exporter ExportCalendar.mq5)")
        return out
    df = pd.read_csv(path, sep=";", encoding="cp1252", usecols=range(9),
                     na_values=[""], engine="python", on_bad_lines="skip")
    # colonnes numeriques forcees (des noms d'events contenant ';' peuvent
    # decaler des lignes -> to_numeric(coerce) les neutralise proprement)
    for col in ("actual","forecast","importance"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["importance"])
    df["time"] = pd.to_datetime(df["time"], format="%Y.%m.%d %H:%M", errors="coerce")
    df = df.dropna(subset=["time"])
    df["time_utc"] = df["time"] - pd.Timedelta(hours=2)   # EET approx -> UTC
    ev = df.dropna(subset=["actual","forecast"]).copy()
    ev["surp"] = ev["actual"] - ev["forecast"]
    # normalisation par event_id (echelle comparable entre indicateurs)
    std = ev.groupby("event_id")["surp"].transform(
              lambda s: s.std(ddof=0) if len(s) >= 8 else float("nan"))
    ev["z"] = (ev["surp"] / std).clip(-4, 4)
    ev = ev.dropna(subset=["z"])
    lo = now_utc.replace(tzinfo=None) - timedelta(hours=hours)
    win = ev[(ev["time_utc"] > lo) & (ev["time_utc"] <= now_utc.replace(tzinfo=None))]
    age = df["time_utc"].max()
    if age < now_utc.replace(tzinfo=None) - timedelta(days=7):
        log(f"calendrier PERIME (derniere ligne {age:%Y-%m-%d}) — re-exporter !")
    for c in CCYS:
        w = win[win["currency"] == c]
        if len(w):
            out[c] = float((w["z"] * w["importance"]).sum() / w["importance"].sum())
    return out

# ---------------------------------------------------------------------- FRED
def _fred_last(series_id: str) -> list:
    r = requests.get("https://api.stlouisfed.org/fred/series/observations",
        params={"series_id":series_id,"api_key":FRED_KEY,"file_type":"json",
                "sort_order":"desc","limit":15}, timeout=20)
    return [float(o["value"]) for o in r.json()["observations"]
            if o["value"] not in (".","")]

def fred_usd_momentum() -> tuple:
    """(dgs2_mom5, curve_mom5) : variation 5 jours ouvres du taux 2 ans US
    et de la pente 10a-2a — memes definitions que 1_build_features.py."""
    if not FRED_KEY:
        return 0.0, 0.0
    try:
        d2, d10 = _fred_last("DGS2"), _fred_last("DGS10")
        n = min(len(d2), len(d10))
        if n <= 5: return 0.0, 0.0
        curve = [d10[i]-d2[i] for i in range(n)]
        return round(d2[0]-d2[5], 4), round(curve[0]-curve[5], 4)
    except Exception as e:
        log(f"FRED erreur: {e}"); return 0.0, 0.0

# --------------------------------------------------------------------- news
def gdelt_titles(ccy: str) -> list:
    """Titres GDELT des 72 dernieres heures pour une devise. [(dt_utc, titre)]
    GDELT rate-limite (~1 req/5s) : pause systematique + 2 retries."""
    backoff=[6, 25, 50]                     # le throttle GDELT exige de la patience
    for attempt in range(3):
        time.sleep(backoff[attempt])        # respect du rate-limit AVANT chaque appel
        try:
            r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc",
                params={"query": f"{GDELT_Q[ccy]} sourcelang:english",
                        "mode":"artlist","maxrecords":75,"timespan":"3d",
                        "format":"json"}, timeout=30)
            if not r.text.lstrip().startswith("{"):   # HTML de throttle -> retry
                log(f"GDELT {ccy} throttle (tentative {attempt+1}/3)")
                continue
            arts = r.json().get("articles", [])
            out = []
            for a in arts:
                try:
                    dt = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ")
                    out.append((dt, a["title"].strip()))
                except Exception:
                    continue
            return out
        except Exception as e:
            log(f"GDELT {ccy} erreur: {e} (tentative {attempt+1}/3)")
            time.sleep(10)
    return []

def av_titles() -> dict:
    """Alpha Vantage NEWS_SENTIMENT (1 requete pour tout le forex).
    Retourne {ccy:[(dt,titre)]} — appele seulement avec --av (quota 25/j)."""
    out = {c: [] for c in CCYS}
    if not AV_KEY: return out
    try:
        r = requests.get("https://www.alphavantage.co/query",
            params={"function":"NEWS_SENTIMENT","topics":"forex",
                    "limit":200,"apikey":AV_KEY}, timeout=30)
        for it in r.json().get("feed", []):
            dt = datetime.strptime(it["time_published"][:13], "%Y%m%dT%H%M")
            title = it["title"].strip()
            blob = (title + " " + it.get("summary","")).upper()
            for c in CCYS:
                if c in blob: out[c].append((dt, title))
    except Exception as e:
        log(f"AlphaVantage erreur: {e}")
    return out

# ------------------------------------------------------------------ FinBERT
_tok = _mdl = None
def _load_finbert():
    global _tok, _mdl
    if _mdl is None:
        log("chargement FinBERT (1er run = telechargement ~440 Mo)...")
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        _tok = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        _mdl = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")
        _mdl.eval()

def finbert_scores(titles: list) -> list:
    """Score signe [-1,+1] par titre (P(pos)-P(neg)). Cache par hash de texte :
    un titre n'est JAMAIS re-note (determinisme + vitesse)."""
    cache = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
    todo = [t for t in titles if hashlib.md5(t.encode()).hexdigest() not in cache]
    if todo:
        _load_finbert()
        import torch
        with torch.no_grad():
            for i in range(0, len(todo), 32):
                batch = todo[i:i+32]
                x = _tok(batch, padding=True, truncation=True, max_length=64,
                         return_tensors="pt")
                p = _mdl(**x).logits.softmax(-1)          # [pos, neg, neu]
                for t, row in zip(batch, p):
                    cache[hashlib.md5(t.encode()).hexdigest()] = round(
                        float(row[0] - row[1]), 4)
        CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    return [cache[hashlib.md5(t.encode()).hexdigest()] for t in titles]

# --------------------------------------------------------------------- main
def run_once(use_news=True, use_av=False):
    now = datetime.now(timezone.utc)
    log("=== run macro_service ===")
    surp   = calendar_surprises(now, 72)
    surp24 = calendar_surprises(now, 24)
    fmom, curvemom = fred_usd_momentum()
    av    = av_titles() if (use_av and use_news) else {c: [] for c in CCYS}

    # memoire des derniers scores valides : un throttle GDELT passager ne doit
    # pas remettre le sentiment a zero (on garde le dernier bon < 24h)
    last_file = CACHE_DIR / "last_scores.json"
    last = json.loads(last_file.read_text()) if last_file.exists() else {}

    rows = []
    for c in CCYS:
        s24 = s72 = mom = 0.0; n24 = 0; fetched = False
        if use_news:
            items = gdelt_titles(c) + av[c]
            # dedoublonnage par titre
            seen = {}; items = [seen.setdefault(t, (dt, t)) for dt, t in items
                                if t not in seen]
            if items:
                fetched = True
                dts, titles = zip(*items)
                scores = finbert_scores(list(titles))
                d = pd.DataFrame({"dt": dts, "s": scores})
                lo24 = now.replace(tzinfo=None) - timedelta(hours=24)
                s72 = float(d["s"].mean())
                d24 = d[d["dt"] >= lo24]
                n24 = len(d24)
                s24 = float(d24["s"].mean()) if n24 else s72
                mom = round(s24 - s72, 4)
                last[c] = {"s24": s24, "s72": s72, "mom": mom, "n24": n24,
                           "ts": now.isoformat()}
        if use_news and not fetched and c in last:      # repli : dernier bon score
            age_h = (now - datetime.fromisoformat(last[c]["ts"])).total_seconds()/3600
            if age_h <= 24:
                s24, s72 = last[c]["s24"], last[c]["s72"]
                mom, n24 = last[c]["mom"], last[c]["n24"]
                log(f"  {c}: fetch vide -> reprise du score de {age_h:.1f}h")
        rows.append({"ccy": c, "sent24": round(s24,4), "sent72": round(s72,4),
                     "sentmom": mom, "cnt24": n24,
                     "surprise24": round(surp24[c],4),
                     "surprise72": round(surp[c],4),
                     "fredmom": fmom if c == "USD" else 0.0,
                     "curvemom": curvemom if c == "USD" else 0.0,
                     "updated_utc": now.strftime("%Y-%m-%d %H:%M")})
        log(f"  {c}: sent24={s24:+.3f} (n={n24})  surprise72={surp[c]:+.3f}")
    last_file.write_text(json.dumps(last))

    df = pd.DataFrame(rows)
    # 1) fichier lu par les EA (atomique : tmp puis replace)
    tmp = OUT_CSV.with_suffix(".tmp")
    df.to_csv(tmp, sep=";", index=False)
    tmp.replace(OUT_CSV)
    log(f"ecrit -> {OUT_CSV}")
    # 2) DATASET FORWARD : append horodate (jamais reecrit) — c'est lui qui
    #    permettra une validation honnete dans quelques semaines
    hist = HIST_DIR / f"macro_hist_{now:%Y%m}.csv"
    df.to_csv(hist, sep=";", index=False, mode="a", header=not hist.exists())
    log(f"append -> {hist.name}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, metavar="MIN",
                    help="boucle toutes les MIN minutes (0 = un seul run)")
    ap.add_argument("--no-news", action="store_true", help="sans GDELT/FinBERT")
    ap.add_argument("--av", action="store_true",
                    help="ajoute Alpha Vantage (quota gratuit 25 req/jour)")
    a = ap.parse_args()
    while True:
        try:
            run_once(use_news=not a.no_news, use_av=a.av)
        except Exception as e:
            log(f"ERREUR RUN: {e}")
        if not a.loop: break
        log(f"prochain run dans {a.loop} min"); time.sleep(a.loop*60)
