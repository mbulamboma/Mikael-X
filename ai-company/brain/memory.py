# -*- coding: utf-8 -*-
"""Memoire persistante de l'agent : journal des trades + etats.

Ce module ne contient AUCUN apprentissage : l'agent ne se raconte pas d'histoires sur
ses erreurs passees. Ce qui remonte dans les prompts vient de FAITS calcules —
`brain/postmortem.py` (bilan chiffre), `strategy/scoreboard.py` (edge par strategie),
`desk/bilan_roles.py` (revue par employe), `desk/situation.py` (cas passes comparables).
Une "lecon" ecrite par un LLM apres une cloture n'est ni verifiable ni mesurable : elle a
ete retiree.

STOCKAGE : SQLite (`state/agent.db`, cf. store.py), pas des fichiers JSON. Un arret
brutal (VPS redemarre, process tue, coupure) ne peut plus corrompre l'etat : chaque
ecriture est transactionnelle et le mode WAL est resistant aux coupures. Au
redemarrage, l'agent retrouve exactement ses positions suivies et sa session FTMO.
Les anciens fichiers JSON sont importes automatiquement une fois.
"""
from __future__ import annotations

from typing import Optional

from store import Store, default_store


class Memory:
    def __init__(self, store: Optional[Store] = None):
        self.store = store or default_store()

    # ------------------------------------------------------------- journal
    def log_event(self, kind: str, payload: dict):
        self.store.log_event(kind, payload)

    def closed_trades(self) -> list[dict]:
        return self.store.events(kind="trade_closed")

    def stats(self) -> dict:
        cl = self.closed_trades()
        if not cl:
            return {"n": 0, "winrate": 0.0, "pnl": 0.0, "avg_R": 0.0}
        wins = [t for t in cl if t.get("pnl", 0) > 0]
        pnl = sum(t.get("pnl", 0) for t in cl)
        rs = [t.get("R", 0.0) for t in cl if "R" in t]
        return {
            "n": len(cl),
            "winrate": round(len(wins) / len(cl) * 100, 1),
            "pnl": round(pnl, 2),
            "avg_R": round(sum(rs) / len(rs), 2) if rs else 0.0,
        }

    # ------------------------------------------------------------- session (perte jour)
    def load_session(self) -> dict:
        data = self.store.kv_get("session", {})
        return data if isinstance(data, dict) else {}

    def save_session(self, data: dict):
        self.store.kv_set("session", data)

    # ------------------------------------------------------------- meta positions ouvertes
    def load_meta(self) -> dict:
        return self.store.meta_all()

    def save_meta(self, data: dict):
        self.store.meta_replace(data)

    # ------------------------------------------------------------- mode secours
    def load_safe_mode(self) -> dict:
        data = self.store.kv_get("safe_mode", {})
        return data if isinstance(data, dict) else {}

    def save_safe_mode(self, data: dict):
        self.store.kv_set("safe_mode", data)

    def clear_safe_mode(self):
        self.store.kv_delete("safe_mode")

    def strategy_stats_text(self) -> str:
        """Resume par strategie (lisible), pour logs / debug."""
        by: dict[str, list[float]] = {}
        for t in self.closed_trades():
            by.setdefault(t.get("strategy", "?"), []).append(t.get("R", 0.0))
        if not by:
            return "(aucune strategie encore evaluee)"
        rows = []
        for s, rs in by.items():
            exp = sum(rs) / len(rs) if rs else 0.0
            rows.append(f"{s}: {len(rs)} trades, expectancy {exp:.2f}R")
        return " | ".join(rows)
