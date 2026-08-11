# -*- coding: utf-8 -*-
"""Moteur de risque FTMO — la couche DETERMINISTE qui a droit de veto sur le LLM.

Le trader (LLM) propose ; ce moteur dispose. Aucune proposition ne devient un
ordre sans passer par `validate()`. C'est ici que vit tout le chassis FTMO :
  - dimensionnement de la position par la distance au stop (jamais "au feeling"),
  - perte journaliere / perte totale,
  - nombre de positions, trades/jour, cooldown apres perte,
  - concentration par symbole.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import FTMOConfig


def currencies_of(symbol: str) -> list[str]:
    """Devises/facteurs portes par un symbole (pour le risque correle)."""
    s = symbol.upper()
    if len(s) >= 6 and s[:6].isalpha():
        return [s[:3], s[3:6]]
    if any(k in s for k in ("US30", "NAS", "SPX", "US500", "NDX", "DJ", "USTEC")):
        return ["USD", "EQUITY"]
    if any(k in s for k in ("GER", "DAX", "EU50", "FRA")):
        return ["EUR", "EQUITY"]
    return [s[:3]]


@dataclass
class AccountState:
    """Photo de l'etat du compte + memoire de la session (fournie par le broker/journal)."""
    equity: float
    balance: float
    day_start_balance: float          # balance a 00:00 serveur (ancre perte journaliere)
    initial_balance: float            # solde de depart du challenge (ancre perte totale)
    open_positions: int
    open_risk_by_symbol: dict[str, float]   # risque $ deja engage par symbole
    trades_today: int
    last_loss_time: Optional[datetime] = None
    # risque $ agrege par DEVISE : EURUSD long + GBPUSD long = un seul pari contre l'USD.
    open_risk_by_currency: dict[str, float] = field(default_factory=dict)
    trading_days: int = 0                   # jours de trading valides (critere FTMO)


@dataclass
class TradeProposal:
    """Ce que le LLM propose. entry/sl/tp en prix. Le lot est calcule ici, PAS par le LLM."""
    symbol: str
    direction: str                    # "buy" | "sell"
    entry: float
    sl: float
    tp: float
    confidence: float = 0.5
    rationale: str = ""


@dataclass
class TradeCosts:
    """Frictions REELLES du symbole/broker. Sans elles, le sizing ment.

    - `spread_pips`      : paye a l'entree (on achete a l'ask, on sort au bid).
    - `slippage_pips`    : provision de derapage, entree + sortie.
    - `commission_per_lot`: commission ALLER-RETOUR par lot (devise du compte).
    - `stops_level_pips` : distance minimale imposee par le broker entre prix et SL/TP.
    - `swap_per_lot_per_night` : portage (important en swing, plusieurs nuits).
    - `atr_pips`         : pour juger si le spread est anormal (spread/ATR).
    - `free_margin` / `margin_required_per_lot` : verification de la marge.
    """
    spread_pips: float = 0.0
    slippage_pips: float = 0.0
    commission_per_lot: float = 0.0
    stops_level_pips: float = 0.0
    swap_per_lot_per_night: float = 0.0
    atr_pips: float = 0.0
    free_margin: float = 0.0
    margin_required_per_lot: float = 0.0
    max_spread_pips: float = 0.0
    max_spread_atr_ratio: float = 0.0
    max_margin_pct_of_free: float = 100.0
    max_risk_per_currency_pct: float = 0.0     # 0 = pas de plafond de correlation


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str]
    lot: float = 0.0
    risk_dollars: float = 0.0
    risk_pct: float = 0.0
    rr: float = 0.0
    rr_net: float = 0.0            # R:R APRES spread, slippage et commission
    costs_dollars: float = 0.0     # cout total estime du trade (aller-retour)
    stop_pips: float = 0.0
    effective_stop_pips: float = 0.0


