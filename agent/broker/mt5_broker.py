# -*- coding: utf-8 -*-
"""Adaptateur MetaTrader 5 — la seule porte vers le marche reel.

EXECUTION DIRECTE : chaque ordre est envoye REELLEMENT au marche via order_send().
Il n'y a plus de mode simulation. Si MT5 n'est pas connecte, l'agent ne trade pas.

Les infos symbole (valeur du pip, min/max lot, step, digits) sont lues du terminal
pour que le sizing FTMO soit exact.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config import MT5Config, AgentConfig

log = logging.getLogger("broker")

try:
    import MetaTrader5 as mt5
    _HAS_MT5 = True
except Exception:                       # package absent (ex: hors Windows) -> mode degrade
    mt5 = None
    _HAS_MT5 = False

TIMEFRAMES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H2": 16386, "H4": 16388, "H8": 16392,
    "D1": 16408, "W1": 32769, "MN1": 49153,               # constantes mt5.TIMEFRAME_*
}
_TF = TIMEFRAMES                                          # alias historique


class MT5Broker:
    def __init__(self, mcfg: MT5Config, acfg: AgentConfig):
        self.mcfg = mcfg
        self.acfg = acfg
        self.connected = False

    # ---------------------------------------------------------------- connexion
    def connect(self) -> bool:
        if not _HAS_MT5:
            log.warning("Package MetaTrader5 absent — mode SIMULATION pur (pas de data live).")
            return False
        kwargs = {}
        if self.mcfg.path:
            kwargs["path"] = self.mcfg.path
        if not mt5.initialize(**kwargs):
            log.error("mt5.initialize a echoue: %s", mt5.last_error())
            return False
        if self.mcfg.login:
            ok = mt5.login(self.mcfg.login, password=self.mcfg.password, server=self.mcfg.server)
            if not ok:
                log.error("mt5.login a echoue: %s", mt5.last_error())
                return False
        self.connected = True
        info = mt5.account_info()
        log.info("Connecte MT5 — compte %s, solde %.2f %s",
                 getattr(info, "login", "?"), getattr(info, "balance", 0.0),
                 getattr(info, "currency", ""))
        return True

    def shutdown(self):
        if _HAS_MT5 and self.connected:
            mt5.shutdown()

    # ---------------------------------------------------------------- compte
    def account(self) -> dict:
        if _HAS_MT5 and self.connected:
            a = mt5.account_info()
            if a is not None:
                return {"equity": a.equity, "balance": a.balance, "currency": a.currency,
                        "free_margin": getattr(a, "margin_free", 0.0),
                        "margin_used": getattr(a, "margin", 0.0),
                        "margin_level": getattr(a, "margin_level", 0.0),
                        "leverage": getattr(a, "leverage", 0)}
        # non connecte : pas de compte -> l'orchestrateur ne tradera pas ce cycle.
        return {"equity": 0.0, "balance": 0.0, "currency": "", "free_margin": 0.0,
                "margin_used": 0.0, "margin_level": 0.0, "leverage": 0}

    # ---------------------------------------------------------------- data
    def candles(self, symbol: str, timeframe: str, n: int = 300) -> pd.DataFrame:
        if not (_HAS_MT5 and self.connected):
            return pd.DataFrame()
        tf = TIMEFRAMES.get(timeframe.upper(), 16385)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    def tick(self, symbol: str) -> Optional[dict]:
        if not (_HAS_MT5 and self.connected):
            return None
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        return {"bid": t.bid, "ask": t.ask, "time": t.time}

    def symbols(self, query: str = "", only_watchlist: bool = False, limit: int = 60) -> list[dict]:
        """Univers NEGOCIABLE du broker : l'agent choisit lui-meme ses paires.

        query          : filtre texte sur le nom ou le groupe (ex "EUR", "XAU", "Index").
        only_watchlist : True -> seulement les symboles du Market Watch.
        """
        if not (_HAS_MT5 and self.connected):
            return []
        try:
            infos = mt5.symbols_get() or []
        except Exception as e:                      # pragma: no cover - depend du terminal
            log.warning("symbols_get a echoue: %s", e)
            return []
        q = (query or "").strip().upper()
        out = []
        for si in infos:
            if only_watchlist and not si.visible:
                continue
            if q and q not in si.name.upper() and q not in (si.path or "").upper():
                continue
            if getattr(si, "trade_mode", 4) == 0:    # SYMBOL_TRADE_MODE_DISABLED
                continue
            spread = getattr(si, "spread", 0)
            out.append({"symbol": si.name, "groupe": (si.path or "").split("\\")[0],
                        "description": (si.description or "")[:60],
                        "spread_points": spread, "digits": si.digits,
                        "dans_market_watch": bool(si.visible)})
            if len(out) >= limit:
                break
        return out

    def ensure_symbol(self, symbol: str) -> bool:
        """Rend un symbole utilisable (l'ajoute au Market Watch si besoin).
        False = symbole inconnu du broker -> ne pas trader dessus."""
        if not (_HAS_MT5 and self.connected):
            return False
        si = mt5.symbol_info(symbol)
        if si is None:
            return False
        if not si.visible and not mt5.symbol_select(symbol, True):
            return False
        return True

    def symbol_spec(self, symbol: str) -> dict:
        """Renvoie les parametres necessaires au sizing FTMO exact."""
        if not (_HAS_MT5 and self.connected):
            # valeurs generiques de secours (FX 5 digits)
            return {"digits": 5, "point": 1e-5, "min_lot": 0.01, "max_lot": 100.0,
                    "lot_step": 0.01, "pip_value_per_lot": 10.0, "price_to_pips": 10_000.0}
        si = mt5.symbol_info(symbol)
        if si is None or not si.visible:
            mt5.symbol_select(symbol, True)
            si = mt5.symbol_info(symbol)
        if si is None:                              # symbole inconnu du broker
            return {}
        digits = si.digits
        point = si.point
        # 1 pip = 10 points pour les paires 3/5 digits, sinon 1 point
        pip = point * (10 if digits in (3, 5) else 1)
        price_to_pips = 1.0 / pip
        points_to_pips = point / pip                # 1 point -> fraction de pip
        # valeur d'un pip pour 1 lot = tick_value * (pip / tick_size)
        tick_value = si.trade_tick_value or 1.0
        tick_size = si.trade_tick_size or point
        pip_value_per_lot = tick_value * (pip / tick_size)
        return {
            "digits": digits, "point": point,
            "min_lot": si.volume_min, "max_lot": si.volume_max, "lot_step": si.volume_step,
            "pip_value_per_lot": pip_value_per_lot, "price_to_pips": price_to_pips,
            # --- frictions reelles (spread, distance minimale de stop, portage, marge) ---
            "spread_pips": round(getattr(si, "spread", 0) * points_to_pips, 2),
            "spread_points": getattr(si, "spread", 0),
            "stops_level_pips": round(getattr(si, "trade_stops_level", 0) * points_to_pips, 2),
            "freeze_level_pips": round(getattr(si, "trade_freeze_level", 0) * points_to_pips, 2),
            "contract_size": getattr(si, "trade_contract_size", 0.0),
            "swap_long": getattr(si, "swap_long", 0.0),      # portage par nuit (unites broker)
            "swap_short": getattr(si, "swap_short", 0.0),
            "swap_mode": getattr(si, "swap_mode", 0),
            "swap_rollover3days": getattr(si, "swap_rollover3days", 3),
            "volume_limit": getattr(si, "volume_limit", 0.0),
            "trade_mode": getattr(si, "trade_mode", 4),
            "filling_mode": getattr(si, "filling_mode", 0),
            "currency_profit": getattr(si, "currency_profit", ""),
        }

    def margin_required(self, symbol: str, lot: float, direction: str = "buy") -> float | None:
        """Marge necessaire pour ouvrir `lot` sur `symbol` (None si indisponible)."""
        if not (_HAS_MT5 and self.connected):
            return None
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        otype = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL
        price = t.ask if direction == "buy" else t.bid
        try:
            return mt5.order_calc_margin(otype, symbol, float(lot), price)
        except Exception:                            # pragma: no cover - depend du terminal
            return None

    # ---------------------------------------------------------------- positions
    def positions(self) -> list[dict]:
        if _HAS_MT5 and self.connected:
            pos = mt5.positions_get() or []
            out = []
            for p in pos:
                out.append({
                    "ticket": p.ticket, "symbol": p.symbol,
                    "direction": "buy" if p.type == 0 else "sell",
                    "volume": p.volume, "entry": p.price_open,
                    "sl": p.sl, "tp": p.tp, "floating": p.profit,
                    "open_time": datetime.fromtimestamp(p.time, tz=timezone.utc),
                })
            return out
        return []

    # ---------------------------------------------------------------- execution
    def _filling(self, symbol: str):
        """Mode de remplissage accepte par CE symbole (sinon 'Unsupported filling mode')."""
        si = mt5.symbol_info(symbol)
        mask = getattr(si, "filling_mode", 0) if si else 0
        if mask & 2:
            return mt5.ORDER_FILLING_IOC
        if mask & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def place_order(self, symbol: str, direction: str, lot: float,
                    sl: float, tp: float, comment: str = "AI-FTMO") -> dict:
        """Passe un ordre REEL au marche, avec gestion du requote et mesure du SLIPPAGE.
        Renvoie {ok, ticket, price, requested_price, slippage_pips, spread_pips, message}."""
        if not (_HAS_MT5 and self.connected):
            return {"ok": False, "ticket": 0, "price": 0.0, "message": "MT5 non connecte."}

        spec = self.symbol_spec(symbol)
        p2p = spec.get("price_to_pips", 0.0)
        deviation = int(getattr(self.acfg.execution, "deviation_points", 20))
        retries = max(0, int(getattr(self.acfg.execution, "order_retries", 2)))
        requote = {getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004),
                   getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020),
                   getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021)}
        first_price, r = 0.0, None

        for attempt in range(retries + 1):
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                return {"ok": False, "ticket": 0, "price": 0.0, "message": "pas de cotation"}
            price = t.ask if direction == "buy" else t.bid
            first_price = first_price or price
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": float(lot),
                "type": mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL,
                "price": price, "sl": float(sl), "tp": float(tp),
                "deviation": deviation, "magic": 770077,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling(symbol),
            }
            r = mt5.order_send(req)
            if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
                filled = getattr(r, "price", price) or price
                slip = (filled - first_price) if direction == "buy" else (first_price - filled)
                return {"ok": True, "ticket": getattr(r, "order", 0), "price": filled,
                        "requested_price": first_price,
                        "slippage_pips": round(slip * p2p, 2) if p2p else 0.0,
                        "spread_pips": spec.get("spread_pips", 0.0),
                        "tentatives": attempt + 1, "message": "LIVE OK"}
            code = getattr(r, "retcode", None)
            if code not in requote:
                break
            log.warning("Requote sur %s (retcode %s) — nouvelle tentative %d/%d.",
                        symbol, code, attempt + 1, retries)

        return {"ok": False, "ticket": 0, "price": 0.0, "requested_price": first_price,
                "message": f"retcode={getattr(r, 'retcode', '?')} "
                           f"{getattr(r, 'comment', '')} {mt5.last_error()}"}

    # ---------------------------------------------------------------- gestion positions
    def _find_position(self, ticket: int):
        if not (_HAS_MT5 and self.connected):
            return None
        pos = mt5.positions_get(ticket=ticket)
        return pos[0] if pos else None

    def close_position(self, ticket: int, fraction: float = 1.0,
                       comment: str = "AI-close") -> dict:
        """Ferme (totalement ou partiellement) une position par un DEAL oppose.
        fraction in ]0,1] : 1.0 = clôture totale, 0.5 = clôture de la moitie."""
        p = self._find_position(ticket)
        if p is None:
            return {"ok": False, "message": f"position {ticket} introuvable"}
        si = mt5.symbol_info(p.symbol)
        step = si.volume_step or 0.01
        vol = p.volume * max(0.0, min(1.0, fraction))
        vol = max(si.volume_min, round(round(vol / step) * step, 8))
        vol = min(vol, p.volume)
        t = mt5.symbol_info_tick(p.symbol)
        # sens oppose pour fermer
        if p.type == 0:      # position BUY -> on vend
            otype, price = mt5.ORDER_TYPE_SELL, t.bid
        else:                # position SELL -> on achete
            otype, price = mt5.ORDER_TYPE_BUY, t.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "position": ticket,
            "volume": float(vol), "type": otype, "price": price,
            "deviation": int(getattr(self.acfg.execution, "deviation_points", 20)),
            "magic": 770077, "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(p.symbol),
        }
        r = mt5.order_send(req)
        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        return {"ok": ok, "closed_volume": vol,
                "full": vol >= p.volume - 1e-9,
                "message": "close OK" if ok else str(mt5.last_error())}

    def modify_position(self, ticket: int, sl: float | None = None,
                        tp: float | None = None) -> dict:
        """Modifie le SL et/ou le TP d'une position (break-even, trailing...)."""
        p = self._find_position(ticket)
        if p is None:
            return {"ok": False, "message": f"position {ticket} introuvable"}
        req = {
            "action": mt5.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": ticket,
            "sl": float(sl) if sl is not None else p.sl,
            "tp": float(tp) if tp is not None else p.tp,
            "magic": 770077,
        }
        r = mt5.order_send(req)
        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        return {"ok": ok, "message": "modify OK" if ok else str(mt5.last_error())}

    # ---------------------------------------------------------------- clotures
    def realized_pnl(self, ticket: int) -> float | None:
        """LIVE : PnL realise d'une position fermee (somme des deals). None si indispo."""
        if not (_HAS_MT5 and self.connected):
            return None
        deals = mt5.history_deals_get(position=ticket)
        if not deals:
            return None
        return float(sum(d.profit for d in deals))
