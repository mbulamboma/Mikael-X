# -*- coding: utf-8 -*-
r"""
V4 — export ONNX du modele macro pour l'EA MIKAEL_MACRO (forward-test DEMO).

⚠️ CONTEXTE HONNETE : la famille de modeles est NO-GO en validation
(R_net -0.014 apres couts ; Spearman +0.044 reel mais insuffisant).
Export UNIQUEMENT pour tester en FORWARD DEMO l'hypothese « modele + veto
FinBERT > 0 », in-testable en backtest (pas d'historique sentiment).

skl2onnx est CASSE dans cet environnement (parite regressor ET classifier
fausse a 0.3-0.5 pres — verifie le 14 juil 2026). Cet exporteur construit donc
le graphe TreeEnsembleRegressor A LA MAIN depuis les arbres sklearn :
controle total, parite exigee EXACTE (<1e-5) sur 5000 echantillons.

Formulation REGRESSION (= V1, dont MT5 executait deja le meme op) :
  cible = label numerique (-1/0/+1) ; score = prediction continue ;
  long si score>0, trade si |score| >= seuil (q90 walk-forward recalcule
  ci-dessous pour CETTE formulation + rapport de validation honnete).
- Entrainement final : ≤2025 (holdout 2026 JAMAIS inclus).
"""
import json
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
import onnx
from onnx import helper, TensorProto
import onnxruntime as ort

ROOT = Path(__file__).parent
OUT  = ROOT.parent / "MIKAEL_MACRO"; OUT.mkdir(exist_ok=True)
FEATS = ["rsi","atrn","trend200","emax","hour","dow","sym_c",
         "cal_s24","cal_s72","fred_dgs2","fred_curve"]
COST_R, Q_SEL = 0.04, 0.90

def make_model():
    return HistGradientBoostingRegressor(
        max_iter=300, learning_rate=0.06, max_leaf_nodes=31,
        min_samples_leaf=200, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.15, random_state=42)

