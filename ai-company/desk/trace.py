# -*- coding: utf-8 -*-
"""TRACING PAS-A-PAS DU DESK — l'observabilite qu'un graphe LangGraph donnerait, sans
le framework.

Le desk enchaine ~10 rols LLM par cycle (Gerant -> analystes -> debat -> Trader -> risque).
Quand une decision surprend, la question est « QUI a dit quoi, avec quel modele, en combien
de temps, et est-ce que ca a marche ? ». Sans trace, on relit des logs epars ; avec, on a un
JSONL par cycle qu'on rejoue et qu'on agrege.

CE QUE C'EST :
  - un enregistreur branche au SEUL point de passage de tous les appels LLM du desk
    (`desk.base.DeskAgent._invoke`), donc AUCUN rol n'a a s'instrumenter lui-meme ;
  - une trace par cycle : `state/traces/<AAAAMMJJ>-<cycle>.jsonl`, une ligne par etape ;
  - rate-safe : si le tracing est coupe ou si l'ecriture echoue, l'appel LLM n'en souffre
    JAMAIS (le trading prime sur son propre journal).

CE QUE CE N'EST PAS : un profileur de tokens exact (on mesure la TAILLE des messages, pas le
comptage tokenizer du fournisseur — suffisant pour reperer un prompt qui gonfle), ni un
service externe (aucun LangSmith, aucune sortie reseau : tout reste sur le disque de l'agent).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import STATE_DIR

log = logging.getLogger("desk.trace")

TRACES_DIR = STATE_DIR / "traces"

#: cycle courant (id lisible). ContextVar pour que des appels imbriques d'un meme cycle
#: s'attribuent au bon fichier meme si un jour le desk tourne des rols en parallele.
_CYCLE: ContextVar[str] = ContextVar("desk_cycle", default="")
_SEQ = {"n": 0}
_LOCK = threading.Lock()
_ENABLED = {"on": True}


def configure(enabled: bool) -> None:
    """Branche/coupe le tracing (lu une fois depuis DeskConfig.trace_enabled)."""
    _ENABLED["on"] = bool(enabled)


def new_cycle_id(now: Optional[datetime] = None) -> str:
    """Identifiant de cycle : horodatage a la seconde + compteur, lisible et triable."""
    now = now or datetime.now(timezone.utc)
    with _LOCK:
        _SEQ["n"] += 1
        n = _SEQ["n"]
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{n:04d}"


def start_cycle(cycle_id: str) -> object:
    """Fixe le cycle courant. Rend un jeton a repasser a `end_cycle` (semantique ContextVar)."""
    return _CYCLE.set(cycle_id or "")


def end_cycle(token: object) -> None:
    try:
        _CYCLE.reset(token)  # type: ignore[arg-type]
    except (ValueError, LookupError):
        _CYCLE.set("")


def current_cycle() -> str:
    return _CYCLE.get()


def _path(cycle_id: str) -> Path:
    date = cycle_id.split("-", 1)[0] if "-" in cycle_id else \
        datetime.now(timezone.utc).strftime("%Y%m%d")
    return TRACES_DIR / f"{date}-{cycle_id}.jsonl"


def record(role: str, title: str, model: str, system: str, human: str,
           latency_ms: float, output: str = "", error: str = "") -> None:
    """Journalise UNE etape LLM. Ne leve jamais : une trace ratee ne casse pas un cycle."""
    if not _ENABLED["on"]:
        return
    cycle_id = _CYCLE.get() or "adhoc"
    try:
        with _LOCK:
            _SEQ["n"] += 1
            seq = _SEQ["n"]
        ligne = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "cycle": cycle_id,
            "seq": seq,
            "role": role,
            "title": title,
            "model": model,
            # on mesure la TAILLE (proxy de cout), pas le contenu : une trace ne doit pas
            # devenir un second stockage des prompts (volume + fuite d'infos potentielle).
            "system_len": len(system or ""),
            "human_len": len(human or ""),
            "output_len": len(output or ""),
            "latency_ms": round(float(latency_ms), 1),
            "ok": not error,
        }
        if error:
            ligne["error"] = str(error)[:300]
        TRACES_DIR.mkdir(parents=True, exist_ok=True)
        with _path(cycle_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(ligne, ensure_ascii=False) + "\n")
    except Exception as e:                       # pragma: no cover - le journal ne bloque rien
        log.debug("trace non ecrite (%s)", e)


def read_cycle(cycle_id: str) -> list[dict]:
    """Relit les etapes d'un cycle (debug/tests). [] si le fichier n'existe pas."""
    p = _path(cycle_id)
    if not p.exists():
        return []
    out = []
    for ligne in p.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne:
            continue
        try:
            out.append(json.loads(ligne))
        except json.JSONDecodeError:
            continue
    return out


def summary(cycle_id: str) -> dict:
    """Agrege un cycle : nb d'etapes, latence totale, echecs, cout relatif par rol."""
    etapes = read_cycle(cycle_id)
    if not etapes:
        return {"cycle": cycle_id, "etapes": 0}
    par_role: dict[str, dict] = {}
    for e in etapes:
        r = par_role.setdefault(e.get("role", "?"),
                                {"appels": 0, "latence_ms": 0.0, "echecs": 0, "chars_in": 0})
        r["appels"] += 1
        r["latence_ms"] += e.get("latency_ms", 0.0)
        r["chars_in"] += e.get("system_len", 0) + e.get("human_len", 0)
        if not e.get("ok", True):
            r["echecs"] += 1
    return {
        "cycle": cycle_id,
        "etapes": len(etapes),
        "latence_ms": round(sum(e.get("latency_ms", 0.0) for e in etapes), 1),
        "echecs": sum(0 if e.get("ok", True) else 1 for e in etapes),
        "par_role": par_role,
    }
