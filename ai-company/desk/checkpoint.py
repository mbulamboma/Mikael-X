# -*- coding: utf-8 -*-
"""CHECKPOINT DE CYCLE — reprendre un cycle interrompu au lieu de tout refaire.

Un cycle du desk coute plusieurs appels LLM (Gerant, 4 analystes/candidat, debat, Trader,
risque). Si le process meurt APRES le debat mais AVANT l'execution (VPS redemarre, OOM,
coupure), le relancer refait — et repaie — tout depuis le mandat. Ce module persiste les
artefacts intermediaires pour qu'un redemarrage immediat reprenne la ou il en etait.

DEUX GARDE-FOUS, parce qu'en trading un vieux raisonnement est DANGEREUX (le marche a bouge) :

  1. CLE DETERMINISTE. Le checkpoint est indexe par une SIGNATURE des entrees du cycle
     (jour serveur, candidats, tranche d'equity), pas par un identifiant aleatoire : seul un
     cycle *identique* rejoue retrouve ses artefacts. Un cycle sur d'autres candidats ou apres
     un mouvement de compte a une autre cle et repart de zero.
  2. TTL COURT. Un artefact plus vieux que `ttl_s` (defaut 180 s) est ignore : on ne reprend
     qu'un crash *immediatement* suivi d'un redemarrage, jamais une decision rassie.

Stockage : le kv SQLite du store (transactionnel, resistant a l'arret brutal). On ne garde
qu'UN cycle a la fois (le dernier) : le checkpoint est un filet anti-crash, pas un historique.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from store import Store, default_store

log = logging.getLogger("desk.checkpoint")

_KV_KEY = "desk_checkpoint"


def signature(jour: str, candidats: list[str], equity: float,
              bucket: float = 100.0) -> str:
    """Signature deterministe et STABLE d'un cycle. `equity` est arrondie a `bucket` pres
    pour qu'un flottant qui bouge d'un dollar ne casse pas la reprise, tout en distinguant
    deux etats de compte reellement differents."""
    cands = ",".join(sorted(str(c).upper() for c in (candidats or [])))
    eq = round(float(equity or 0.0) / bucket) * bucket
    brut = f"{jour}|{cands}|{eq:.0f}"
    return hashlib.sha1(brut.encode("utf-8")).hexdigest()[:16]


class Checkpoint:
    """Filet anti-crash d'UN cycle. `enabled=False` -> tout devient no-op (get rend None)."""

    def __init__(self, sig: str, enabled: bool = True, ttl_s: int = 180,
                 store: Optional[Store] = None):
        self.sig = sig
        self.enabled = bool(enabled)
        self.ttl_s = int(ttl_s)
        self.store = store or default_store()

    def _load(self) -> dict:
        data = self.store.kv_get(_KV_KEY, {})
        return data if isinstance(data, dict) else {}

    def get(self, phase: str) -> Optional[Any]:
        """Artefact d'une phase (`mandate`, `briefs`, `debate`...) si un checkpoint FRAIS
        du MEME cycle existe. None sinon (y compris checkpoint desactive ou perime)."""
        if not self.enabled:
            return None
        data = self._load()
        if data.get("sig") != self.sig:
            return None
        if self._perime(data.get("ts")):
            return None
        phases = data.get("phases")
        if not isinstance(phases, dict) or phase not in phases:
            return None
        log.info("Checkpoint: phase '%s' reprise du cycle %s (pas de recalcul LLM).",
                 phase, self.sig)
        return phases[phase]

    def set(self, phase: str, artefact: Any) -> None:
        """Enregistre l'artefact d'une phase. Repart d'un checkpoint vide si la signature
        a change (nouveau cycle) : on ne conserve jamais deux cycles a la fois."""
        if not self.enabled:
            return
        data = self._load()
        if data.get("sig") != self.sig:
            data = {"sig": self.sig, "phases": {}}
        data["ts"] = datetime.now(timezone.utc).isoformat()
        phases = data.setdefault("phases", {})
        # on ne persiste que du serialisable : un artefact non-JSON ne doit pas casser le cycle
        try:
            json.dumps(artefact, default=str)
        except (TypeError, ValueError):
            log.debug("Checkpoint: phase '%s' non serialisable, ignoree.", phase)
            return
        phases[phase] = artefact
        self.store.kv_set(_KV_KEY, data)

    def clear(self) -> None:
        """A appeler quand le cycle a produit ses actions : le filet n'a plus lieu d'etre."""
        if not self.enabled:
            return
        try:
            self.store.kv_delete(_KV_KEY)
        except Exception:                        # pragma: no cover - nettoyage best-effort
            pass

    def _perime(self, ts: Optional[str]) -> bool:
        if not ts:
            return True
        try:
            t0 = datetime.fromisoformat(str(ts))
        except (TypeError, ValueError):
            return True
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - t0).total_seconds()
        return age > self.ttl_s
