# -*- coding: utf-8 -*-
"""Catalogue de strategies (playbooks) que l'agent a le droit de choisir.

L'agent n'est PAS enferme dans une seule strategie : a chaque cycle il choisit
celle qui colle le mieux au regime de marche ET qui a le meilleur track-record
(cf. scoreboard.py). Chaque playbook fournit au LLM un mode d'emploi concret
(quand entrer, ou placer le stop, ou viser) pour qu'il raisonne comme un pro
qui applique une methode nommee — pas au feeling.

`regime_fit` : score a priori [0..1] de l'adequation strategie x regime.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Playbook:
    name: str
    summary: str
    entry_rules: str
    stop_rules: str
    target_rules: str
    regime_fit: dict[str, float] = field(default_factory=dict)


BOOK: dict[str, Playbook] = {
    "trend_follow": Playbook(
        name="trend_follow",
        summary="Suivi de tendance D1 : on trade dans le sens de la MA50/200, sur repli.",
        entry_rules="Entrer dans le sens de la tendance apres un repli vers l'EMA20 "
                    "(RSI qui se detend puis repart). Pas de contre-tendance.",
        stop_rules="Stop 1.0-1.5x ATR sous le dernier creux (buy) / au-dessus du dernier sommet (sell).",
        target_rules="TP a 2-3x le risque, ou sur le prochain niveau Donchian oppose.",
        regime_fit={"trend_up": 0.9, "trend_down": 0.9, "range": 0.2, "high_vol": 0.4},
    ),
    "donchian_breakout": Playbook(
        name="donchian_breakout",
        summary="Cassure Donchian 20 : on achete la cassure du plus-haut / vend celle du plus-bas.",
        entry_rules="Entrer a la cloture au-dela du Donchian20 (haut pour buy, bas pour sell) "
                    "avec momentum (ret_5b confirme) et volatilite en expansion.",
        stop_rules="Stop de l'autre cote du canal, ou 1.5x ATR — jamais dans le canal.",
        target_rules="TP a 2x le risque minimum ; laisser courir si la tendance s'installe.",
        regime_fit={"trend_up": 0.7, "trend_down": 0.7, "range": 0.1, "high_vol": 0.9},
    ),
    "mean_reversion": Playbook(
        name="mean_reversion",
        summary="Retour a la moyenne : on fade les extremes quand il n'y a PAS de tendance nette.",
        entry_rules="Acheter le bas de range (pos_in_range < 20, RSI < 35) / vendre le haut "
                    "(pos_in_range > 80, RSI > 65). Uniquement en regime range.",
        stop_rules="Stop serre 1x ATR juste au-dela de l'extreme du range.",
        target_rules="TP au milieu du range (retour a l'EMA20). R:R >= 1.5 exige.",
        regime_fit={"trend_up": 0.1, "trend_down": 0.1, "range": 0.9, "high_vol": 0.2},
    ),
    "momentum": Playbook(
        name="momentum",
        summary="Momentum court : on suit une impulsion forte deja engagee.",
        entry_rules="Entrer dans le sens d'une impulsion nette (ret_20b fort + EMA20 en pente) "
                    "sur micro-repli, sans attendre un repli profond.",
        stop_rules="Stop 1.2x ATR sous/au-dessus du point d'entree.",
        target_rules="TP a 2x le risque ; sortie rapide si l'impulsion s'essouffle.",
        regime_fit={"trend_up": 0.7, "trend_down": 0.7, "range": 0.2, "high_vol": 0.7},
    ),
    "flat": Playbook(
        name="flat",
        summary="Ne rien faire : aucun edge clair. Preserver le capital est une decision.",
        entry_rules="Aucune entree.",
        stop_rules="-",
        target_rules="-",
        regime_fit={"trend_up": 0.2, "trend_down": 0.2, "range": 0.3, "high_vol": 0.3},
    ),
}


def names() -> list[str]:
    return list(BOOK.keys())


def as_prompt_block() -> str:
    """Bloc lisible injecte dans le prompt du LLM."""
    lines = []
    for p in BOOK.values():
        lines.append(
            f"### {p.name}\n{p.summary}\n"
            f"- Entree : {p.entry_rules}\n- Stop : {p.stop_rules}\n- Cible : {p.target_rules}"
        )
    return "\n\n".join(lines)


def fit(strategy: str, regime: str) -> float:
    p = BOOK.get(strategy)
    return p.regime_fit.get(regime, 0.3) if p else 0.0