# ------------------------------------------------ exporteur ONNX fait main
def hgb_to_onnx(m, n_features: int) -> onnx.ModelProto:
    """HistGradientBoostingRegressor -> TreeEnsembleRegressor (ai.onnx.ml).
    Convention sklearn : x[f] <= threshold -> enfant GAUCHE ; NaN suit
    missing_go_to_left. Les valeurs de feuilles incluent deja le shrinkage."""
    def t32_floor(t: float) -> float:
        """Plus grand float32 <= t : rend la comparaison x_f32 <= seuil_f32
        EQUIVALENTE a x <= seuil_f64 pour toute entree float32. Sans cela,
        125/150 seuils arrondis au float32 superieur inversaient des splits
        (parite mesuree a 0.55 pres — bug diagnostique le 14 juil 2026)."""
        t32 = np.float32(t)
        if float(t32) > t:
            t32 = np.nextafter(t32, np.float32(-np.inf))
        return float(t32)

    tree_ids, node_ids, feat_ids, modes, values = [],[],[],[],[]
    true_ids, false_ids, miss_true = [],[],[]
    tgt_tree, tgt_node, tgt_w = [],[],[]
    for t,(pred,) in enumerate(m._predictors):
        nodes = pred.nodes
        for i,nd in enumerate(nodes):
            tree_ids.append(t); node_ids.append(i)
            if nd["is_leaf"]:
                feat_ids.append(0); modes.append("LEAF"); values.append(0.0)
                true_ids.append(0); false_ids.append(0); miss_true.append(0)
                tgt_tree.append(t); tgt_node.append(i)
                tgt_w.append(float(nd["value"]))
            else:
                feat_ids.append(int(nd["feature_idx"]))
                modes.append("BRANCH_LEQ")
                values.append(t32_floor(float(nd["num_threshold"])))
                true_ids.append(int(nd["left"]))     # <= -> gauche
                false_ids.append(int(nd["right"]))
                miss_true.append(1 if nd["missing_go_to_left"] else 0)
    node = helper.make_node(
        "TreeEnsembleRegressor", ["input"], ["score"], domain="ai.onnx.ml",
        n_targets=1,
        nodes_treeids=tree_ids, nodes_nodeids=node_ids,
        nodes_featureids=feat_ids, nodes_modes=modes,
        nodes_values=values, nodes_truenodeids=true_ids,
        nodes_falsenodeids=false_ids,
        nodes_missing_value_tracks_true=miss_true,
        target_treeids=tgt_tree, target_nodeids=tgt_node,
        target_ids=[0]*len(tgt_tree), target_weights=tgt_w,
        base_values=[float(m._baseline_prediction.ravel()[0])],
        post_transform="NONE")
    graph = helper.make_graph(
        [node], "mikael_macro_v4",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1,n_features])],
        [helper.make_tensor_value_info("score", TensorProto.FLOAT, [1,1])])
    model = helper.make_model(graph, opset_imports=[
        helper.make_opsetid("", 15), helper.make_opsetid("ai.onnx.ml", 3)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model

# ----------------------------------------------------------------- pipeline
ds = pd.read_parquet(ROOT/"dataset.parquet").dropna(subset=FEATS+["label","r_buy","r_sell"])
ds["year"] = ds["time"].dt.year
y = ds["label"].astype(np.float64)

# 1. walk-forward 2019-23 : seuil q90 pour la formulation regression
oos_scores, oos_frames = [], []
for yr in range(2019, 2024):
    tr, te = ds[ds.year<yr], ds[ds.year==yr]
    mm = make_model().fit(tr[FEATS].to_numpy(np.float64), y[tr.index])
    t = te.copy(); t["score"] = mm.predict(te[FEATS].to_numpy(np.float64))
    oos_frames.append(t)
oos = pd.concat(oos_frames)
thr = float(np.quantile(np.abs(oos["score"]), Q_SEL))
print(f"seuil q90 (walk-forward regression) = {thr:.4f}")

# 2. validation 2024-25 de CETTE formulation (rapport honnete)
tr, val = ds[ds.year<=2023], ds[ds.year.isin([2024,2025])].copy()
mv = make_model().fit(tr[FEATS].to_numpy(np.float64), y[tr.index])
val["score"] = mv.predict(val[FEATS].to_numpy(np.float64))
sel = val[np.abs(val["score"])>=thr].copy()
sel["r"] = np.where(sel["score"]>0, sel["r_buy"], sel["r_sell"]) - COST_R
rho,pv = spearmanr(val["score"], np.where(val["score"]>0, val["r_buy"], val["r_sell"]))
print(f"VALIDATION 2024-25 (regression): N={len(sel)}  "
      f"R_net={sel['r'].mean():+.4f}  win={(sel['r']>0).mean()*100:.1f}%  "
      f"Spearman={rho:+.4f} (p={pv:.2g})")
print("RAPPEL : cette famille est NO-GO — export pour FORWARD DEMO uniquement.")

# 3. entrainement final ≤2025 + export
trf = ds[ds.year<=2025]
mf = make_model().fit(trf[FEATS].to_numpy(np.float64), y[trf.index])
model = hgb_to_onnx(mf, len(FEATS))
path = OUT/"model.onnx"
onnx.save(model, path)

# 4. PARITE EXACTE exigee
sample = ds[FEATS].sample(5000, random_state=0).to_numpy(np.float32)
sk = mf.predict(sample.astype(np.float64))
sess = ort.InferenceSession(str(path))
rt = np.array([sess.run(None,{"input":s.reshape(1,-1)})[0][0][0] for s in sample])
err = np.abs(sk-rt).max()
flips = int((np.sign(sk)!=np.sign(rt)).sum())
print(f"parite: ecart max={err:.2e}  inversions de signe={flips}/5000")
assert err < 1e-5 and flips == 0, "PARITE NON ATTEINTE — ne pas deployer"

meta = {"features": FEATS, "formulation": "regression label -1/0/+1",
        "threshold_abs_score": round(thr,4),
        "score": "long si >0 ; trade si |score|>=threshold",
        "train": "2015-2025 (holdout 2026 exclu)",
        "validation_2024_25": {"N": int(len(sel)),
            "R_net": round(float(sel['r'].mean()),4),
            "spearman": round(float(rho),4)},
        "verdict": "NO-GO backtest — FORWARD DEMO UNIQUEMENT, jamais compte reel"}
(OUT/"model_meta.json").write_text(json.dumps(meta, indent=2))
(OUT/"model_reg_test.onnx").unlink(missing_ok=True)
print(f"-> {path} ({path.stat().st_size/1024:.0f} Ko) + model_meta.json")
