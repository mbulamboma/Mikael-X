# -*- coding: utf-8 -*-
"""Boucle de vie de l'agent trader FTMO (multi-strategies, AWS Bedrock).

L'orchestrateur est aussi le FOURNISSEUR d'acces marche des outils du LLM
(`brain/tools.bind_live`) : l'agent demande le symbole, le timeframe, l'indicateur
ou la recherche d'actualite qu'il veut, et tout est charge a la demande (avec cache
de cycle). La watchlist AGENT_SYMBOLS n'est qu'un pre-scan de depart.

Cycle (toutes les AGENT_LOOP_SECONDS) :
  1. Rafraichit compte + snapshots marche + positions ; resout les clotures.
  2. Pour chaque cloture -> attribue le resultat (R) a la strategie utilisee et met
     a jour le scoreboard + le post-mortem chiffre (aucune "lecon" redigee par le LLM :
     seules des mesures verifiables remontent dans les prompts).
  3. Calcule le regime + le classement des strategies (adequation x track-record).
  4. Garde-fous FTMO de portefeuille (peut-on trader ?).
  5. Le LLM choisit la MEILLEURE strategie et rend trade/wait.
  6. Le moteur de risque valide + dimensionne -> execution REELLE MT5 + memorise
     la strategie de la position pour l'attribution future.

Garde-fou news : pas de nouvelle entree si annonce a fort impact imminente (black-out).

SECOURS SANS IA : si le LLM devient indisponible (Bedrock, credentials, quota...),
`brain/autopilot.py` prend la main — regles Python deterministes, AUCUNE nouvelle
entree, protection des positions ouvertes (SL d'urgence, break-even, trailing,
time-stop, fermeture totale si la perte du jour approche la limite). Des que le book
est vide, le script s'arrete (SafeExit) pour inspection manuelle puis relance.

Lancement :
    python run.py            # un cycle unique (test)
    python run.py --loop     # boucle continue
"""
from __future__ import annotations
import argparse
import logging
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CFG, STATE_DIR
from broker.mt5_broker import MT5Broker, TIMEFRAMES
from data.market import snapshot, _atr
from data import chart, indicators
from data.news import NewsFeed
from data.web import WebResearch
from risk.ftmo import (FTMOEngine, AccountState, TradeProposal, TradeCosts,
                      currencies_of)
from brain.memory import Memory
from brain.autopilot import SafePilot
from brain.postmortem import PostMortem
from brain import tools as T
from desk import journal
from notify import Mailer
import process
from strategy import regime as regime_mod, playbooks
from strategy.scoreboard import Scoreboard

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("run")


class SafeExit(Exception):
    """Arret volontaire : l'IA est indisponible ET le book est vide -> on rend la main
    a l'humain (inspection manuelle puis relance)."""