class FTMOEngine:
    def __init__(self, cfg: FTMOConfig):
        self.cfg = cfg

    # ---- garde-fous "portefeuille" : peut-on encore trader du tout ? ----
    def portfolio_gate(self, acc: AccountState, now: Optional[datetime] = None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        block: list[str] = []
        c = self.cfg

        daily_loss_pct = (acc.day_start_balance - acc.equity) / acc.initial_balance * 100
        total_loss_pct = (acc.initial_balance - acc.equity) / acc.initial_balance * 100
        profit_pct = (acc.equity - acc.initial_balance) / acc.initial_balance * 100

        if daily_loss_pct >= c.daily_stop_pct:
            block.append(f"STOP JOURNALIER agent atteint ({daily_loss_pct:.2f}% >= {c.daily_stop_pct}%).")
        if total_loss_pct >= c.total_soft_stop_pct:
            block.append(f"STOP TOTAL agent atteint ({total_loss_pct:.2f}% >= {c.total_soft_stop_pct}%).")
        if profit_pct >= c.profit_target_pct:
            # Exception : si le critere "minimum de jours de trades" n'est pas encore
            # rempli, arreter d'ouvrir rendrait l'etape INVALIDABLE. On laisse alors
            # passer (le reste des garde-fous s'applique toujours).
            if acc.trading_days >= c.min_trading_days:
                block.append(f"OBJECTIF +{c.profit_target_pct}% ATTEINT ({profit_pct:.2f}%) — "
                             f"on preserve, plus de risque.")
            else:
                block_msg = (f"Objectif atteint mais seulement {acc.trading_days}/"
                             f"{c.min_trading_days} jours de trading : ouvertures encore "
                             f"autorisees pour valider le critere.")
                # information, pas un blocage
                acc.__dict__.setdefault("_notes", []).append(block_msg)
        if acc.open_positions >= c.max_open_positions:
            block.append(f"Max positions simultanees atteint ({acc.open_positions}/{c.max_open_positions}).")
        if acc.trades_today >= c.max_trades_per_day:
            block.append(f"Max trades/jour atteint ({acc.trades_today}/{c.max_trades_per_day}).")
        if acc.last_loss_time is not None:
            mins = (now - acc.last_loss_time).total_seconds() / 60
            if mins < c.cooldown_minutes_after_loss:
                block.append(f"Cooldown post-perte actif ({mins:.0f}/{c.cooldown_minutes_after_loss} min).")
        return block

    # ---- validation + sizing d'une proposition precise ----
    def validate(self, p: TradeProposal, acc: AccountState,
                 pip_value_per_lot: float, price_to_pips: float,
                 min_lot: float, max_lot: float, lot_step: float,
                 now: Optional[datetime] = None,
                 costs: Optional[TradeCosts] = None) -> RiskDecision:
        """Valide ET dimensionne. `costs` = frictions reelles (spread, commission,
        slippage, stop minimum, marge) : sans elles le risque annonce est sous-estime."""
        reasons: list[str] = []
        k = costs or TradeCosts()

        gate = self.portfolio_gate(acc, now)
        if gate:
            return RiskDecision(False, gate)

        # coherence directionnelle du stop / target
        if p.direction == "buy":
            if not (p.sl < p.entry < p.tp):
                reasons.append("Incoherence buy: attendu sl < entry < tp.")
        elif p.direction == "sell":
            if not (p.tp < p.entry < p.sl):
                reasons.append("Incoherence sell: attendu tp < entry < sl.")
        else:
            reasons.append(f"Direction inconnue: {p.direction!r}.")
        if reasons:
            return RiskDecision(False, reasons)

        stop_dist_price = abs(p.entry - p.sl)
        tp_dist_price = abs(p.tp - p.entry)
        if stop_dist_price <= 0:
            return RiskDecision(False, ["Distance au stop nulle."])
        rr = tp_dist_price / stop_dist_price
        if rr < 1.0:
            reasons.append(f"R:R trop faible ({rr:.2f} < 1.0).")

        stop_pips = stop_dist_price * price_to_pips
        tp_pips = tp_dist_price * price_to_pips

        # --- frictions du marche : spread anormal, stop trop proche du prix ---
        if k.max_spread_pips > 0 and k.spread_pips > k.max_spread_pips:
            reasons.append(f"Spread anormal ({k.spread_pips:.1f} pips > "
                           f"{k.max_spread_pips:.1f}) — cout d'entree prohibitif.")
        if k.max_spread_atr_ratio > 0 and k.atr_pips > 0 and \
                k.spread_pips > k.max_spread_atr_ratio * k.atr_pips:
            reasons.append(f"Spread {k.spread_pips:.1f} pips > "
                           f"{k.max_spread_atr_ratio * 100:.0f}% de l'ATR ({k.atr_pips:.1f} pips).")
        if k.stops_level_pips > 0 and stop_pips < k.stops_level_pips:
            reasons.append(f"Stop trop proche du prix ({stop_pips:.1f} pips < minimum broker "
                           f"{k.stops_level_pips:.1f}) — l'ordre serait rejete.")
        if k.stops_level_pips > 0 and tp_pips < k.stops_level_pips:
            reasons.append(f"Cible trop proche du prix ({tp_pips:.1f} pips < minimum broker "
                           f"{k.stops_level_pips:.1f}).")
        if reasons:
            return RiskDecision(False, reasons, rr=round(rr, 2), stop_pips=round(stop_pips, 1))

        # budget de risque = min(risque/trade, marge journaliere restante, marge totale restante)
        c = self.cfg
        base_budget = acc.initial_balance * c.risk_per_trade_pct / 100

        daily_loss_now = acc.day_start_balance - acc.equity
        daily_room = acc.initial_balance * c.daily_stop_pct / 100 - daily_loss_now
        total_loss_now = acc.initial_balance - acc.equity
        total_room = acc.initial_balance * c.total_soft_stop_pct / 100 - total_loss_now

        already = acc.open_risk_by_symbol.get(p.symbol, 0.0)
        symbol_room = acc.initial_balance * c.max_risk_per_symbol_pct / 100 - already

        # CORRELATION : trois paires "contre le dollar" = un seul pari. On plafonne le
        # risque cumule par DEVISE, sinon 3 x 1 % peuvent devenir 3 % sur un seul facteur.
        ccy_room = float("inf")
        ccy_bloquante = ""
        if k.max_risk_per_currency_pct > 0:
            plafond = acc.initial_balance * k.max_risk_per_currency_pct / 100
            for ccy in currencies_of(p.symbol):
                reste = plafond - acc.open_risk_by_currency.get(ccy, 0.0)
                if reste < ccy_room:
                    ccy_room, ccy_bloquante = reste, ccy

        budget = min(base_budget, daily_room, total_room, symbol_room, ccy_room)
        if budget <= 0:
            motif = ("Aucun budget de risque disponible (marges FTMO epuisees)."
                     if budget != ccy_room else
                     f"Exposition {ccy_bloquante} deja au plafond "
                     f"({k.max_risk_per_currency_pct:.1f}% du compte) — trade correle refuse.")
            return RiskDecision(False, [motif])

        # --- PERTE REELLE si le stop est touche : distance + spread + slippage,
        #     plus la commission aller-retour. C'est CA le risque a dimensionner.
        effective_stop_pips = stop_pips + k.spread_pips + k.slippage_pips
        risk_per_lot = effective_stop_pips * pip_value_per_lot + k.commission_per_lot
        if risk_per_lot <= 0:
            return RiskDecision(False, ["Valeur du pip indisponible pour ce symbole."])

        raw_lot = budget / risk_per_lot
        lot = max(min_lot, min(max_lot, self._round_step(raw_lot, lot_step)))
        risk_dollars = lot * risk_per_lot
        risk_pct = risk_dollars / acc.initial_balance * 100

        # gain NET si la cible est atteinte (on sort du mauvais cote du spread aussi)
        gain_dollars = max(0.0, (tp_pips - k.slippage_pips) * pip_value_per_lot * lot
                           - k.commission_per_lot * lot)
        rr_net = gain_dollars / risk_dollars if risk_dollars > 0 else 0.0
        costs_dollars = ((k.spread_pips + 2 * k.slippage_pips) * pip_value_per_lot
                         + k.commission_per_lot) * lot

        if lot < min_lot:
            reasons.append("Lot calcule sous le minimum broker.")
        if risk_dollars > base_budget * 1.05:
            reasons.append(f"Risque {risk_dollars:.0f}$ depasse le budget {base_budget:.0f}$ "
                           f"(frais inclus) — reduis la taille ou elargis le stop.")
        if rr_net < 1.0:
            reasons.append(f"R:R NET trop faible ({rr_net:.2f} apres spread/commission/slippage) "
                           f"— l'esperance ne paie pas les frais.")
        # marge : ne jamais engager plus que X % de la marge libre
        if k.margin_required_per_lot > 0 and k.free_margin > 0:
            margin = k.margin_required_per_lot * lot
            plafond = k.free_margin * k.max_margin_pct_of_free / 100
            if margin > plafond:
                reasons.append(f"Marge requise {margin:.0f}$ > {k.max_margin_pct_of_free:.0f}% "
                               f"de la marge libre ({k.free_margin:.0f}$).")

        approved = not reasons
        if not reasons:
            reasons = [f"OK — risque {risk_pct:.2f}% ({risk_dollars:.0f}$ frais inclus), "
                       f"lot {lot}, R:R brut {rr:.2f} / net {rr_net:.2f}, "
                       f"couts ~{costs_dollars:.0f}$."]
        return RiskDecision(approved, reasons, lot, risk_dollars, risk_pct, round(rr, 2),
                            rr_net=round(rr_net, 2), costs_dollars=round(costs_dollars, 2),
                            stop_pips=round(stop_pips, 1),
                            effective_stop_pips=round(effective_stop_pips, 1))

    @staticmethod
    def _round_step(x: float, step: float) -> float:
        if step <= 0:
            return round(x, 2)
        return round(round(x / step) * step, 8)
