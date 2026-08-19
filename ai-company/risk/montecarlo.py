# -*- coding: utf-8 -*-
"""SIMULATEUR MONTE-CARLO — quelle est la PROBABILITE DE PASSER le challenge FTMO ?

Le reflexe d'entreprise dont on parlait : ne JAMAIS acheter un challenge sans avoir estime,
par simulation, sa probabilite de reussite. Passer FTMO n'est pas « faire +10 % » — c'est
*atteindre la cible AVANT de toucher une limite de perte*. C'est donc un probleme de survie,
et une esperance moyenne positive ne dit rien de la probabilite de buster en chemin.

CE QUE CE MODULE FAIT :
  - il tire des milliers de challenges depuis une DISTRIBUTION DE R (multiples de risque) —
    soit EMPIRIQUE (tes trades clotures, cf. brain/memory.py), soit PARAMETRIQUE (winrate +
    R gagnant/perdant) quand l'historique est trop maigre ;
  - il applique les VRAIES regles FTMO (perte jour, perte totale, cible, jours mini, delai)
    ET les garde-fous PROPRES a l'agent (arret des ouvertures a -daily_stop, stop total soft,
    plafond de trades/jour) — parce que l'agent ne trade PAS bêtement jusqu'a -5 % : il se
    protege avant, et c'est ca qui change la probabilite de survie ;
  - il rend une distribution d'issues : P(reussite), P(bust jour), P(bust total), P(delai),
    jours-avant-passage attendus, et la distribution de l'equity finale.

REPRODUCTIBILITE : tout est pilote par une graine (`seed`). Deux runs identiques rendent le
meme chiffre — c'est une exigence de validation, pas un detail.

HYPOTHESES ASSUMEES (documentees, donc discutables) :
  - les R des trades sont tires de facon i.i.d. (pas d'autocorrelation de regime modelisee) ;
    l'option `stress` injecte des scenarios adverses (serie perdante) pour compenser en partie.
  - la perte jour est suivie en CUMULE INTRAJOURNALIER des R realises : on buste des que le
    cumul du jour franchit la limite. C'est une approximation LEGEREMENT optimiste du flottant
    reel (plusieurs positions simultanees pourraient toucher leur stop ensemble).
  - un « jour de trading » compte dès qu'au moins un trade y est pris (critere FTMO des jours
    minimum).

Aucune dependance lourde : `random` + `statistics` de la stdlib. Utilisable en test, en CLI
(`python -m risk.montecarlo`), ou appele par un rapport.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import FTMOConfig


# --------------------------------------------------------------------------- distribution de R
@dataclass
class ReturnModel:
    """Comment on tire le R d'un trade. Deux modes exclusifs :
      - EMPIRIQUE : `samples` non vide -> on tire dedans (avec remise) ;
      - PARAMETRIQUE : winrate + R gagnant/perdant (defauts prudents d'un swing R:R 2)."""
    samples: list[float] = field(default_factory=list)
    win_rate: float = 0.45
    win_R: float = 2.0
    loss_R: float = -1.0
    # part des trades qui finissent ~a l'equilibre (frais, sortie manuelle) : ni gain ni perte
    breakeven_rate: float = 0.0

    @classmethod
    def from_trades(cls, trades: list[dict], min_n: int = 20) -> "ReturnModel":
        """Construit le modele depuis les trades clotures. Repli parametrique calibre sur
        l'historique s'il y a MOINS de `min_n` trades (un echantillon trop court ment)."""
        rs = [float(t.get("R")) for t in (trades or [])
              if isinstance(t, dict) and _is_num(t.get("R"))]
        if len(rs) >= min_n:
            return cls(samples=rs)
        if rs:                                   # trop court pour l'empirique : on CALIBRE
            gains = [r for r in rs if r > 0]
            pertes = [r for r in rs if r < 0]
            return cls(
                win_rate=len(gains) / len(rs) if rs else 0.45,
                win_R=statistics.mean(gains) if gains else 2.0,
                loss_R=statistics.mean(pertes) if pertes else -1.0)
        return cls()                             # aucun historique : defauts prudents

    def draw(self, rng: random.Random) -> float:
        if self.samples:
            return rng.choice(self.samples)
        u = rng.random()
        if u < self.breakeven_rate:
            return 0.0
        return self.win_R if rng.random() < self.win_rate else self.loss_R

    def expectancy(self, rng: Optional[random.Random] = None, n: int = 20000) -> float:
        """Esperance en R (utile au rapport : un edge negatif condamne le challenge)."""
        if self.samples:
            return statistics.mean(self.samples)
        return self.win_rate * self.win_R + (1 - self.win_rate - self.breakeven_rate) * self.loss_R


# --------------------------------------------------------------------------- config de la sim
@dataclass
class SimConfig:
    n_paths: int = 10000
    seed: int = 12345
    trades_per_day_mean: float = 2.0             # borne haute = ftmo.max_trades_per_day
    # STRESS : proba, a chaque tirage, de forcer un R perdant (serie noire, gap defavorable).
    # 0 = pas de stress. Sert a ne pas surestimer la survie sur un historique clement.
    stress_loss_prob: float = 0.0
    # OVERRIDES d'analyse de sensibilite (0 = utiliser la config FTMO). Repondre a « et si je
    # sizais a 2 %/trade ? » ou « et si je prenais 5 trades/jour ? » sans toucher au .env.
    risk_per_trade_override: float = 0.0
    max_trades_per_day_override: int = 0


@dataclass
class PathOutcome:
    issue: str                                   # "reussite" | "bust_jour" | "bust_total" | "delai"
    jours: int
    trades: int
    equity_finale_pct: float                     # equity finale en % du solde initial (100 = pair)


@dataclass
class SimReport:
    n_paths: int
    p_reussite: float
    p_bust_jour: float
    p_bust_total: float
    p_delai: float
    jours_avant_passage: dict                     # {"p50":..,"p90":..,"moyenne":..} sur les reussites
    equity_finale: dict                           # {"p05":..,"p50":..,"p95":..,"moyenne":..}
    expectancy_R: float
    source_distribution: str                      # "empirique (N trades)" | "parametrique"
    cfg_echo: dict                                # regles appliquees (tracabilite)

    def texte(self) -> str:
        return _format_report(self)


# --------------------------------------------------------------------------- moteur
class MonteCarlo:
    def __init__(self, ftmo: FTMOConfig, model: ReturnModel, sim: Optional[SimConfig] = None):
        self.ftmo = ftmo
        self.model = model
        self.sim = sim or SimConfig()

    # ---- une trajectoire ----
    def _path(self, rng: random.Random) -> PathOutcome:
        f = self.ftmo
        risk = self.sim.risk_per_trade_override or f.risk_per_trade_pct   # 1R en % du solde
        cible = f.profit_target_pct
        perte_jour_max = f.max_daily_loss_pct
        perte_totale_max = f.max_total_loss_pct
        # garde-fous de l'AGENT (il arrete d'ouvrir AVANT la limite FTMO) : c'est ce qui fait
        # la vraie difference de survie entre un systeme discipline et un martingale.
        stop_jour_agent = f.daily_stop_pct
        stop_total_agent = f.total_soft_stop_pct
        plafond_tpd = self.sim.max_trades_per_day_override or f.max_trades_per_day
        tpd = max(1, min(int(round(self.sim.trades_per_day_mean)), plafond_tpd))

        equity = 0.0                                          # PnL cumule en % (0 = solde initial)
        pic = 0.0                                             # plus-haut d'equity (perte totale = statique ici)
        jours = 0
        trades = 0
        for _ in range(f.phase_days):
            jours += 1
            perte_jour = 0.0                                  # cumul intrajournalier en %
            for _ in range(tpd):
                # l'agent a-t-il deja atteint son stop du jour / son stop total soft ?
                if -perte_jour >= stop_jour_agent:
                    break                                     # plus d'ouverture aujourd'hui
                if -equity >= stop_total_agent:
                    break
                r = self._draw(rng)
                delta = r * risk
                trades += 1
                equity += delta
                perte_jour += delta
                # --- limites DURES FTMO : verifiees apres chaque trade ---
                if -perte_jour >= perte_jour_max:
                    return PathOutcome("bust_jour", jours, trades, 100 + equity)
                if -equity >= perte_totale_max:
                    return PathOutcome("bust_total", jours, trades, 100 + equity)
                # --- cible atteinte : reussite SI le minimum de jours est rempli ---
                if equity >= cible and jours >= f.min_trading_days:
                    return PathOutcome("reussite", jours, trades, 100 + equity)
            # fin de journee : si la cible est atteinte mais les jours mini pas encore faits,
            # on continue (comportement de l'agent : il ne fige pas un compte invalidable).
        # delai epuise sans avoir passe
        issue = "reussite" if (equity >= cible and jours >= self.ftmo.min_trading_days) else "delai"
        return PathOutcome(issue, jours, trades, 100 + equity)

    def _draw(self, rng: random.Random) -> float:
        if self.sim.stress_loss_prob and rng.random() < self.sim.stress_loss_prob:
            # scenario adverse force : la pire perte plausible du modele
            return min(self.model.loss_R, min(self.model.samples) if self.model.samples else self.model.loss_R)
        return self.model.draw(rng)

    # ---- agregat ----
    def run(self) -> SimReport:
        rng = random.Random(self.sim.seed)
        issues = {"reussite": 0, "bust_jour": 0, "bust_total": 0, "delai": 0}
        jours_ok: list[int] = []
        equities: list[float] = []
        for _ in range(self.sim.n_paths):
            out = self._path(rng)
            issues[out.issue] += 1
            equities.append(out.equity_finale_pct)
            if out.issue == "reussite":
                jours_ok.append(out.jours)
        n = max(1, self.sim.n_paths)
        return SimReport(
            n_paths=self.sim.n_paths,
            p_reussite=issues["reussite"] / n,
            p_bust_jour=issues["bust_jour"] / n,
            p_bust_total=issues["bust_total"] / n,
            p_delai=issues["delai"] / n,
            jours_avant_passage=_quantiles_int(jours_ok),
            equity_finale=_quantiles(equities),
            expectancy_R=round(self.model.expectancy(), 3),
            source_distribution=(f"empirique ({len(self.model.samples)} trades)"
                                 if self.model.samples else "parametrique"),
            cfg_echo={
                "cible_pct": self.ftmo.profit_target_pct,
                "perte_jour_max_pct": self.ftmo.max_daily_loss_pct,
                "perte_totale_max_pct": self.ftmo.max_total_loss_pct,
                "stop_jour_agent_pct": self.ftmo.daily_stop_pct,
                "stop_total_agent_pct": self.ftmo.total_soft_stop_pct,
                "jours_min": self.ftmo.min_trading_days,
                "delai_jours": self.ftmo.phase_days,
                "risque_par_trade_pct": self.sim.risk_per_trade_override or self.ftmo.risk_per_trade_pct,
                "trades_par_jour": max(1, min(int(round(self.sim.trades_per_day_mean)),
                                       self.sim.max_trades_per_day_override
                                       or self.ftmo.max_trades_per_day)),
                "stress_loss_prob": self.sim.stress_loss_prob,
                "seed": self.sim.seed,
            })


# --------------------------------------------------------------------------- helpers
def _is_num(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def _quantiles(xs: list[float]) -> dict:
    if not xs:
        return {}
    s = sorted(xs)
    return {"p05": round(_pct(s, 5), 2), "p50": round(_pct(s, 50), 2),
            "p95": round(_pct(s, 95), 2), "moyenne": round(statistics.mean(s), 2)}


def _quantiles_int(xs: list[int]) -> dict:
    if not xs:
        return {}
    s = sorted(xs)
    return {"p50": int(_pct(s, 50)), "p90": int(_pct(s, 90)),
            "moyenne": round(statistics.mean(s), 1)}


def _pct(sorted_xs: list, p: float) -> float:
    """Percentile par interpolation lineaire (methode simple, sans numpy)."""
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return float(sorted_xs[0])
    k = (len(sorted_xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_xs) - 1)
    frac = k - lo
    return float(sorted_xs[lo]) * (1 - frac) + float(sorted_xs[hi]) * frac


def simulate(ftmo: FTMOConfig, trades: Optional[list[dict]] = None,
             sim: Optional[SimConfig] = None,
             model: Optional[ReturnModel] = None) -> SimReport:
    """Point d'entree pratique : construit le modele depuis les trades (ou parametrique) et
    lance la simulation. `model` explicite l'emporte sur `trades`."""
    m = model or ReturnModel.from_trades(trades or [])
    return MonteCarlo(ftmo, m, sim).run()


def _format_report(r: SimReport) -> str:
    j = r.jours_avant_passage
    e = r.equity_finale
    lignes = [
        "== SIMULATION MONTE-CARLO - PROBABILITE DE PASSAGE FTMO ==",
        f"Trajectoires        : {r.n_paths}   |   distribution R : {r.source_distribution}",
        f"Esperance par trade : {r.expectancy_R:+.3f} R"
        + ("   /!\\ EDGE NEGATIF - aucun sizing ne sauve ca" if r.expectancy_R <= 0 else ""),
        "",
        f"  P(REUSSITE)       : {r.p_reussite*100:5.1f} %",
        f"  P(bust journalier): {r.p_bust_jour*100:5.1f} %",
        f"  P(bust total)     : {r.p_bust_total*100:5.1f} %",
        f"  P(delai epuise)   : {r.p_delai*100:5.1f} %",
    ]
    if j:
        lignes.append(f"  Jours avant passage (reussites) : median {j.get('p50')}, "
                      f"p90 {j.get('p90')}, moyenne {j.get('moyenne')}")
    if e:
        lignes.append(f"  Equity finale (% du solde)      : p05 {e.get('p05')}, "
                      f"median {e.get('p50')}, p95 {e.get('p95')}")
    lignes += ["", "Regles appliquees (FTMO + garde-fous agent) :",
               "  " + ", ".join(f"{k}={v}" for k, v in r.cfg_echo.items())]
    return "\n".join(lignes)


# --------------------------------------------------------------------------- CLI
def _main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Probabilite de passage d'un challenge FTMO (MC).")
    p.add_argument("--paths", type=int, default=10000, help="nombre de trajectoires")
    p.add_argument("--seed", type=int, default=12345)
    p.add_argument("--trades-per-day", type=float, default=2.0)
    p.add_argument("--stress", type=float, default=0.0,
                   help="proba de forcer un R perdant a chaque trade (0-1)")
    p.add_argument("--risk", type=float, default=0.0,
                   help="risque/trade en %% (0 = config FTMO) — analyse de sensibilite")
    p.add_argument("--max-trades", type=int, default=0,
                   help="plafond de trades/jour (0 = config FTMO)")
    p.add_argument("--history", action="store_true",
                   help="calibrer sur les trades clotures (sinon parametrique)")
    p.add_argument("--win-rate", type=float, default=0.45)
    p.add_argument("--win-r", type=float, default=2.0)
    p.add_argument("--loss-r", type=float, default=-1.0)
    a = p.parse_args(argv)

    from config import AgentConfig
    cfg = AgentConfig()
    trades = []
    if a.history:
        from brain.memory import Memory
        trades = Memory().closed_trades()
    model = (ReturnModel.from_trades(trades) if a.history
             else ReturnModel(win_rate=a.win_rate, win_R=a.win_r, loss_R=a.loss_r))
    sim = SimConfig(n_paths=a.paths, seed=a.seed,
                    trades_per_day_mean=a.trades_per_day, stress_loss_prob=a.stress,
                    risk_per_trade_override=a.risk, max_trades_per_day_override=a.max_trades)
    report = MonteCarlo(cfg.ftmo, model, sim).run()
    print(report.texte())
    return 0


if __name__ == "__main__":                       # pragma: no cover
    raise SystemExit(_main())
