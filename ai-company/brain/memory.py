# -*- coding: utf-8 -*-
"""Memoire persistante de l'agent : journal des trades + lecons apprises.

C'est le mecanisme d'AMELIORATION CONTINUE :
  1. Chaque decision (prise ou refusee) est journalisee.
  2. A la cloture d'un trade, un "reflecteur" LLM ecrit une LECON courte, fondee sur
     le bilan chiffre (brain/postmortem.py).
  3. Les lecons les plus pertinentes sont reinjectees dans le prompt du cycle suivant
     -> l'agent ne refait pas deux fois la meme erreur.

STOCKAGE : SQLite (`state/agent.db`, cf. store.py), pas des fichiers JSON. Un arret
brutal (VPS redemarre, process tue, coupure) ne peut plus corrompre l'etat : chaque
ecriture est transactionnelle et le mode WAL est resistant aux coupures. Au
redemarrage, l'agent retrouve exactement ses positions suivies, sa session FTMO et
ses lecons. Les anciens fichiers JSON sont importes automatiquement une fois.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from store import Store, default_store


@dataclass
class Lesson:
    ts: str
    symbol: str
    outcome: str          # "win" | "loss" | "breakeven" | "vetoed"
    text: str             # la lecon, 1-2 phrases actionnables
    tags: list[str] = field(default_factory=list)


class Memory:
    def __init__(self, store: Optional[Store] = None):
        self.store = store or default_store()

    # ------------------------------------------------------------- lecons
    @property
    def lessons(self) -> list[Lesson]:
        return [Lesson(**l) for l in self.store.lessons()]

    def add_lesson(self, symbol: str, outcome: str, text: str, tags: Optional[list[str]] = None):
        if not str(text).strip():
            return
        self.store.add_lesson(symbol, outcome, str(text), [t for t in (tags or []) if t])

    def recent_lessons_text(self, k: int = 12) -> str:
        """Bloc a injecter dans le prompt systeme (les plus recentes, dedupliquees)."""
        return self.relevant_lessons_text(k=k)

    def relevant_lessons_text(self, symbols: Optional[list[str]] = None,
                              strategies: Optional[list[str]] = None, k: int = 12,
                              roles: Optional[list[str]] = None) -> str:
        """Lecons PERTINENTES d'abord : celles qui concernent les symboles/strategies du
        moment, puis les plus recentes. Doublons ecartes (le LLM se repete beaucoup).

        `roles` (desk multi-agents) : si fourni, on ne garde QUE les lecons taguees d'un de
        ces roles (ex ["trader"], ["suivi"]) — chaque employe apprend de SES propres erreurs.
        Une lecon sans tag de rol reste visible de tous (heritage de l'agent solo)."""
        lessons = self.lessons
        if not lessons:
            return "(aucune lecon pour l'instant — premier demarrage)"
        syms = {s.upper() for s in (symbols or [])}
        strats = {s.lower() for s in (strategies or [])}
        wanted_roles = {r.lower() for r in (roles or [])}
        role_tags = {"gerant", "trader", "risk", "analyste", "debat", "suivi", "vigie",
                     "technique", "fondamental", "sentiment", "actualite", "bull", "bear",
                     "juge", "agressif", "neutre", "prudent"}

        if wanted_roles:
            def keep(l) -> bool:
                tags = {(t or "").lower() for t in l.tags}
                has_role = bool(tags & role_tags)
                return (not has_role) or bool(tags & wanted_roles)
            lessons = [l for l in lessons if keep(l)]
            if not lessons:
                return "(aucune lecon specifique a ce rol pour l'instant)"

        def score(idx_lesson) -> tuple:
            i, l = idx_lesson
            cible = (l.symbol or "").upper() in syms
            meth = any((t or "").lower() in strats for t in l.tags)
            perte = l.outcome in ("loss", "vetoed")        # on apprend surtout des erreurs
            return (cible + meth + perte, i)               # tri stable : pertinence puis recence

        ranked = sorted(enumerate(lessons), key=score, reverse=True)
        seen, rows = set(), []
        for _, l in ranked:
            cle = l.text.strip().lower()[:90]
            if cle in seen:
                continue
            seen.add(cle)
            rows.append(f"- [{l.outcome}/{l.symbol}] {l.text}")
            if len(rows) >= k:
                break
        return "\n".join(rows)

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
