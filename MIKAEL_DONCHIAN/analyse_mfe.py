# -*- coding: utf-8 -*-
"""
analyse_mfe.py — analyse du journal MFE/MAE de MIKAEL_DONCHIAN (v2.12+).

Usage :
    python analyse_mfe.py MIKAEL_DONCHIAN_mfe_20260713.csv        # tout
    python analyse_mfe.py MIKAEL_DONCHIAN_mfe_20260713.csv fx     # paires FX seules
    python analyse_mfe.py MIKAEL_DONCHIAN_mfe_20260713.csv idx    # indices/CFD seuls
    (instance mixte : TOUJOURS calibrer les seuils par famille, pas melanges —
     l'ATR d'un indice et d'une paire FX ne vivent pas dans le meme regime)

Entree : CSV ecrit par l'EA, colonnes :
    close_time;symbol;pl;mfe_atr;mae_atr;dur_min;reason
    (mfe_atr >= 0 = meilleur point atteint en xATR ; mae_atr <= 0 = pire point ;
     reason = sl / tp / so / ea_close / close)

Sorties (texte) :
  1. Vue d'ensemble (n, win rate, PF sur pl nets).
  2. Distributions MFE/MAE gagnants vs perdants (quantiles).
  3. CALIBRATION ZERO-LOCK : pour chaque seuil z, part des gagnants TP dont la
     MFE >= z vs part des perdants — la separation dit ou placer le seuil.
  4. SCRATCHES : trades sortis ~0 (BE) — combien, cout net cumule (commissions).
  5. ROUND-TRIPS : trades montes >= 1.0 ATR SANS finir TP (calibration trail-start).
  6. NO-PROGRESS : MAE des gagnants (jusqu'ou les futurs gagnants plongent) —
     garde-fou avant tout exit "coupe si adverse".

HONNETETE STATISTIQUE : en dessous de ~30 lignes, les chiffres sont indicatifs.
Le script n'est PAS un simulateur de chemin : il ne sait pas dans quel ordre
MFE/MAE ont ete atteints. Il eclaire les seuils, il ne les prouve pas.
"""
import csv
import sys
from statistics import median


def quantiles(xs, qs=(0.1, 0.25, 0.5, 0.75, 0.9)):
    if not xs:
        return {q: float("nan") for q in qs}
    s = sorted(xs)
    out = {}
    for q in qs:
        i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        out[q] = s[i]
    return out


def fmt_q(d):
    return "  ".join(f"p{int(q*100):02d}={v:+.2f}" for q, v in d.items())


MAJORS = {"USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF",
          "SEK", "NOK", "DKK", "SGD", "MXN", "ZAR", "PLN", "CZK", "HUF"}


def is_fx(sym):
    # miroir de IsFxPair() de l'EA : 6 lettres, deux devises connues
    return len(sym) >= 6 and sym[:3] in MAJORS and sym[3:6] in MAJORS


