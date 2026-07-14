# -*- coding: utf-8 -*-
r"""
V4 MACRO — entrainement + validation honnete du modele EA-A.

PROTOCOLE (pre-enregistre, ne pas modifier apres lecture des resultats) :
  1. Walk-forward 2019-2023 : pour chaque annee Y, train sur <Y, prediction Y.
     Ces predictions OOS servent UNIQUEMENT a calibrer le seuil (quantile 90
     de |score|) — jamais a juger.
  2. VALIDATION FINALE 2024-2025 : train ≤2023, seuil fige a l'etape 1.
     Criteres GO (TOUS requis) :
       - R_net moyen des trades selectionnes ≥ +0.05 R  (couts 0.04 R deduits)
       - N selectionnes ≥ 200
       - Spearman(score, r_direction) > 0 avec p < 0.05 sur TOUTES les lignes
       - au moins 1 des 2 annees positive individuellement
  3. HOLDOUT 2026 : scelle. OPEN_HOLDOUT=True une seule fois, apres GO en 2,
     et le verdict 2026 est FINAL quoi qu'il arrive.
Verdict NO-GO = l'EA-A n'existe pas. On n'itere pas des variantes de features
sur la meme validation (cf. postmortem V1 : c'est comme ca qu'on se ment).

score = P(buy) - P(sell) d'un HistGradientBoostingClassifier 3 classes.
"""
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingClassifier

ROOT = Path(__file__).parent
OPEN_HOLDOUT = False          # NE PASSER A True QU'UNE FOIS, APRES GO 2024-25

FEATS = ["rsi","atrn","trend200","emax","hour","dow","sym_c",
         "cal_s24","cal_s72","fred_dgs2","fred_curve"]
COST_R = 0.04                 # spread+commission en R (mesure V3 ~0.037)
Q_SEL  = 0.90                 # quantile de selection sur |score|

def make_model():
    return HistGradientBoostingClassifier(
        max_iter=300, learning_rate=0.06, max_leaf_nodes=31,
        min_samples_leaf=200, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.15, random_state=42)

def score_of(m, X):
    p = m.predict_proba(X)
    cls = list(m.classes_)
    return p[:, cls.index(1)] - p[:, cls.index(-1)]

def eval_block(d, thr, name):
    """Trades selectionnes = |score|>=thr ; direction = signe du score."""
    sel = d[np.abs(d["score"]) >= thr].copy()
    sel["r"] = np.where(sel["score"]>0, sel["r_buy"], sel["r_sell"]) - COST_R
    rho, pval = spearmanr(d["score"], np.where(d["score"]>0, d["r_buy"], d["r_sell"]))
    print(f"\n=== {name} ===")
    print(f"lignes={len(d)}  selection={len(sel)} ({len(sel)/max(len(d),1)*100:.1f}%)")
    if len(sel):
        print(f"R_net/trade = {sel['r'].mean():+.4f}   win={(sel['r']>0).mean()*100:.1f}%")
        by_year = sel.groupby(sel["time"].dt.year)["r"].agg(["mean","count"])
        print(by_year.rename(columns={"mean":"R_net","count":"n"}).round(4).to_string())
    print(f"Spearman(score, r_dir) = {rho:+.4f} (p={pval:.2g})")
    return sel, rho, pval

def main():
    ds = pd.read_parquet(ROOT/"dataset.parquet").dropna(subset=FEATS+["r_buy","r_sell"])
    ds["year"] = ds["time"].dt.year
    print(f"dataset: {len(ds)} lignes, features: {FEATS}")

    # --- 1. walk-forward 2019-2023 : calibration du seuil UNIQUEMENT ---
    oos = []
    for y in range(2019, 2024):
        tr, te = ds[ds.year<y], ds[ds.year==y]
        m = make_model().fit(tr[FEATS], tr["label"])
        t = te.copy(); t["score"] = score_of(m, te[FEATS])
        oos.append(t)
        print(f"  wf {y}: train={len(tr)}  oos={len(te)}")
    oos = pd.concat(oos)
    thr = float(np.quantile(np.abs(oos["score"]), Q_SEL))
    print(f"\nseuil fige (q{int(Q_SEL*100)} |score| OOS 2019-23) = {thr:.4f}")
    eval_block(oos, thr, "WALK-FORWARD 2019-2023 (calibration, ne juge pas)")

    # --- 2. VALIDATION FINALE 2024-2025 (les criteres GO s'appliquent ICI) ---
    tr = ds[ds.year<=2023]
    val = ds[ds.year.isin([2024,2025])].copy()
    m = make_model().fit(tr[FEATS], tr["label"])
    val["score"] = score_of(m, val[FEATS])
    sel, rho, pval = eval_block(val, thr, "VALIDATION FINALE 2024-2025")

    r_net = sel["r"].mean() if len(sel) else -9
    by_y  = sel.groupby(sel["time"].dt.year)["r"].mean() if len(sel) else pd.Series(dtype=float)
    crits = {
        "R_net >= +0.05":        r_net >= 0.05,
        "N >= 200":              len(sel) >= 200,
        "Spearman > 0, p<0.05":  (rho > 0) and (pval < 0.05),
        ">=1 annee positive":    bool((by_y > 0).any()) if len(by_y) else False,
    }
    print("\n=== CRITERES GO (pre-enregistres) ===")
    for k, v in crits.items(): print(f"  [{'OK' if v else 'X '}] {k}")
    go = all(crits.values())
    print(f"\nVERDICT VALIDATION : {'GO' if go else 'NO-GO'}")

    # importance grossiere : permutation sur la validation (info, pas critere)
    if len(val) > 1000:
        base = abs(spearmanr(val["score"],
                   np.where(val["score"]>0, val["r_buy"], val["r_sell"]))[0])
        print("\nfeatures (baisse de |Spearman| si permutee — info seulement):")
        rng = np.random.default_rng(0)
        for f in FEATS:
            vp = val.copy(); vp[f] = rng.permutation(vp[f].to_numpy())
            sp = score_of(m, vp[FEATS])
            d  = abs(spearmanr(sp, np.where(sp>0, vp["r_buy"], vp["r_sell"]))[0])
            print(f"  {f:<12} {base-d:+.4f}")

    # --- 3. HOLDOUT 2026 : scelle ---
    if not OPEN_HOLDOUT:
        print("\nHOLDOUT 2026 : SCELLE (OPEN_HOLDOUT=False). "
              "Ne l'ouvrir qu'apres un GO ci-dessus ; verdict final, unique.")
        return
    if not go:
        print("\nNO-GO en validation -> le holdout RESTE scelle."); return
    hold = ds[ds.year>=2026].copy()
    m2 = make_model().fit(ds[ds.year<=2025][FEATS], ds[ds.year<=2025]["label"])
    hold["score"] = score_of(m2, hold[FEATS])
    eval_block(hold, thr, "HOLDOUT 2026 (VERDICT FINAL — usage unique)")

if __name__ == "__main__":
    main()
