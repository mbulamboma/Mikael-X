# -*- coding: utf-8 -*-
"""PILOTE DE SECOURS — 100 % Python deterministe, zero LLM.

Quand l'IA devient indisponible (Bedrock injoignable, credentials expirees, quota,
dependance manquante, reponse invalide...), on ne laisse PAS les positions ouvertes
sans surveillance et on n'ouvre plus rien. Ce module produit, a chaque cycle, les
memes actions que le LLM (`close` / `modify` / `trail`) mais par des REGLES FIXES,
orientees survie FTMO :

  1. Protection du compte d'abord : si la perte du jour approche le stop agent
     (ou la perte totale le seuil doux), on ferme TOUT, sans discuter.
  2. Aucune position sans stop : SL d'urgence a N x ATR si le stop manque.
  3. Securisation : stop au break-even des +1R.
  4. Trailing ATR arme sur chaque position pour verrouiller les gains.
  5. Time-stop : une position qui traine sans rien donner est fermee.
  6. JAMAIS d'ouverture.

L'orchestrateur execute ces actions puis, des que le book est VIDE, arrete le
processus : l'humain inspecte et relance (cf. run.Orchestrator._safe_mode).
"""
from __future__ import annotations

import logging

log = logging.getLogger("autopilot")


def _f(x, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v


def implied_r(pos: dict) -> float | None:
    """Profit flottant en R. Utilise floating_R s'il est connu, sinon le deduit de la
    geometrie entree/stop/prix (positions heritees, sans risque memorise)."""
    if pos.get("floating_R") is not None:
        return _f(pos["floating_R"])
    entry, sl, price = _f(pos.get("entry")), _f(pos.get("sl")), _f(pos.get("price_now"))
    if not entry or not sl or not price:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    gain = (price - entry) if pos.get("direction") == "buy" else (entry - price)
    return round(gain / risk, 2)


class SafePilot:
    """Gestionnaire deterministe des positions ouvertes (mode degrade)."""

    def __init__(self, cfg, ftmo):
        self.cfg = cfg
        self.ftmo = ftmo

    # ------------------------------------------------------------------ decision
    def actions(self, positions: list[dict], account: dict,
                atr_by_symbol: dict[str, float] | None = None,
                spec_of=None) -> list[dict]:
        """Renvoie la liste d'actions a executer ce cycle (jamais d'ouverture)."""
        if not positions:
            return []
        panic = self.panic_reason(account)
        if panic:
            log.error("PILOTE DE SECOURS — %s : fermeture de TOUTES les positions.", panic)
            return [{"type": "close", "ticket": p["ticket"], "fraction": 1.0,
                     "reason": f"secours: {panic}"} for p in positions]
        return self.protective_actions(positions, atr_by_symbol, spec_of)

    def protective_actions(self, positions: list[dict],
                           atr_by_symbol: dict[str, float] | None = None,
                           spec_of=None) -> list[dict]:
        """Protections position par position (SL d'urgence, break-even, trailing,
        time-stop) — SANS la regle de panique. Utilisees aussi hors mode degrade :
        ce filet ne doit jamais dependre de la disponibilite du LLM.

        `spec_of(symbol)` : fonction rendant la spec du symbole (digits reels). Sans
        elle on retombe sur une heuristique, ce qui peut faire REJETER un stop par le
        broker (ex : or cote a 2 decimales) — donc on la fournit toujours en production.
        """
        atr_by_symbol = atr_by_symbol or {}
        out: list[dict] = []
        for p in positions:
            digits = None
            if spec_of is not None:
                try:
                    digits = (spec_of(p.get("symbol", "")) or {}).get("digits")
                except Exception:
                    digits = None
            out.extend(self._manage(p, atr_by_symbol.get(p.get("symbol")), digits))
        return out

    # ------------------------------------------------------------------ regles
    def panic_reason(self, account: dict) -> str:
        """Faut-il tout fermer immediatement pour proteger le challenge ?"""
        seuil_jour = self.ftmo.daily_stop_pct * max(0.1, min(self.cfg.panic_ratio, 1.0))
        perte_jour = _f(account.get("perte_jour_pct"))
        if perte_jour >= seuil_jour:
            return (f"perte du jour {perte_jour:.2f}% >= {seuil_jour:.2f}% "
                    f"(stop agent {self.ftmo.daily_stop_pct:.2f}%)")
        pnl_total = _f(account.get("pnl_total_pct"))
        if pnl_total <= -self.ftmo.total_soft_stop_pct:
            return f"perte totale {pnl_total:.2f}% <= -{self.ftmo.total_soft_stop_pct:.2f}%"
        return ""

    def _manage(self, p: dict, atr: float | None, digits: int | None = None) -> list[dict]:
        tk = p.get("ticket")
        entry, sl = _f(p.get("entry")), _f(p.get("sl"))
        if digits is None:                      # repli si la spec broker est indisponible
            digits = 5 if entry < 20 else (3 if entry < 500 else 2)
        digits = int(digits)
        r = implied_r(p)
        acts: list[dict] = []

        # 1. aucune position sans stop (regle FTMO de survie)
        if not sl and atr:
            emergency = (entry - self.cfg.missing_sl_atr * atr if p.get("direction") == "buy"
                         else entry + self.cfg.missing_sl_atr * atr)
            log.warning("PILOTE DE SECOURS — ticket %s sans SL : pose a %.5f.", tk, emergency)
            return [{"type": "modify", "ticket": tk, "sl": round(emergency, digits), "tp": None,
                     "reason": "secours: stop d'urgence (position sans SL)"}]

        # 2. time-stop : ca traine et ca ne donne rien -> on sort
        age_j = _f(p.get("age_h")) / 24.0
        if age_j >= self.cfg.time_stop_days and (r is None or r < self.cfg.time_stop_min_r):
            return [{"type": "close", "ticket": tk, "fraction": 1.0,
                     "reason": f"secours: time-stop {age_j:.1f}j sans progression"}]

        # 3. break-even des +1R (une seule fois : uniquement si le stop est encore derriere)
        if r is not None and r >= self.cfg.breakeven_at_r and sl:
            derriere = sl < entry if p.get("direction") == "buy" else sl > entry
            if derriere:
                acts.append({"type": "modify", "ticket": tk, "sl": round(entry, digits),
                             "tp": None, "reason": "secours: stop au break-even (+"
                                                   f"{self.cfg.breakeven_at_r:.1f}R)"})

        # 4. trailing ATR arme si absent -> le suivi automatique fait le reste
        if not p.get("trailing"):
            acts.append({"type": "trail", "ticket": tk, "atr_mult": self.cfg.trail_atr_mult,
                         "pips": None, "activate_r": self.cfg.trail_activate_r,
                         "timeframe": None, "enabled": True,
                         "reason": "secours: trailing automatique arme"})
        return acts