def main(path, family=None):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            try:
                rows.append({
                    "time": r["close_time"], "sym": r["symbol"],
                    "pl": float(r["pl"]), "mfe": float(r["mfe_atr"]),
                    "mae": float(r["mae_atr"]), "dur": int(r["dur_min"]),
                    "reason": r["reason"].strip(),
                })
            except (KeyError, ValueError):
                continue  # ligne partielle/corrompue : ignoree

    if family == "fx":
        rows = [r for r in rows if is_fx(r["sym"])]
    elif family == "idx":
        rows = [r for r in rows if not is_fx(r["sym"])]

    n = len(rows)
    if n == 0:
        print("Aucune ligne exploitable."); return
    fam = {"fx": " (FX seulement)", "idx": " (indices/CFD seulement)"}.get(family, "")
    print(f"=== ANALYSE MFE/MAE : {n} trades clos{fam} ===")
    nfx = sum(1 for r in rows if is_fx(r["sym"]))
    if family is None and 0 < nfx < n:
        print(f"!! echantillon MIXTE ({nfx} FX / {n - nfx} non-FX) : relancer avec"
              f" 'fx' puis 'idx' pour calibrer les seuils par famille.")
    if n < 30:
        print(f"!! n={n} < 30 : INDICATIF seulement, ne pas decider la-dessus.\n")

    wins = [r for r in rows if r["pl"] > 0]
    loss = [r for r in rows if r["pl"] <= 0]
    tp = [r for r in rows if r["reason"] == "tp"]
    gp = sum(r["pl"] for r in wins)
    gl = -sum(r["pl"] for r in loss)
    print(f"P/L net total : {gp - gl:+.2f}$ | gagnants {len(wins)} / perdants {len(loss)}"
          f" | PF={gp/gl if gl > 0 else float('inf'):.2f}")
    print(f"Sorties : " + ", ".join(f"{k}={sum(1 for r in rows if r['reason']==k)}"
          for k in sorted({r["reason"] for r in rows})))

    print("\n--- Distributions (en xATR) ---")
    print(f"MFE gagnants : {fmt_q(quantiles([r['mfe'] for r in wins]))}")
    print(f"MFE perdants : {fmt_q(quantiles([r['mfe'] for r in loss]))}")
    print(f"MAE gagnants : {fmt_q(quantiles([r['mae'] for r in wins]))}")
    print(f"MAE perdants : {fmt_q(quantiles([r['mae'] for r in loss]))}")

    print("\n--- CALIBRATION ZERO-LOCK : P(MFE >= z) ---")
    print("  (bon seuil = beaucoup de gagnants au-dessus, peu de perdants ;")
    print("   un z trop bas scratche des trades qui seraient devenus gagnants)")
    print(f"  {'z':>5} | {'gagnants':>9} | {'perdants':>9} | ecart")
    for z in (0.10, 0.20, 0.30, 0.40, 0.50, 0.75, 1.00):
        pw = sum(1 for r in wins if r["mfe"] >= z) / len(wins) if wins else 0
        pl_ = sum(1 for r in loss if r["mfe"] >= z) / len(loss) if loss else 0
        print(f"  {z:5.2f} | {pw:8.0%} | {pl_:8.0%} | {pw - pl_:+.0%}")

    scr = [r for r in rows if r["reason"] == "sl" and abs(r["pl"]) < 60]
    print(f"\n--- SCRATCHES BE (sortie [sl], |pl| < 60$) ---")
    print(f"  {len(scr)}/{n} trades ({len(scr)/n:.0%}) | cout net cumule "
          f"{sum(r['pl'] for r in scr):+.2f}$ | pl median {median([r['pl'] for r in scr]) if scr else 0:+.2f}$")
    print("  (si ce poste est un gros % ET nettement negatif -> zero-lock trop serre)")

    rt = [r for r in rows if r["mfe"] >= 1.0 and r["reason"] != "tp"]
    print(f"\n--- ROUND-TRIPS (MFE >= 1.0 ATR sans finir TP) ---")
    print(f"  {len(rt)}/{n} trades | pl net cumule {sum(r['pl'] for r in rt):+.2f}$")
    for r in rt:
        print(f"    {r['time']} {r['sym']:7s} pl={r['pl']:+9.2f} mfe={r['mfe']:+.2f} ({r['reason']})")
    print("  (s'il y en a beaucoup -> argument DONNEES pour trail-start plus bas)")

    if wins:
        deep = sum(1 for r in wins if r["mae"] <= -0.5) / len(wins)
        print(f"\n--- NO-PROGRESS / MAE des gagnants ---")
        print(f"  {deep:.0%} des gagnants sont passes sous -0.50 ATR avant de gagner.")
        print("  (plus ce % est haut, plus un exit 'coupe si adverse' tuerait de gagnants)")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] not in ("fx", "idx")):
        print(__doc__); sys.exit(1)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)