def _today_key(now: datetime | None = None) -> str:
    """Cle de journee. ATTENTION : la journee FTMO se reinitialise a MINUIT HEURE
    SERVEUR (EET/EEST), pas a minuit UTC — d'ou l'horloge serveur passee en argument
    par l'orchestrateur (broker.server_now())."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _position_risk(pos: dict, spec: dict) -> float:
    if not pos.get("sl"):
        return 0.0
    stop_pips = abs(pos["entry"] - pos["sl"]) * spec["price_to_pips"]
    return stop_pips * spec["pip_value_per_lot"] * pos["volume"]


class Orchestrator:
    def __init__(self):
        self.cfg = CFG
        self.broker = MT5Broker(CFG.mt5, CFG)
        self.risk = FTMOEngine(CFG.ftmo)
        self.mem = Memory()
        self.news = NewsFeed(CFG.news)
        self.web = WebResearch(CFG.web)
        self.pilot = SafePilot(CFG.safe, CFG.ftmo)     # secours deterministe (sans LLM)
        self.mail = Mailer(CFG.mail)                   # notifications (trades, urgences)
        self.agent = None
        self._degrade_signale = False                  # une seule alerte par panne d'IA
        self._last_summary: dict = {}                  # etat compte pour les emails
        self._prev_tickets: set[int] = set()
        self._tickets_init = False
        # caches de CYCLE : specs et bougies deja chargees (l'agent peut demander
        # n'importe quel symbole / timeframe, on ne retelecharge pas deux fois).
        self._spec_cache: dict[str, dict] = {}
        self._bars_cache: dict[tuple[str, str], "object"] = {}
        self._last_regime = "unknown"          # regime dominant du cycle (trace post-mortem)
        self._server_now: datetime | None = None   # horloge serveur (journee FTMO)
        self._vigie_last: dict[str, datetime] = {}  # anti-spam de la vigie, par ticket

    # -------------------------------------------------------- acces marche a la demande
    def _spec(self, symbol: str) -> dict:
        """Spec d'un symbole QUELCONQUE (l'ajoute au Market Watch si besoin).
        {} = symbole inconnu du broker -> on ne trade pas dessus."""
        sym = symbol.strip().upper()
        if sym in self._spec_cache:
            return self._spec_cache[sym]
        spec = self.broker.symbol_spec(sym) if self.broker.ensure_symbol(sym) else {}
        self._spec_cache[sym] = spec
        return spec

    def _bars(self, symbol: str, timeframe: str, n: int = 300):
        tf = (timeframe or self.cfg.timeframe).strip().upper()
        if tf not in TIMEFRAMES:
            tf = self.cfg.timeframe
        key = (symbol.strip().upper(), tf)
        if key not in self._bars_cache:
            self._bars_cache[key] = self.broker.candles(key[0], tf, n)
        return self._bars_cache[key]

    # ---- API exposee aux OUTILS du LLM (brain/tools.bind_live) -------------------
    # C'est ce qui rend l'agent libre : il choisit son symbole, son timeframe,
    # ses indicateurs et ses recherches d'actualite, sans passer par la watchlist.
    def symbols(self, query: str = "", only_watchlist: bool = False, limit: int = 40) -> list:
        return self.broker.symbols(query=query, only_watchlist=only_watchlist, limit=limit)

    def market(self, symbol: str, timeframe: str | None = None) -> dict:
        spec = self._spec(symbol)
        if not spec:
            return {"error": f"{symbol} inconnu ou non negociable chez ce broker"}
        return snapshot(symbol.upper(), self._bars(symbol, timeframe or self.cfg.timeframe), spec)

    def chart(self, symbol: str, timeframes: list[str], candles: int = 10) -> dict:
        spec = self._spec(symbol)
        if not spec:
            return {"error": f"{symbol} inconnu ou non negociable chez ce broker"}
        tfs = [t for t in (timeframes or []) if t in TIMEFRAMES] or list(self.cfg.chart_timeframes)
        frames = {tf: self._bars(symbol, tf) for tf in tfs}
        return chart.read(symbol.upper(), frames, spec, candles=candles)

    def indicator(self, symbol: str, timeframe: str, name: str, params: dict) -> dict:
        spec = self._spec(symbol)
        if not spec:
            return {"error": f"{symbol} inconnu ou non negociable chez ce broker"}
        df = self._bars(symbol, timeframe)
        res = indicators.compute(name, df, digits=spec.get("digits", 5), **(params or {}))
        return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), **res}

    def news_for(self, symbol: str) -> dict:
        return self.news.for_symbols([symbol.upper()]).get("per_symbol", {}).get(symbol.upper(), {})

    def news_search(self, query: str, hours: int = 48) -> dict:
        return self.news.search(query, hours)

    def major_events(self, hours: int = 72) -> dict:
        return self.news.major_events(hours)

    def fred_series(self, series_id: str, limit: int = 12) -> dict:
        return self.news.fred_series(series_id, limit)

    # ---- enquete web (analyse macro / d'expert) ; budget par cycle dans WebResearch
    def web_search(self, query: str, limit: int = 6) -> dict:
        return self.web.search(query, limit)

    def web_read(self, url: str, max_chars: int | None = None) -> dict:
        return self.web.read(url, max_chars)

    # Recherches / lectures EN PARALLELE : plusieurs pages en un seul aller-retour de
    # temps mural au lieu de N appels sequentiels (cf. outils web_search_multiple /
    # web_read_multiple). Le budget web par cycle reste partage et respecte.
    def multiple_search(self, queries: list[str], limit: int = 6) -> list[dict]:
        return self.web.multiple_search(queries, limit)

    def multiple_read(self, urls: list[str], max_chars: int | None = None) -> list[dict]:
        return self.web.multiple_read(urls, max_chars)

    def retail_sentiment(self, symbol: str = "") -> dict:
        return self.web.retail_sentiment(symbol)

    # -------------------------------------------------------- couts et frictions reelles
    def costs_view(self, symbol: str, lot: float = 0.0, direction: str = "buy") -> dict:
        """Ce que coute VRAIMENT un trade sur ce symbole (vue lisible pour le LLM)."""
        spec = self._spec(symbol)
        if not spec:
            return {"error": f"{symbol} inconnu ou non negociable chez ce broker"}
        e = self.cfg.execution
        pipv = spec.get("pip_value_per_lot", 0.0)
        atr_pips = self._atr_pips(symbol, spec)
        lot = lot or self.cfg.ftmo.account_size * self.cfg.ftmo.risk_per_trade_pct / 100 / max(
            (atr_pips or 20) * pipv, 1e-9)
        lot = round(max(spec.get("min_lot", 0.01), lot), 2)
        spread = spec.get("spread_pips", 0.0)
        swap = spec.get("swap_long" if direction == "buy" else "swap_short", 0.0)
        marge = self.broker.margin_required(symbol, lot, direction)
        return {
            "symbole": symbol, "lot_reference": lot,
            "valeur_pip_par_lot": round(pipv, 4),
            "spread_pips": spread,
            "spread_cout": round(spread * pipv * lot, 2),
            "spread_pct_atr": round(spread / atr_pips * 100, 1) if atr_pips else None,
            "commission_aller_retour": round(e.commission_per_lot * lot, 2),
            "slippage_provision_pips": e.slippage_pips,
            "cout_total_estime": round(((spread + 2 * e.slippage_pips) * pipv
                                        + e.commission_per_lot) * lot, 2),
            "swap_par_nuit_par_lot": swap,
            "swap_3x_le": spec.get("swap_rollover3days"),
            "stop_minimum_broker_pips": spec.get("stops_level_pips", 0.0),
            "atr_pips": round(atr_pips, 1) if atr_pips else None,
            "marge_requise": round(marge, 2) if marge else None,
            "marge_libre": round(self.broker.account().get("free_margin", 0.0), 2),
            "gap_week_end_provision_pips": round(e.gap_provision_atr * atr_pips, 1) if atr_pips else None,
            "note": "Le stop REEL paye = distance + spread + slippage ; la commission "
                    "s'ajoute. Un stop trop serre est mange par les frais : vise un stop "
                    "large devant le spread et un R:R NET >= 2.",
        }

    def _atr_pips(self, symbol: str, spec: dict) -> float:
        df = self._bars(symbol, self.cfg.timeframe)
        if df is None or df.empty or len(df) < 20:
            return 0.0
        try:
            return _atr(df, 14) * spec.get("price_to_pips", 0.0)
        except Exception:
            return 0.0

    def _costs(self, symbol: str, spec: dict, direction: str) -> TradeCosts:
        """Frictions passees au moteur de risque pour le sizing et les vetos."""
        e = self.cfg.execution
        acct = self.broker.account()
        marge_1lot = self.broker.margin_required(symbol, 1.0, direction) or 0.0
        return TradeCosts(
            spread_pips=spec.get("spread_pips", 0.0),
            slippage_pips=e.slippage_pips,
            commission_per_lot=e.commission_per_lot,
            stops_level_pips=spec.get("stops_level_pips", 0.0),
            swap_per_lot_per_night=spec.get("swap_long" if direction == "buy" else "swap_short", 0.0),
            atr_pips=self._atr_pips(symbol, spec),
            free_margin=acct.get("free_margin", 0.0),
            margin_required_per_lot=marge_1lot,
            max_spread_pips=e.max_spread_pips,
            max_spread_atr_ratio=e.max_spread_atr_ratio,
            max_margin_pct_of_free=e.max_margin_pct_of_free,
            max_risk_per_currency_pct=e.max_risk_per_currency_pct)

    def _entry_guard(self, sym: str, spec: dict, prop: TradeProposal) -> tuple[TradeProposal, str]:
        """Recale l'entree sur le PRIX REEL avant le sizing — ou refuse si elle est trop loin.

        Les ordres partent AU MARCHE (`TRADE_ACTION_DEAL`) : le niveau `entry` propose par
        le LLM n'est JAMAIS le prix d'execution. Il sert uniquement a calculer la distance
        au stop, donc le LOT et le R:R. Si le LLM annonce une entree a 40 pips du marche,
        le lot est dimensionne sur une distance fictive et le risque reellement pris n'est
        pas celui du budget. Deux garde-fous :

          1. derive > `AGENT_MAX_ENTRY_DRIFT_ATR` x ATR  -> REFUS (niveau perime ou invente :
             ce n'est plus le trade qui a ete raisonne) ;
          2. sinon on recale `entry` sur le prix executable (ask si buy, bid si sell). Le
             R:R est alors recalcule sur la realite — si le prix a couru vers la cible, le
             moteur refusera de lui-meme (R:R devenu insuffisant).

        Renvoie (proposition, raison_de_refus). Raison vide = on continue.
        """
        e = self.cfg.execution
        tick = self.broker.tick(sym)
        if not tick:
            return prop, ""                     # pas de cotation : le broker tranchera
        prix = tick["ask"] if prop.direction == "buy" else tick["bid"]
        if prix <= 0:
            return prop, ""
        p2p = spec.get("price_to_pips", 0.0)
        derive_pips = abs(prop.entry - prix) * p2p
        atr_pips = self._atr_pips(sym, spec)
        if e.max_entry_drift_atr > 0 and atr_pips > 0 and \
                derive_pips > e.max_entry_drift_atr * atr_pips:
            return prop, (f"entree proposee {prop.entry} a {derive_pips:.1f} pips du prix reel "
                          f"{prix} (> {e.max_entry_drift_atr:.2f} x ATR = "
                          f"{e.max_entry_drift_atr * atr_pips:.1f} pips)")
        if not e.entry_reprice or derive_pips <= 0:
            return prop, ""
        log.info("Entree %s recalee sur le marche: %s -> %s (%.1f pips) — sizing sur le reel.",
                 sym, prop.entry, prix, derive_pips)
        return replace(prop, entry=prix), ""

    def _weekend_guard(self, now: datetime | None = None) -> str:
        """Pas de NOUVELLE entree juste avant la fermeture hebdomadaire (risque de gap)."""
        e = self.cfg.execution
        if not e.weekend_guard:
            return ""
        now = now or self.server_now()
        if now.weekday() == 4 and now.hour >= e.friday_cutoff_utc:      # vendredi
            return (f"garde week-end: apres {e.friday_cutoff_utc}h (heure serveur) le "
                    f"vendredi, risque de gap a la reouverture (AGENT_WEEKEND_GUARD=0)")
        return ""

    def server_now(self) -> datetime:
        """Horloge SERVEUR (journee FTMO). Mise en cache le temps d'un cycle."""
        if self._server_now is None:
            try:
                self._server_now = self.broker.server_now()
            except Exception:
                self._server_now = datetime.now(timezone.utc)
        return self._server_now

    # -------------------------------------------------------- session / FTMO state
    def _session(self, account: dict) -> dict:
        s = self.mem.load_session()
        today = _today_key(self.server_now())          # journee = celle du serveur FTMO
        ph = self.cfg.ftmo.phase
        # (re)demarrage d'une ETAPE : re-ancre solde initial + fenetre 30j + jours de trading
        if s.get("phase") != ph:
            log.info("Nouvelle ETAPE %s -> re-ancrage du solde initial et de la fenetre.", ph)
            s.update({"phase": ph,
                      "initial_balance": account["balance"] or self.cfg.ftmo.account_size,
                      "phase_start": self.cfg.ftmo.phase_start or today,
                      "trading_days": [], "day": today,
                      "day_start_balance": account["balance"], "trades_today": 0,
                      "last_loss_iso": None})
        if s.get("initial_balance") is None:
            s["initial_balance"] = account["balance"] or self.cfg.ftmo.account_size
        if s.get("day") != today:
            s["day"] = today
            s["day_start_balance"] = account["balance"]
            s["trades_today"] = 0
        s.setdefault("day_start_balance", account["balance"])
        s.setdefault("trades_today", 0)
        s.setdefault("last_loss_iso", None)
        s.setdefault("phase_start", self.cfg.ftmo.phase_start or today)
        s.setdefault("trading_days", [])
        self.mem.save_session(s)
        return s

    def _phase_info(self, s: dict, total_pnl_pct: float) -> dict:
        """Contexte de l'etape : objectif, ecart, jours ecoules/restants, jours de trading."""
        c = self.cfg.ftmo
        try:
            d0 = datetime.fromisoformat(str(s.get("phase_start"))).date()
        except (TypeError, ValueError):
            d0 = datetime.now(timezone.utc).date()
        elapsed = (datetime.now(timezone.utc).date() - d0).days
        target = c.profit_target_pct
        return {
            "etape": c.phase,
            "objectif_etape_pct": target,
            "reste_avant_objectif_pct": round(target - total_pnl_pct, 2),
            "objectif_atteint": total_pnl_pct >= target,
            "jours_ecoules": elapsed,
            "jours_restants": max(0, c.phase_days - elapsed),
            "jours_trades": len(set(s.get("trading_days", []))),
            "jours_trades_min": c.min_trading_days,
        }

    def _account_state(self, account: dict, positions: list, s: dict) -> AccountState:
        """`positions` inclut les positions d'AUTRES EA : elles pesent sur l'equity donc
        sur les limites FTMO, et sur le risque correle. On ne les pilote jamais."""
        risk_by_sym: dict[str, float] = {}
        risk_by_ccy: dict[str, float] = {}
        for p in positions:
            spec = self._spec(p["symbol"])
            if not spec:
                continue
            r = _position_risk(p, spec)
            risk_by_sym[p["symbol"]] = risk_by_sym.get(p["symbol"], 0.0) + r
            for ccy in currencies_of(p["symbol"]):
                risk_by_ccy[ccy] = risk_by_ccy.get(ccy, 0.0) + r
        last_loss = datetime.fromisoformat(s["last_loss_iso"]) if s.get("last_loss_iso") else None
        return AccountState(
            equity=account["equity"], balance=account["balance"],
            # tolerant : le watchdog peut tourner avant le premier cycle complet
            day_start_balance=s.get("day_start_balance") or account["balance"],
            initial_balance=s.get("initial_balance") or account["balance"],
            open_positions=len(positions), open_risk_by_symbol=risk_by_sym,
            open_risk_by_currency=risk_by_ccy,
            trading_days=len(set(s.get("trading_days", []))),
            trades_today=int(s.get("trades_today", 0)), last_loss_time=last_loss)

    def _account_summary(self, acc: AccountState) -> dict:
        c = self.cfg.ftmo
        daily_loss_pct = (acc.day_start_balance - acc.equity) / acc.initial_balance * 100
        total_pnl_pct = (acc.equity - acc.initial_balance) / acc.initial_balance * 100
        return {
            "equity": round(acc.equity, 2), "balance": round(acc.balance, 2),
            "pnl_total_pct": round(total_pnl_pct, 2), "objectif_pct": c.profit_target_pct,
            "reste_avant_objectif_pct": round(c.profit_target_pct - total_pnl_pct, 2),
            "perte_jour_pct": round(max(0.0, daily_loss_pct), 2),
            "stop_jour_agent_pct": c.daily_stop_pct,
            "positions_ouvertes": acc.open_positions, "trades_du_jour": acc.trades_today,
            "budget_risque_trade_pct": c.risk_per_trade_pct,
        }

    # -------------------------------------------------------- notifications
    def _mail_context(self) -> tuple[dict, list]:
        """Etat du compte + positions enrichies, pour le corps des emails."""
        try:
            meta = self.mem.load_meta()
            positions = self._enrich_positions(self.broker.positions(), meta)
        except Exception:
            positions = []
        return self._last_summary, positions

    def _notifier(self, methode: str, *args, **kwargs):
        """Envoie une notification sans jamais faire echouer le cycle."""
        try:
            summary, positions = self._mail_context()
            getattr(self.mail, methode)(*args, **kwargs, summary=summary, positions=positions)
        except Exception as e:
            log.warning("Notification %s impossible: %s", methode, e)

    def _lazy_agent(self):
        """Choisit le cerveau selon AGENT_MODE : DESK multi-agents (defaut) ou agent SOLO
        (repli historique). Les deux exposent le meme contrat (decide/degraded)."""
        if self.agent is None:
            if self.cfg.desk.mode == "solo":
                from brain.agent import TraderAgent
                self.agent = TraderAgent(self.cfg)
                log.info("Cerveau: agent SOLO (AGENT_MODE=solo).")
            else:
                from desk.desk import TradingDesk
                self.agent = TradingDesk(self.cfg)
                log.info("Cerveau: DESK multi-agents (AGENT_MODE=desk).")
        return self.agent

    # -------------------------------------------------------- regime + strategies
    def _dominant_regime(self, snaps: dict) -> str:
        counts: dict[str, int] = {}
        for snap in snaps.values():
            r = regime_mod.classify(snap)
            if r != "unknown":
                counts[r] = counts.get(r, 0) + 1
        return max(counts, key=counts.get) if counts else "range"

    def _strategies_block(self, snaps: dict, sb: Scoreboard) -> str:
        lines = ["Regimes par symbole :"]
        for sym, snap in snaps.items():
            lines.append(f"- {sym}: {regime_mod.classify(snap)}")
        dominant = self._dominant_regime(snaps)
        self._last_regime = dominant          # trace pour le post-mortem des trades ouverts
        lines.append("\nClassement des strategies (regime dominant = %s) :" % dominant)
        lines.append(sb.as_prompt_block(dominant))
        return "\n".join(lines)

    # -------------------------------------------------------- clotures + attribution
    def _record_close(self, meta: dict, pnl: float | None, exit_price: float | None):
        strat = meta.get("strategy", "unknown")
        risk = meta.get("risk_dollars", 0.0)
        rr = meta.get("rr", 0.0)
        result = meta.get("result")
        # R : PnL reel MT5 ; si l'historique n'est pas encore dispo, R=0 (neutre)
        if pnl is None:
            R = rr if result == "tp" else (-1.0 if result == "sl" else 0.0)
            pnl = round(risk * R, 2)
        else:
            R = round(pnl / risk, 2) if risk else 0.0
        trade = {"ticket": meta.get("ticket"), "symbol": meta.get("symbol"),
                 "strategy": strat, "direction": meta.get("direction"),
                 "entry": meta.get("entry"), "exit": exit_price,
                 "R": R, "pnl": pnl, "result": result, "closed_at": _today_key(self.server_now()),
                 # --- matiere du POST-MORTEM : plan vs realite ---
                 "rr_planifie": rr, "rr_net_planifie": meta.get("rr_net"),
                 "mfe_R": meta.get("mfe_R"), "mae_R": meta.get("mae_R"),
                 "slippage_pips": meta.get("slippage_pips"),
                 "spread_pips_entree": meta.get("spread_pips_entree"),
                 "couts_estimes": meta.get("couts_estimes"),
                 "regime": meta.get("regime"), "confidence": meta.get("confidence"),
                 "duree_h": self._duree_h(meta.get("ouvert_le")),
                 "entry_planifiee": meta.get("entry_planifiee"),
                 "derive_entree_pips": meta.get("derive_entree_pips"),
                 # trace de QUI a decide : c'est la matiere de la revue par employe
                 "dossier": meta.get("dossier") or {}}
        self.mem.log_event("trade_closed", trade)
        log.info("Cloture %s [%s] -> R=%.2f, PnL=%.2f", meta.get("symbol"), strat, R, pnl)
        # Pas de "lecon" ecrite par le LLM a la cloture : le trade journalise ci-dessus
        # alimente des mesures (post-mortem chiffre, scoreboard, revue par employe, cas
        # comparables) qui, elles, sont verifiables et se relisent sans y croire sur parole.
        self._notifier("trade_ferme", trade)
        # POINT 3 : reflexion hybride a la cloture (note factuelle bornee + sourcee). Present
        # sur le DESK uniquement ; l'agent solo ne l'expose pas. Ne bloque jamais la cloture.
        reflect = getattr(self.agent, "reflect_on_close", None)
        if callable(reflect):
            try:
                reflect(trade)
            except Exception as e:
                log.warning("Reflexion post-cloture ignoree (%s).", e)
        return R

    @staticmethod
    def _duree_h(iso: str | None) -> float | None:
        if not iso:
            return None
        try:
            t0 = datetime.fromisoformat(str(iso))
        except (TypeError, ValueError):
            return None
        return round((datetime.now(timezone.utc) - t0).total_seconds() / 3600, 1)

    def _handle_closes(self, positions: list, meta: dict, s: dict):
        """Detecte les positions fermees (ticket disparu) et attribue le resultat."""
        cur = {p["ticket"] for p in positions}
        if not self._tickets_init:
            # premier cycle : on ancre les positions deja ouvertes sans les "fermer"
            self._prev_tickets = cur
            self._tickets_init = True
            return
        for tk in self._prev_tickets - cur:
            m = meta.pop(str(tk), {"ticket": tk})
            pnl = self.broker.realized_pnl(tk)
            R = self._record_close(m, pnl, None)
            if R < 0:
                s["last_loss_iso"] = datetime.now(timezone.utc).isoformat()
        self._prev_tickets = cur
        self.mem.save_meta(meta)
        self.mem.save_session(s)

    # -------------------------------------------------------- vue des positions
    def _enrich_positions(self, positions: list, meta: dict) -> list:
        """Ajoute a chaque position : profit flottant en R, distance SL/TP, strategie.
        Met aussi a jour MFE/MAE (meilleur et pire point atteint, en R) : matiere
        premiere du post-mortem (stop trop serre ? sortie trop tot ?)."""
        out = []
        touched = False
        for p in positions:
            spec = self._spec(p["symbol"])
            m = meta.get(str(p["ticket"]), {})
            risk = m.get("risk_dollars", 0.0)
            if risk:
                r_now = p.get("floating", 0.0) / risk
                if r_now > m.get("mfe_R", 0.0):
                    m["mfe_R"], touched = round(r_now, 2), True
                if r_now < m.get("mae_R", 0.0):
                    m["mae_R"], touched = round(r_now, 2), True
            price = p["entry"]
            tk = self.broker.tick(p["symbol"])
            if tk:
                price = tk["bid"] if p["direction"] == "buy" else tk["ask"]
            p2p = spec.get("price_to_pips", 0.0)
            out.append({
                "ticket": p["ticket"], "symbol": p["symbol"], "direction": p["direction"],
                "volume": p["volume"], "entry": p["entry"], "price_now": round(price, spec.get("digits", 5)),
                "sl": p["sl"], "tp": p["tp"],
                "strategy": m.get("strategy", "?"),
                "trailing": m.get("trail") if m.get("trail", {}).get("enabled") else None,
                "floating_R": round(p.get("floating", 0.0) / risk, 2) if risk else None,
                "floating_pnl": round(p.get("floating", 0.0), 2),
                "dist_sl_pips": round(abs(price - p["sl"]) * p2p, 1) if p["sl"] else None,
                "dist_tp_pips": round(abs(p["tp"] - price) * p2p, 1) if p["tp"] else None,
                "mfe_R": m.get("mfe_R"), "mae_R": m.get("mae_R"),
                "age_h": round((datetime.now(timezone.utc) - p["open_time"]).total_seconds() / 3600, 1)
                if p.get("open_time") else None,
            })
        if touched:
            self.mem.save_meta(meta)
        return out

    # -------------------------------------------------------- trailing stop automatique
    def _apply_trailing(self, meta: dict):
        """Maintient a CHAQUE cycle les trailing stops armes par l'agent : remonte le SL
        derriere le prix (jamais dans le mauvais sens), une fois le seuil d'activation atteint.
        Fonctionne pour tout symbole, meme hors watchlist (donnees chargees a la demande)."""
        changed = False
        for p in self.broker.positions():
            m = meta.get(str(p["ticket"]))
            tr = m.get("trail") if m else None
            if not tr or not tr.get("enabled", True):
                continue
            spec = self._spec(p["symbol"])
            digits = spec.get("digits", 5)
            # distance de suivi (en prix)
            if tr.get("pips"):
                dist = tr["pips"] / spec.get("price_to_pips", 10_000.0)
            elif tr.get("atr_mult"):
                df = self._bars(p["symbol"], tr.get("timeframe") or self.cfg.timeframe)
                if df is None or df.empty:
                    continue
                dist = tr["atr_mult"] * _atr(df, 14)
            else:
                continue
            tk = self.broker.tick(p["symbol"])
            price = (tk["bid"] if p["direction"] == "buy" else tk["ask"]) if tk else p["entry"]
            # activation par R (securiser le break-even d'abord)
            risk = (m or {}).get("risk_dollars", 0.0)
            floating_r = p.get("floating", 0.0) / risk if risk else tr.get("activate_r", 1.0)
            if floating_r < tr.get("activate_r", 1.0):
                continue
            if p["direction"] == "buy":
                new_sl = round(price - dist, digits)
                better = new_sl > (p["sl"] or -1e18) and new_sl < price
            else:
                new_sl = round(price + dist, digits)
                better = (not p["sl"] or new_sl < p["sl"]) and new_sl > price
            if better:
                res = self.broker.modify_position(p["ticket"], sl=new_sl)
                if res.get("ok"):
                    log.info("TRAILING ticket %s -> SL %.5f (%s)", p["ticket"], new_sl, p["symbol"])
                    m["sl"] = new_sl
                    changed = True
        if changed:
            self.mem.save_meta(meta)

    # -------------------------------------------------------- filet deterministe (sans IA)
    def _protect(self, positions: list, etrangeres: list, summary: dict, meta: dict,
                 s: dict, account: dict) -> bool:
        """PROTECTIONS QUI NE DEPENDENT PAS DU LLM, appliquees a chaque cycle ET par le
        watchdog entre deux cycles. Renvoie True si une URGENCE a ete traitee (l'IA n'a
        alors pas son mot a dire ce tour-ci).

        1. Perte du jour proche de la limite / perte totale -> on ferme TOUT (nos positions).
        2. Position sans stop -> SL d'urgence ; +1R -> break-even ; trailing arme.
        3. Vendredi apres le cutoff -> mise a plat si AGENT_WEEKEND_FLATTEN=1.

        Rappel : les positions d'un autre EA comptent dans la perte (elles pesent sur
        l'equity FTMO) mais ne sont JAMAIS fermees par l'agent — on alerte a la place.
        """
        if not positions and not etrangeres:
            return False
        pos_view = self._enrich_positions(positions, meta)
        panique = self.pilot.panic_reason(summary)

        if panique:
            log.error("URGENCE FTMO — %s : fermeture immediate de %d position(s).",
                      panique, len(positions))
            self.mem.log_event("urgence_ftmo", {"raison": panique, "positions": len(positions),
                                                "etrangeres": len(etrangeres), **summary})
            for p in positions:
                self._exec_close({"ticket": p["ticket"], "fraction": 1.0,
                                  "reason": f"urgence: {panique}"[:24]}, meta)
            if etrangeres:
                log.error("ATTENTION : %d position(s) hors agent restent ouvertes et pesent "
                          "sur la perte du jour — intervention MANUELLE requise.",
                          len(etrangeres))
            self._last_summary = summary
            self._notifier("alerte", "URGENCE FTMO — fermeture totale", panique,
                           action_requise=(
                               f"{len(etrangeres)} position(s) hors agent restent ouvertes : "
                               f"a fermer MANUELLEMENT." if etrangeres else
                               "Verifier le compte et la cause de la perte avant de relancer."))
            return True

        if self._flatten_weekend_due():
            log.warning("MISE A PLAT WEEK-END : fermeture de %d position(s) avant la "
                        "coupure hebdomadaire (risque de gap).", len(positions))
            self.mem.log_event("weekend_flatten", {"positions": len(positions)})
            for p in positions:
                self._exec_close({"ticket": p["ticket"], "fraction": 1.0,
                                  "reason": "week-end: gap"}, meta)
            return bool(positions)

        # protections position par position (stop d'urgence, break-even, trailing)
        atr_by_sym = {}
        for p in pos_view:
            df = self._bars(p["symbol"], self.cfg.timeframe)
            if df is not None and not df.empty:
                try:
                    atr_by_sym[p["symbol"]] = _atr(df, 14)
                except Exception:
                    pass
        actions = self.pilot.protective_actions(pos_view, atr_by_sym, self._spec)
        if actions:
            log.info("Protections deterministes: %s",
                     [f"{a['type']} #{a['ticket']}" for a in actions])
            pos_by_tk = {p["ticket"]: p for p in positions}
            for a in actions:
                if a["type"] == "modify":
                    self._exec_modify(a, pos_by_tk, meta)
                elif a["type"] == "trail":
                    self._exec_trail(a, meta)
                elif a["type"] == "close":
                    self._exec_close(a, meta)
        return False

    def _flatten_weekend_due(self) -> bool:
        e = self.cfg.execution
        if not e.weekend_flatten:
            return False
        now = self.server_now()
        return now.weekday() == 4 and now.hour >= e.friday_cutoff_utc

    def watchdog(self):
        """Tour de garde SANS IA (toutes les AGENT_WATCH_SECONDS) : on ne decide rien,
        on protege. C'est ce qui evite de decouvrir une perte a -5 % une heure trop tard."""
        if not self.broker.connected:
            return
        self._server_now = None
        self._spec_cache.clear()
        self._bars_cache.clear()
        account = self.broker.account()
        if account["balance"] <= 0:
            return
        positions = self.broker.positions()
        etrangeres = [p for p in self.broker.positions(own_only=False) if not p.get("a_nous")]
        if not positions and not etrangeres:
            return
        s = self.mem.load_session()
        if not s.get("initial_balance"):
            return
        meta = self.mem.load_meta()
        acc = self._account_state(account, positions + etrangeres, s)
        summary = self._account_summary(acc)
        self._last_summary = summary
        if self._protect(positions, etrangeres, summary, meta, s, account):
            return                       # urgence deja traitee : on ne reveille personne
        self._vigie_watch(self.broker.positions(), meta, summary)

    # -------------------------------------------------------- vigie & session extraordinaire
    def _vigie_triggers(self, p: dict) -> list[str]:
        """Declencheurs DETERMINISTES et bon marche : c'est ce qui autorise (ou non) a
        reveiller la Vigie. Sans eux, on paierait un appel LLM par position toutes les
        60 secondes pour ne rien apprendre."""
        d = self.cfg.desk
        out = []
        fr = p.get("floating_R")
        mfe = p.get("mfe_R") or 0.0
        if fr is not None and fr <= d.vigie_alert_r:
            out.append(f"perte flottante {fr}R (seuil {d.vigie_alert_r}R)")
        if fr is not None and mfe >= d.vigie_giveback_r and fr <= mfe * 0.5:
            out.append(f"gains rendus: MFE {mfe}R -> {fr}R")
        bo = self.news.blackout_for(p["symbol"]) if self.cfg.news.enabled else {}
        if bo.get("active"):
            out.append(f"news imminente: {bo.get('reason')} (dans {bo.get('in_hours')}h)")
        return out

    def _vigie_watch(self, positions: list, meta: dict, summary: dict):
        """Boucle d'escalade Vigie -> DG -> session extraordinaire, ENTRE deux cycles.

        Distincte des protections deterministes (deja passees juste avant) : elle sert aux
        degradations qu'un seuil ne sait pas voir (these cassee, news, gains rendus). Elle ne
        peut que REDUIRE le risque : les ouvertures sont filtrees ici, pas seulement cote desk.
        Toute panne (LLM, agent solo sans vigie) est silencieuse : le filet deterministe reste."""
        d = self.cfg.desk
        watch = getattr(self.agent, "watch", None)
        if not d.vigie_enabled or not positions or watch is None \
                or getattr(self.agent, "degraded", False):
            return
        pos_view = self._enrich_positions(positions, meta)
        now = datetime.now(timezone.utc)
        alertes = []
        for p in pos_view:
            declencheurs = self._vigie_triggers(p)
            if not declencheurs:
                continue
            precedent = self._vigie_last.get(str(p["ticket"]))
            if precedent and (now - precedent).total_seconds() < d.vigie_min_minutes * 60:
                continue                     # anti-spam : un meme ticket n'est pas harcele
            self._vigie_last[str(p["ticket"])] = now
            alertes.append({"position": p, "declencheurs": declencheurs})
        if not alertes:
            return
        log.info("Vigie reveillee sur %d position(s): %s", len(alertes),
                 [a["position"]["ticket"] for a in alertes])
        # dossier FRAIS pour la vigie (le contexte du dernier cycle a vieilli)
        T.bind_context({}, {}, summary, pos_view, "", "", self.news.snapshot(
            [p["symbol"] for p in pos_view]))
        T.bind_live(self)
        try:
            actions = watch(alertes, summary) or []
        except Exception as e:
            log.warning("Boucle vigie interrompue (%s) — protections deterministes seules.", e)
            return
        # SECURITE : une session extraordinaire ne peut QUE reduire le risque.
        actions = [a for a in actions if a.get("type") in ("close", "modify", "trail")]
        if not actions:
            return
        self.mem.log_event("session_extraordinaire",
                           {"alertes": alertes, "actions": actions, **summary})
        pos_by_tk = {p["ticket"]: p for p in self.broker.positions()}
        for a in actions:
            if a["type"] == "close":
                self._exec_close(a, meta)
            elif a["type"] == "modify":
                self._exec_modify(a, pos_by_tk, meta)
            else:
                self._exec_trail(a, meta)
        self._notifier("alerte", "Session extraordinaire — position en degradation",
                       "; ".join(f"ticket {a['position']['ticket']}: "
                                 f"{', '.join(a['declencheurs'])}" for a in alertes),
                       action_requise=f"{len(actions)} action(s) de reduction du risque "
                                      f"appliquee(s) hors cycle : "
                                      f"{[a['type'] for a in actions]}.")

    # -------------------------------------------------------- execution des actions
    def _execute_actions(self, actions: list, s: dict, meta: dict,
                         news: dict, can_open: bool, account: dict):
        if actions:
            self.mem.log_event("plan", {"actions": actions})
        closes = [a for a in actions if a.get("type") == "close"]
        modifies = [a for a in actions if a.get("type") == "modify"]
        trails = [a for a in actions if a.get("type") == "trail"]
        opens = [a for a in actions if a.get("type") == "open"]

        pos_by_tk = {p["ticket"]: p for p in self.broker.positions()}
        for a in closes:
            self._exec_close(a, meta)
        for a in modifies:
            self._exec_modify(a, pos_by_tk, meta)
        for a in trails:
            self._exec_trail(a, meta)

        if not opens:
            return
        for a in opens:
            if not can_open:
                log.info("Ouverture %s ignoree (garde-fou portefeuille actif).", a.get("symbol"))
                continue
            positions = self.broker.positions()
            acc = self._account_state(account, positions, s)
            self._exec_open(a, acc, s, meta, news)

    def _exec_close(self, a: dict, meta: dict):
        tk = a["ticket"]; frac = a.get("fraction", 1.0)
        res = self.broker.close_position(tk, frac, comment=str(a.get("reason", "AI-close"))[:24])
        log.info("CLOTURE ticket %s x%.2f -> %s", tk, frac, res.get("message"))
        self.mem.log_event("close", {"ticket": tk, "fraction": frac,
                                     "reason": a.get("reason"), "result": res})
        if res.get("ok") and not res.get("full"):        # partielle -> risque residuel reduit
            m = meta.get(str(tk))
            if m:
                m["risk_dollars"] = m.get("risk_dollars", 0.0) * (1 - frac)
                self.mem.save_meta(meta)

    def _exec_modify(self, a: dict, pos_by_tk: dict, meta: dict):
        tk = a["ticket"]; new_sl = a.get("sl"); new_tp = a.get("tp")
        p = pos_by_tk.get(tk)
        if p is None:
            log.info("Modify ignore : ticket %s introuvable.", tk)
            return
        # garde-fou : un nouveau SL ne doit pas AUGMENTER le risque au-dela du budget FTMO
        if new_sl:
            spec = self._spec(p["symbol"])
            stop_pips = abs(p["entry"] - new_sl) * spec.get("price_to_pips", 0.0)
            risk = stop_pips * spec.get("pip_value_per_lot", 0.0) * p["volume"]
            budget = self.cfg.ftmo.account_size * self.cfg.ftmo.risk_per_trade_pct / 100
            if risk > budget * 1.2:
                log.info("Modify %s refuse : SL trop large (risque %.0f$ > budget).", tk, risk)
                self.mem.log_event("modify_veto", {"ticket": tk, "sl": new_sl, "risk": risk})
                return
        res = self.broker.modify_position(tk, new_sl, new_tp)
        log.info("MODIFY ticket %s sl=%s tp=%s -> %s", tk, new_sl, new_tp, res.get("message"))
        self.mem.log_event("modify", {"ticket": tk, "sl": new_sl, "tp": new_tp,
                                      "reason": a.get("reason"), "result": res})
        if res.get("ok"):
            m = meta.get(str(tk))
            if m:
                if new_sl:
                    m["sl"] = new_sl
                if new_tp:
                    m["tp"] = new_tp
                self.mem.save_meta(meta)

    def _exec_trail(self, a: dict, meta: dict):
        """Arme / desarme le trailing stop d'une position (config persistee dans open_meta)."""
        tk = a["ticket"]
        m = meta.setdefault(str(tk), {"ticket": tk})
        if not a.get("enabled", True):
            m.pop("trail", None)
            log.info("TRAILING desarme sur ticket %s.", tk)
        else:
            m["trail"] = {"enabled": True, "atr_mult": a.get("atr_mult"),
                          "pips": a.get("pips"), "activate_r": a.get("activate_r", 1.0),
                          "timeframe": a.get("timeframe") or self.cfg.timeframe}
            log.info("TRAILING arme sur ticket %s (%s).", tk,
                     f"{a['pips']:.0f} pips" if a.get("pips") else f"{a.get('atr_mult')}xATR")
        self.mem.log_event("trail", {"ticket": tk, "config": m.get("trail"), "reason": a.get("reason")})
        self.mem.save_meta(meta)

    def _exec_open(self, a: dict, acc: AccountState, s: dict, meta: dict, news: dict):
        sym = a["symbol"]; strat = a.get("strategy", "unknown")
        # L'agent choisit LIBREMENT sa paire : on resout la spec a la demande.
        # Seul refus possible ici : symbole inconnu / non negociable chez le broker.
        spec = self._spec(sym)
        if not spec:
            log.warning("Ouverture %s ignoree : symbole inconnu ou non negociable.", sym)
            self.mem.log_event("symbol_unknown", {"symbol": sym, "strategy": strat})
            return
        if self.cfg.news.enabled and self.cfg.news.fail_closed and not self.news.calendar_ok:
            log.error("Ouverture %s refusee : calendrier economique illisible et "
                      "NEWS_FAIL_CLOSED=1 (aucune protection news).", sym)
            self.mem.log_event("news_fail_closed", {"symbol": sym})
            return
        bo = news.get("blackout", {}).get(sym) or self.news.blackout_for(sym)
        if bo.get("active"):
            log.info("BLACK-OUT news sur %s (%s dans %sh) — entree bloquee.",
                     sym, bo.get("reason"), bo.get("in_hours"))
            self.mem.log_event("news_blackout", {"symbol": sym, **bo})
            return
        gap = self._weekend_guard()
        if gap:
            log.info("Ouverture %s bloquee : %s", sym, gap)
            self.mem.log_event("weekend_guard", {"symbol": sym, "raison": gap})
            return
        prop = TradeProposal(symbol=sym, direction=a["direction"], entry=a["entry"],
                             sl=a["sl"], tp=a["tp"], confidence=a.get("confidence", 0.6),
                             rationale=a.get("rationale", ""))
        # L'ordre part au marche : on dimensionne sur le prix REEL, pas sur le niveau annonce.
        entry_ia = prop.entry
        prop, refus = self._entry_guard(sym, spec, prop)
        if refus:
            log.info("Ouverture %s refusee : %s", sym, refus)
            self.mem.log_event("entry_drift", {"symbol": sym, "strategy": strat,
                                               "entry_ia": entry_ia, "raison": refus})
            return
        # Plafond de risque impose par le Risk Manager LLM du desk (durcit seulement).
        risk_override = float(a.get("risk_pct") or 0.0)
        if risk_override:
            log.info("Risk Manager plafonne %s a %.2f%% du solde.", sym, risk_override)
        rd = self.risk.validate(prop, acc,
                                pip_value_per_lot=spec["pip_value_per_lot"],
                                price_to_pips=spec["price_to_pips"], min_lot=spec["min_lot"],
                                max_lot=spec["max_lot"], lot_step=spec["lot_step"],
                                costs=self._costs(sym, spec, prop.direction),
                                risk_pct_override=risk_override)
        if not rd.approved:
            log.info("VETO risque %s: %s", sym, "; ".join(rd.reasons))
            self.mem.log_event("risk_veto", {"proposal": vars(prop), "reasons": rd.reasons})
            return
        log.info("OUVERTURE [%s]: %s %s lot %.2f (%s)", strat, prop.direction, sym,
                 rd.lot, rd.reasons[0])
        res = self.broker.place_order(sym, prop.direction, rd.lot, prop.sl, prop.tp,
                                      comment=f"{strat} conf={prop.confidence:.2f}")
        self.mem.log_event("order_sent", {"proposal": vars(prop), "strategy": strat,
                                          "lot": rd.lot, "risk_pct": rd.risk_pct,
                                          "rr": rd.rr, "rr_net": rd.rr_net,
                                          "couts_estimes": rd.costs_dollars,
                                          "stop_pips": rd.stop_pips,
                                          "stop_effectif_pips": rd.effective_stop_pips,
                                          "result": res})
        if res["ok"]:
            s["trades_today"] += 1
            today = _today_key(self.server_now())         # min 4 jours de trading (heure serveur)
            if today not in s.setdefault("trading_days", []):
                s["trading_days"].append(today)
            meta[str(res["ticket"])] = {
                "ticket": res["ticket"], "symbol": sym, "strategy": strat,
                "direction": prop.direction, "entry": res["price"], "sl": prop.sl,
                "tp": prop.tp, "risk_dollars": rd.risk_dollars, "rr": rd.rr,
                # --- traces pour le POST-MORTEM (apprentissage) ---
                "rr_net": rd.rr_net, "lot": rd.lot, "regime": self._last_regime,
                "confidence": prop.confidence, "rationale": prop.rationale[:400],
                "entry_planifiee": entry_ia,          # le niveau ANNONCE par le cerveau
                "entry_sizing": prop.entry,           # le niveau reel ayant servi au sizing
                "derive_entree_pips": round(abs(entry_ia - prop.entry)
                                            * spec.get("price_to_pips", 0.0), 1),
                "slippage_pips": res.get("slippage_pips", 0.0),
                "spread_pips_entree": res.get("spread_pips", 0.0),
                "couts_estimes": rd.costs_dollars, "ouvert_le": datetime.now(timezone.utc).isoformat(),
                # DOSSIER DE DECISION : qui a dit quoi (mandat, briefs, debat, verdict risque).
                # Sans lui, impossible d'attribuer un resultat a un rol (apprentissage Phase 4).
                "dossier": a.get("dossier") or {},
                "mfe_R": 0.0, "mae_R": 0.0}
            self.mem.save_meta(meta)
            self.mem.save_session(s)
            self._prev_tickets.add(res["ticket"])
            self._notifier("trade_ouvert", {**meta[str(res["ticket"])],
                                            "lot": rd.lot, "risk_pct": rd.risk_pct,
                                            "rr": rd.rr, "rr_net": rd.rr_net})
        log.info("Execution: %s", res["message"])

    # -------------------------------------------------------- un cycle complet
    def cycle(self):
        if not self.broker.connected:
            log.error("MT5 non connecte — aucun trade possible ce cycle.")
            return
        account = self.broker.account()
        if account["balance"] <= 0:
            log.error("Solde MT5 indisponible — cycle ignore.")
            return
        # nouveau cycle -> donnees fraiches (specs et bougies rechargees a la demande)
        self._spec_cache.clear()
        self._bars_cache.clear()
        self._server_now = None          # horloge serveur relue a chaque cycle
        self.web.reset_budget()          # quota de recherches web rendu a l'agent

        s = self._session(account)
        positions = self.broker.positions()          # les NOTRES (magic) : on n'agit que dessus
        meta = self.mem.load_meta()
        self._handle_closes(positions, meta, s)
        positions = self.broker.positions()
        etrangeres = [p for p in self.broker.positions(own_only=False) if not p.get("a_nous")]
        if etrangeres:
            log.info("%d position(s) d'un autre EA / manuelles : comptees dans le risque, "
                     "JAMAIS touchees.", len(etrangeres))

        # Pre-scan de la WATCHLIST (point de depart). L'agent reste libre d'aller
        # chercher n'importe quel autre symbole du broker via list_symbols/get_chart.
        watch = list(dict.fromkeys(list(self.cfg.symbols)
                                   + [p["symbol"] for p in positions]))
        snaps, charts = {}, {}
        for sym in watch:
            spec = self._spec(sym)
            if not spec:
                log.warning("Symbole %s indisponible chez le broker — ignore du scan.", sym)
                continue
            frames = {tf: self._bars(sym, tf) for tf in self.cfg.chart_timeframes}
            snaps[sym] = snapshot(sym, self._bars(sym, self.cfg.timeframe), spec)
            charts[sym] = chart.read(sym, frames, spec)

        # PROTECTIONS DETERMINISTES D'ABORD — elles ne dependent JAMAIS du LLM :
        # trailing, stop d'urgence, break-even, panique perte-jour, mise a plat week-end.
        self._apply_trailing(meta)
        positions = self.broker.positions()          # SL potentiellement resserres

        acc = self._account_state(account, positions + etrangeres, s)
        summary = self._account_summary(acc)
        summary.update(self._phase_info(s, summary["pnl_total_pct"]))
        if self._protect(positions, etrangeres, summary, meta, s, account):
            return                                   # urgence traitee : on ne sollicite pas l'IA
        positions = self.broker.positions()
        acc = self._account_state(account, positions + etrangeres, s)
        summary.update(self._account_summary(acc))
        self._last_summary = summary             # etat de reference pour les emails
        sb = Scoreboard(self.mem.closed_trades())
        news = self.news.snapshot(watch)
        pos_view = self._enrich_positions(positions, meta)
        log.info("Etape %s | PnL %.2f%%/objectif %.0f%% (reste %.2f%%) | perte jour %.2f%% | "
                 "J%s/%s | jours trades %s/%s | pos %d",
                 summary["etape"], summary["pnl_total_pct"], summary["objectif_etape_pct"],
                 summary["reste_avant_objectif_pct"], summary["perte_jour_pct"],
                 summary["jours_ecoules"], self.cfg.ftmo.phase_days,
                 summary["jours_trades"], summary["jours_trades_min"], acc.open_positions)

        # Le garde-fou FTMO bloque les OUVERTURES, mais l'agent doit toujours pouvoir
        # GERER (cloturer/securiser) ses positions ouvertes.
        gate = self.risk.portfolio_gate(acc)
        can_open = not gate
        # Le gate FTMO deterministe reste l'autorite : on l'expose au cerveau (le Gerant du
        # desk evite ainsi de convoquer analystes/debat quand les ouvertures sont bloquees).
        summary["ouvertures_bloquees"] = not can_open
        summary["gate_raisons"] = gate
        if gate:
            for g in gate:
                log.info("Ouvertures suspendues: %s", g)
            self.mem.log_event("gate_block", {"reasons": gate, **summary})
            if not positions:
                # book vide : si l'IA etait deja hors service, plus rien ne justifie de tourner
                if self.agent is not None and getattr(self.agent, "degraded", False):
                    self._exit_if_flat(0, "IA indisponible et plus aucune position ouverte")
                return                       # rien a gerer, rien a ouvrir -> on saute le cycle

        strat_block = self._strategies_block(snaps, sb)
        pm = PostMortem(self.mem.closed_trades())
        T.bind_context(snaps, charts, summary, pos_view,
                       strat_block, news, postmortem=pm.as_prompt_block())
        T.bind_live(self)          # acces libre : symboles, timeframes, indicateurs, news
        try:
            agent = self._lazy_agent()
            actions = agent.decide(summary)
            degraded, why = bool(agent.degraded), str(agent.last_error or "")
        except Exception as e:                       # meme la construction de l'agent a echoue
            log.warning("Decision agent impossible: %s", e)
            actions, degraded, why = [], True, str(e)

        if degraded:
            # --- L'IA EST HORS SERVICE -> pilote deterministe, aucune nouvelle entree.
            can_open = False
            actions = self._safe_actions(pos_view, summary, why)
        else:
            self._clear_safe_marker()

        # MESURE : on archive le dossier d'entree + le plan AVANT execution. C'est ce qui
        # rend le cycle rejouable (desk/replay.py) et comparable entre cerveaux.
        self._journal_cycle(actions, degraded)

        log.info("Actions du cycle%s: %s", " [SECOURS]" if degraded else "",
                 [f"{a.get('type')} {a.get('symbol', a.get('ticket',''))}" for a in actions] or "aucune")
        if self.cfg.eval.shadow and not degraded:
            # MODE OMBRE : le cerveau est OBSERVE, pas suivi. Son plan est journalise,
            # aucune de ses actions n'est executee. Les protections deterministes
            # (trailing, stop d'urgence, panique perte-jour) ont deja tourne ce cycle.
            log.warning("MODE OMBRE (EVAL_SHADOW=1) : %d action(s) journalisee(s), "
                        "AUCUNE executee.", len(actions))
            return
        self._execute_actions(actions, s, meta, news, can_open, account)

        if degraded:
            restantes = len(self.broker.positions())
            self._exit_if_flat(restantes, why)

    # -------------------------------------------------------- mesure / rejeu
    def _journal_cycle(self, actions: list, degraded: bool):
        """Archive le dossier d'entree du cycle + le plan rendu, en rotation.

        Sans cette trace, comparer deux cerveaux revient a comparer deux souvenirs :
        `desk/replay.py` rejoue ces dossiers hors-ligne et simule l'issue des trades."""
        e = self.cfg.eval
        store = getattr(self.mem, "store", None)
        if store is None or not (e.journal_cycles or e.shadow):
            return                          # memoire sans journal (tests, stubs) : on passe
        try:
            journal.record_cycle(store, T.cycle_context(), actions,
                                 mode=self.cfg.desk.mode, degraded=degraded, shadow=e.shadow,
                                 server_now=self.server_now().isoformat(), keep=e.journal_keep)
        except Exception as exc:            # mesurer ne doit JAMAIS empecher de trader
            log.warning("Journalisation du cycle impossible (ignore): %s", exc)

    # -------------------------------------------------------- mode secours (sans LLM)
    def _safe_actions(self, pos_view: list, summary: dict, why: str) -> list:
        """L'IA ne repond plus : un script dur protege les positions en cours."""
        log.error("IA INDISPONIBLE (%s) — PILOTE DE SECOURS DETERMINISTE : "
                  "aucune nouvelle entree, on protege les %d position(s) jusqu'a la sortie.",
                  why or "raison inconnue", len(pos_view))
        if not self._degrade_signale:            # une seule alerte par episode de panne
            self._degrade_signale = True
            self._last_summary = summary
            self._notifier("alerte", "IA indisponible — pilote de secours actif",
                           why or "raison inconnue",
                           action_requise="Aucune nouvelle position ne sera ouverte. Les "
                                          "positions en cours sont protegees par des regles "
                                          "deterministes ; le script s'arretera une fois le "
                                          "book vide. Verifier Bedrock (modele, cle, quota).")
        atr_by_sym = {}
        for p in pos_view:
            df = self._bars(p["symbol"], self.cfg.timeframe)
            if df is not None and not df.empty:
                try:
                    atr_by_sym[p["symbol"]] = _atr(df, 14)
                except Exception:
                    pass
        actions = self.pilot.actions(pos_view, summary, atr_by_sym, spec_of=self._spec)
        self.mem.log_event("safe_mode", {"raison": why, "positions": len(pos_view),
                                         "actions": actions, **summary})
        self._write_safe_marker(why, pos_view, summary)
        return actions

    def _write_safe_marker(self, why: str, pos_view: list, summary: dict):
        """Trace persistante pour l'inspection manuelle (table kv, cle `safe_mode`).
        En base : elle survit a un arret brutal et au redemarrage du script."""
        prev = self.mem.load_safe_mode()
        self.mem.save_safe_mode({
            "actif": True,
            "depuis": prev.get("depuis") or datetime.now(timezone.utc).isoformat(),
            "dernier_cycle": datetime.now(timezone.utc).isoformat(),
            "cycles": int(prev.get("cycles", 0)) + 1,
            "raison": why,
            "positions_restantes": [{"ticket": p["ticket"], "symbol": p["symbol"],
                                     "direction": p["direction"], "floating_R": p.get("floating_R")}
                                    for p in pos_view],
            "compte": {k: summary.get(k) for k in ("equity", "pnl_total_pct", "perte_jour_pct")},
        })

    def _clear_safe_marker(self):
        """L'IA repond de nouveau -> on efface la trace de mode degrade."""
        if self.mem.load_safe_mode():
            log.info("IA de nouveau disponible — sortie du pilote de secours.")
            self.mem.clear_safe_mode()
            self._notifier("alerte", "IA de nouveau disponible",
                           "Le modele repond : reprise du pilotage normal.",
                           action_requise="")
        self._degrade_signale = False

    def _exit_if_flat(self, restantes: int, why: str):
        """Book vide en mode secours -> arret du script pour inspection manuelle."""
        if restantes > 0:
            log.warning("PILOTE DE SECOURS actif : %d position(s) encore ouverte(s), "
                        "on continue de les gerer.", restantes)
            return
        if not self.cfg.safe.exit_when_flat:
            log.warning("PILOTE DE SECOURS : book vide (SAFE_EXIT_WHEN_FLAT=0, on continue).")
            return
        self.mem.log_event("safe_exit", {"raison": why})
        self.mem.clear_safe_mode()
        self._notifier("alerte", "ARRET DU SCRIPT — plus aucune position ouverte",
                       f"L'IA est indisponible ({why or 'raison inconnue'}) et le pilote de "
                       f"secours a fini de gerer les positions : elles sont toutes fermees.",
                       action_requise="Le script est ARRETE. Inspecter avec "
                                      "`python run.py --status`, corriger l'acces au modele, "
                                      "puis relancer `python run.py --loop`.")
        self.mail.flush(15)                      # on part : on laisse l'email sortir
        raise SafeExit(why or "IA indisponible")

    # -------------------------------------------------------- inspection manuelle
    def print_status(self):
        """`python run.py --status` : tout l'etat persistant, lisible, sans SQL."""
        st = self.mem.store
        print(f"\n=== ETAT PERSISTANT — {st.path} ===\n")

        session = self.mem.load_session()
        if session:
            print("SESSION FTMO")
            for k in ("phase", "phase_start", "initial_balance", "day",
                      "day_start_balance", "trades_today", "last_loss_iso"):
                if k in session:
                    print(f"  {k:20s} {session[k]}")
            print(f"  {'jours_trades':20s} {len(set(session.get('trading_days', [])))}")
        else:
            print("SESSION FTMO : (aucune — jamais demarre)")

        safe = self.mem.load_safe_mode()
        print("\nMODE SECOURS : " + ("INACTIF" if not safe else
              f"ACTIF depuis {safe.get('depuis')} ({safe.get('cycles')} cycles) — "
              f"{safe.get('raison')}"))

        meta = self.mem.load_meta()
        print(f"\nPOSITIONS SUIVIES : {len(meta)}")
        for tk, m in meta.items():
            print(f"  #{tk} {m.get('symbol')} {m.get('direction')} [{m.get('strategy')}] "
                  f"risque {m.get('risk_dollars', 0):.0f}$ | MFE {m.get('mfe_R')} / "
                  f"MAE {m.get('mae_R')} | trailing {'oui' if m.get('trail') else 'non'}")

        events = st.events(limit=12)
        print(f"\nDERNIERS EVENEMENTS ({len(events)}) :")
        for e in events:
            detail = e.get("symbol") or e.get("ticket") or e.get("raison") or ""
            print(f"  {e['ts'][:19]}  {e['kind']:16s} {str(detail)[:60]}")

        print("\nBILAN / POST-MORTEM :")
        print(PostMortem(self.mem.closed_trades()).as_prompt_block())
        print()
        st.close()

    def _sleep_with_watchdog(self, seconds: int) -> bool:
        """Attend le prochain cycle LLM en executant le watchdog toutes les
        AGENT_WATCH_SECONDS. Renvoie False si le script doit s'arreter."""
        pas = max(5, int(self.cfg.execution.watch_seconds or seconds))
        restant = int(seconds)
        while restant > 0:
            dodo = min(pas, restant)
            # `wait` plutot que `sleep` : un SIGTERM pendant l'heure d'attente doit
            # rendre la main tout de suite, pas au bout d'une minute de watchdog.
            if process.ARRET.wait(dodo):
                return False
            restant -= dodo
            try:
                self.watchdog()
            except SafeExit:
                raise
            except Exception as e:
                log.warning("Watchdog: %s", e)
        return True

    def run(self, loop: bool) -> int:
        # INSTANCE UNIQUE d'abord : deux agents sur le meme compte doublent le risque
        # reellement pris et faussent le calcul de la perte journaliere FTMO.
        verrou = process.VerrouInstance(STATE_DIR / "agent.lock")
        if not verrou.acquerir():
            return 3
        # Le signal ne tue plus : il DEMANDE l'arret. La boucle finit ce qu'elle fait,
        # les emails partent, la base est fermee proprement.
        process.install_signal_handlers()
        ok = self.broker.connect()
        if not ok:
            log.error("Connexion MT5 ECHOUEE — verifier terminal/credentials. "
                      "L'agent ne pourra pas trader.")
        log.info("=== Agent trader FTMO SWING multi-strategies — EXECUTION DIRECTE | "
                 "Bedrock %s | TF %s ===", self.cfg.bedrock.model_id, self.cfg.timeframe)
        log.info("Etat persistant SQLite: %s", self.mem.store.path)
        self.mem.store.purge_events(self.cfg.eval.event_retention_days)
        reprise = self.mem.load_safe_mode()
        if reprise:
            log.warning("Reprise apres arret en MODE SECOURS (%s, depuis %s) — "
                        "l'etat des positions suivies a ete restaure depuis la base.",
                        reprise.get("raison"), reprise.get("depuis"))
        code = 0
        try:
            while True:
                if process.arret_demande():
                    log.warning("Arret demande — on ne demarre pas de nouveau cycle.")
                    break
                try:
                    self.cycle()
                except SafeExit as e:
                    log.warning("=" * 78)
                    log.warning("ARRET DU SCRIPT — IA indisponible (%s) et PLUS AUCUNE "
                                "position ouverte.", e)
                    log.warning("Les positions ont ete gerees jusqu'a leur fermeture par le "
                                "pilote de secours. Inspecte avec `python run.py --status` "
                                "puis relance.")
                    log.warning("=" * 78)
                    break
                except Exception as e:
                    log.exception("Erreur de cycle: %s", e)
                if not loop:
                    break
                # Entre deux decisions du LLM, le WATCHDOG protege en continu :
                # perte-jour, stop d'urgence, break-even, trailing. Sans IA, sans delai.
                if not self._sleep_with_watchdog(self.cfg.loop_seconds):
                    break
        except SafeExit as e:                    # levee depuis le watchdog
            log.warning("ARRET DU SCRIPT (watchdog) — %s", e)
        except KeyboardInterrupt:
            log.warning("Interruption clavier — arret immediat demande.")
        finally:
            # ORDRE IMPORTANT. Le broker d'abord (on lache la connexion), puis les emails
            # (une alerte qui n'arrive pas ne sert a rien : c'est ici que se jouait la
            # perte des alertes, les threads daemon mourant avec le process), puis la
            # base (checkpoint WAL), et enfin le verrou — libere en dernier pour qu'aucune
            # autre instance ne demarre pendant qu'on referme.
            self.broker.shutdown()
            if not self.mail.flush(15):
                log.warning("Des notifications n'ont pas pu partir avant l'arret.")
            self.mem.store.close()          # checkpoint WAL : rien ne se perd a l'arret
            verrou.liberer()
            log.info("Arret propre termine (envoyes=%d, echoues=%d, jetes=%d).",
                     self.mail.envoyes, self.mail.echoues, self.mail.jetes)
        return code


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="boucle continue")
    ap.add_argument("--status", action="store_true",
                    help="inspecte l'etat persistant (SQLite) et quitte")
    ap.add_argument("--test-mail", action="store_true",
                    help="envoie un email de test et quitte (verifie la config SMTP)")
    args = ap.parse_args()
    if args.status:
        Orchestrator().print_status()
        sys.exit(0)
    if args.test_mail:
        o = Orchestrator()
        c = o.cfg.mail
        if not c.ready:
            log.error("Config mail incomplete : MAIL_HOST=%s MAIL_TO=%s (voir .env)",
                      c.host or "(vide)", ", ".join(c.to) or "(vide)")
            sys.exit(1)
        log.info("Envoi d'un email de test vers %s via %s:%s (%s)...",
                 ", ".join(c.to), c.host, c.port, c.mode)
        # on passe par la file reelle (reessai compris) : c'est le chemin de production
        # qu'on veut verifier, pas un envoi direct qui court-circuiterait tout.
        o.mail.send("Email de test",
                    "Si vous lisez ceci, les notifications fonctionnent.\n\n"
                    + Mailer.portefeuille({}, []))
        o.mail.flush(60)
        log.info("Resultat : %d envoye(s), %d echoue(s).", o.mail.envoyes, o.mail.echoues)
        sys.exit(0 if o.mail.envoyes else 1)
    sys.exit(Orchestrator().run(loop=args.loop))
