# -*- coding: utf-8 -*-
"""POST-MORTEM — ce que les trades passes disent VRAIMENT, en chiffres.

C'est la seule memoire de performance de l'agent : il n'ecrit aucune "lecon" apres une
cloture (une phrase produite par un LLM n'est ni verifiable ni mesurable, et elle vieillit
mal). Ici on regarde le journal (table `events` de `state/agent.db`) et on extrait
des FAITS mesurables, puis on formule des defauts recurrents :

  - plan vs realite : R:R planifie contre R reellement encaisse,
  - MFE / MAE (meilleur et pire point atteint en R) -> stop trop serre ? sortie trop tot ?
  - taux de reussite et esperance par strategie, par symbole, par regime,
  - cout d'execution reel : slippage moyen, spread paye, frais / gain moyen,
  - discipline : trades pris apres un veto, sur-trading, sur-confiance
    (confiance annoncee vs resultat).

Le bloc produit par `as_prompt_block()` est injecte a chaque cycle : l'agent voit
ses propres travers chiffres avant de decider. Chaque ligne est recalculee depuis le
journal — rien ici n'est une opinion, donc rien ici ne peut deriver.
"""
from __future__ import annotations

from collections import defaultdict


def _n(x, default=0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


class PostMortem:
    """Analyse quantitative des trades clotures (aucun LLM ici : que des faits)."""

    def __init__(self, closed_trades: list[dict], min_trades: int = 5):
        self.trades = [t for t in closed_trades if isinstance(t, dict)]
        self.min_trades = min_trades

    # ------------------------------------------------------------------ stats
    def _group(self, key: str) -> dict[str, dict]:
        by: dict[str, list[dict]] = defaultdict(list)
        for t in self.trades:
            by[str(t.get(key) or "?")].append(t)
        out = {}
        for k, rows in by.items():
            rs = [_n(r.get("R")) for r in rows]
            wins = [r for r in rs if r > 0]
            out[k] = {
                "n": len(rows),
                "winrate": round(len(wins) / len(rows) * 100, 1),
                "expectancy_R": round(_avg(rs), 2),
                "pnl": round(sum(_n(r.get("pnl")) for r in rows), 2),
                "gain_moyen_R": round(_avg([r for r in rs if r > 0]), 2),
                "perte_moyenne_R": round(_avg([r for r in rs if r < 0]), 2),
            }
        return dict(sorted(out.items(), key=lambda kv: kv[1]["expectancy_R"], reverse=True))

    def execution_stats(self) -> dict:
        """Ce que l'execution coute vraiment (slippage, spread, frais / gain)."""
        slip = [_n(t.get("slippage_pips")) for t in self.trades if t.get("slippage_pips") is not None]
        spread = [_n(t.get("spread_pips_entree")) for t in self.trades
                  if t.get("spread_pips_entree") is not None]
        couts = [_n(t.get("couts_estimes")) for t in self.trades if t.get("couts_estimes")]
        pnl_brut = [abs(_n(t.get("pnl"))) for t in self.trades if t.get("pnl")]
        return {
            "slippage_moyen_pips": round(_avg(slip), 2) if slip else None,
            "spread_moyen_entree_pips": round(_avg(spread), 2) if spread else None,
            "cout_moyen_par_trade": round(_avg(couts), 2) if couts else None,
            "couts_en_pct_du_pnl_absolu": (round(sum(couts) / sum(pnl_brut) * 100, 1)
                                           if couts and sum(pnl_brut) else None),
        }

    def summary(self) -> dict:
        rs = [_n(t.get("R")) for t in self.trades]
        wins = [r for r in rs if r > 0]
        planned = [_n(t.get("rr_planifie")) for t in self.trades if t.get("rr_planifie")]
        mfe = [_n(t.get("mfe_R")) for t in self.trades if t.get("mfe_R") is not None]
        mae = [_n(t.get("mae_R")) for t in self.trades if t.get("mae_R") is not None]
        return {
            "n_trades": len(self.trades),
            "winrate": round(len(wins) / len(rs) * 100, 1) if rs else 0.0,
            "expectancy_R": round(_avg(rs), 2),
            "R_total": round(sum(rs), 2),
            "rr_planifie_moyen": round(_avg(planned), 2) if planned else None,
            "R_realise_moyen": round(_avg(rs), 2),
            "mfe_moyen_R": round(_avg(mfe), 2) if mfe else None,
            "mae_moyen_R": round(_avg(mae), 2) if mae else None,
            "par_strategie": self._group("strategy"),
            "par_symbole": self._group("symbol"),
            "par_regime": self._group("regime"),
            "execution": self.execution_stats(),
        }

    # ------------------------------------------------------------------ defauts
    def flaws(self) -> list[str]:
        """Defauts RECURRENTS, formules en langage de trader. Vide si trop peu de data."""
        out: list[str] = []
        n = len(self.trades)
        if n < self.min_trades:
            return out
        rs = [_n(t.get("R")) for t in self.trades]
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r < 0]
        s = self.summary()

        # 1. le plan promet 2R et on encaisse 0.3R -> sorties trop precoces / cibles irrealistes
        if s["rr_planifie_moyen"] and s["expectancy_R"] < s["rr_planifie_moyen"] * 0.25:
            out.append(f"ECART PLAN/REALITE : R:R planifie {s['rr_planifie_moyen']:.1f} mais "
                       f"esperance reelle {s['expectancy_R']:.2f}R — soit tu sors trop tot, "
                       f"soit tes cibles sont irrealistes. Vise des TP atteignables.")

        # 2. les gagnants montent haut puis reviennent -> gains rendus
        gagnants_rendus = [t for t in self.trades
                           if _n(t.get("mfe_R")) >= 1.0 and _n(t.get("R")) <= 0]
        if len(gagnants_rendus) >= max(2, n * 0.2):
            out.append(f"GAINS RENDUS : {len(gagnants_rendus)}/{n} trades ont atteint +1R "
                       f"ou plus avant de finir a l'equilibre ou en perte — securise "
                       f"(break-even, plan_trail, prise partielle) des +1R.")

        # 3. les perdants sont d'abord passes en profit ? sinon stop trop serre
        stop_serre = [t for t in self.trades
                      if _n(t.get("R")) <= -0.9 and _n(t.get("mfe_R")) < 0.3]
        if len(stop_serre) >= max(2, n * 0.3):
            out.append(f"STOP TROP SERRE : {len(stop_serre)}/{n} pertes n'ont jamais depasse "
                       f"+0.3R — tes stops sont dans le bruit (compare-les a l'ATR et au spread).")

        # 4. perte moyenne > gain moyen (asymetrie defavorable)
        if wins and losses and _avg(wins) < abs(_avg(losses)):
            out.append(f"ASYMETRIE DEFAVORABLE : gain moyen {_avg(wins):.2f}R < perte moyenne "
                       f"{abs(_avg(losses)):.2f}R — laisse courir les gagnants, coupe plus vite.")

        # 5. couts qui mangent la performance
        ex = s["execution"]
        if ex.get("couts_en_pct_du_pnl_absolu") and ex["couts_en_pct_du_pnl_absolu"] > 25:
            out.append(f"FRAIS EXCESSIFS : les couts representent "
                       f"{ex['couts_en_pct_du_pnl_absolu']:.0f}% du PnL brut — stops trop "
                       f"courts / trop de trades. Cherche des mouvements plus amples.")
        if ex.get("slippage_moyen_pips") and ex["slippage_moyen_pips"] > 2:
            out.append(f"SLIPPAGE ELEVE : {ex['slippage_moyen_pips']:.1f} pips en moyenne — "
                       f"evite d'entrer dans la minute d'une annonce ou sur un marche fin.")

        # 6. strategies / symboles a bannir temporairement
        for label, stats in (("strategie", s["par_strategie"]), ("symbole", s["par_symbole"])):
            for k, v in stats.items():
                if v["n"] >= max(4, self.min_trades) and v["expectancy_R"] <= -0.3:
                    out.append(f"{label.upper()} A EVITER : {k} -> {v['n']} trades, "
                               f"esperance {v['expectancy_R']}R, winrate {v['winrate']}% "
                               f"— arrete-la jusqu'a changement de regime.")

        # 7. sur-confiance : confiance elevee mais resultats negatifs
        confiants = [t for t in self.trades if _n(t.get("confidence")) >= 0.75]
        if len(confiants) >= 4 and _avg([_n(t.get("R")) for t in confiants]) < 0:
            out.append(f"SUR-CONFIANCE : tes trades annonces a confiance >= 0.75 perdent en "
                       f"moyenne ({_avg([_n(t.get('R')) for t in confiants]):.2f}R sur "
                       f"{len(confiants)}) — la conviction n'est pas un edge, exige la preuve.")
        return out

    # ------------------------------------------------------------------ prompt
    def as_prompt_block(self, top: int = 4) -> str:
        s = self.summary()
        if not self.trades:
            return "(aucun trade cloture : pas encore de post-mortem)"
        lines = [f"Bilan: {s['n_trades']} trades, winrate {s['winrate']}%, "
                 f"esperance {s['expectancy_R']}R, total {s['R_total']}R."]
        if s["rr_planifie_moyen"]:
            lines.append(f"Plan vs realite: R:R planifie moyen {s['rr_planifie_moyen']} -> "
                         f"R realise moyen {s['R_realise_moyen']} "
                         f"(MFE moyen {s['mfe_moyen_R']}, MAE moyen {s['mae_moyen_R']}).")
        ex = s["execution"]
        if any(v is not None for v in ex.values()):
            lines.append(f"Execution: slippage {ex['slippage_moyen_pips']} pips, spread entree "
                         f"{ex['spread_moyen_entree_pips']} pips, cout moyen "
                         f"{ex['cout_moyen_par_trade']}$ ({ex['couts_en_pct_du_pnl_absolu']}% du PnL).")
        for label, stats in (("Strategies", s["par_strategie"]), ("Symboles", s["par_symbole"])):
            best = [f"{k} ({v['n']}t, {v['expectancy_R']}R)" for k, v in list(stats.items())[:top]]
            if best:
                lines.append(f"{label} par esperance: " + ", ".join(best))
        flaws = self.flaws()
        if flaws:
            lines.append("\nDEFAUTS RECURRENTS A CORRIGER MAINTENANT :")
            lines += [f"- {f}" for f in flaws]
        else:
            lines.append("(pas encore assez de trades pour conclure sur des defauts recurrents)")
        return "\n".join(lines)
