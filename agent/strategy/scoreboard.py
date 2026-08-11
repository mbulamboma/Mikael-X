# -*- coding: utf-8 -*-
"""Tableau de performance par strategie — l'agent apprend LAQUELLE marche.

C'est le 2e etage du choix de strategie : le regime dit ce qui est adapte "en
theorie", le scoreboard dit ce qui a REELLEMENT gagne pour cet agent, sur ce
compte, ces derniers temps. On combine les deux + une exploration UCB pour ne
pas rester coince sur une strategie devenue mauvaise (ni en abandonner une trop
tot faute d'essais).

Alimente par le journal (memory.closed_trades) : chaque trade cloture porte
`strategy` et `R` (resultat en multiples de risque).
"""
from __future__ import annotations
import math
from dataclasses import dataclass

from strategy import playbooks


@dataclass
class StratStat:
    strategy: str
    n: int
    winrate: float
    expectancy_R: float      # gain moyen par trade en R (le vrai edge)
    pnl: float


class Scoreboard:
    def __init__(self, closed_trades: list[dict]):
        self.stats: dict[str, StratStat] = self._compute(closed_trades)
        self.total = sum(s.n for s in self.stats.values())

    def _compute(self, trades: list[dict]) -> dict[str, StratStat]:
        by: dict[str, list[dict]] = {}
        for t in trades:
            s = t.get("strategy") or "unknown"
            by.setdefault(s, []).append(t)
        out: dict[str, StratStat] = {}
        for s, rows in by.items():
            rs = [r.get("R", 0.0) for r in rows]
            wins = [r for r in rs if r > 0]
            pnl = sum(r.get("pnl", 0.0) for r in rows)
            out[s] = StratStat(
                strategy=s, n=len(rows),
                winrate=round(len(wins) / len(rows) * 100, 1) if rows else 0.0,
                expectancy_R=round(sum(rs) / len(rs), 3) if rs else 0.0,
                pnl=round(pnl, 2),
            )
        return out

    def score(self, strategy: str, regime: str, c: float = 0.7) -> float:
        """Score de selection = adequation regime + edge historique + bonus exploration UCB.

        Une strategie jamais testee obtient un gros bonus d'exploration (on lui
        donne sa chance) ; une strategie a expectancy negative est penalisee."""
        fit = playbooks.fit(strategy, regime)               # 0..1
        st = self.stats.get(strategy)
        if st is None or st.n == 0:
            explore = 1.0                                   # priorite : jamais essayee
            edge = 0.0
        else:
            explore = c * math.sqrt(math.log(max(self.total, 1) + 1) / st.n)
            edge = st.expectancy_R                          # peut etre negatif -> penalise
        return round(fit + edge + explore, 3)

    def rank(self, regime: str) -> list[tuple[str, float]]:
        ranked = [(s, self.score(s, regime)) for s in playbooks.names()]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    def as_prompt_block(self, regime: str) -> str:
        """Bloc injecte dans le prompt : classement + stats reelles par strategie."""
        lines = [f"Regime actuel: {regime}"]
        for s, sc in self.rank(regime):
            st = self.stats.get(s)
            if st and st.n:
                lines.append(f"- {s}: score {sc} | {st.n} trades, "
                             f"winrate {st.winrate}%, expectancy {st.expectancy_R}R, PnL {st.pnl}$")
            else:
                lines.append(f"- {s}: score {sc} | (jamais testee — a explorer)")
        return "\n".join(lines)

    def best(self, regime: str) -> str:
        return self.rank(regime)[0][0]
