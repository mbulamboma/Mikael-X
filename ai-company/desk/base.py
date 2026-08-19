# -*- coding: utf-8 -*-
"""Socle commun des agents du desk : client Bedrock partage + I/O JSON robuste.

Chaque employe du desk (Gerant, analystes, Trader, Risk Manager, Trade Manager, Vigie)
herite de `DeskAgent`. Ils partagent :
  - un CLIENT Bedrock mis en cache par (modele, temperature, max_tokens) — modele unique
    par defaut, override par rol possible (cf. config.DeskConfig.model_for) ;
  - `ask_json()` / `ask_text()` : un appel LLM avec messages BRUTS (pas de ChatPromptTemplate,
    car nos dossiers contiennent du JSON dont les accolades casseraient le template) ;
  - une gestion homogene de l'INDISPONIBILITE : toute panne LLM leve `DeskUnavailable`,
    que l'orchestration du desk transforme soit en `degraded` (repli pilote deterministe),
    soit en "on saute cette brique" selon la criticite (cf. desk/desk.py).

Aucune de ces classes ne touche au broker ni au moteur de risque : elles RAISONNENT et
rendent du JSON structure. L'execution reste la chasse gardee de l'orchestrateur + FTMO.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

try:
    from langchain_aws import ChatBedrockConverse
    _LC_OK = True
    _LC_ERR: Exception | None = None
except Exception as exc:  # pragma: no cover - dependance optionnelle
    ChatBedrockConverse = None
    _LC_OK = False
    _LC_ERR = exc

from config import AgentConfig

log = logging.getLogger("desk")


class DeskUnavailable(RuntimeError):
    """Un agent du desk n'a pas pu repondre (Bedrock injoignable, credentials, quota,
    dependance manquante). Remontee jusqu'a l'orchestration qui decide du repli."""


def extract_json(texte: str) -> dict:
    """Recupere le PREMIER objet JSON complet d'une reponse LLM (tolere le bavardage,
    les ```json et le raisonnement prefixe de DeepSeek-R1). {} si rien d'exploitable.

    Copie volontaire de la logique de brain.agent._extract_json pour que le desk soit
    autonome (pas de dependance croisee avec l'agent solo)."""
    if not texte:
        return {}
    t = texte.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    debut = t.find("{")
    if debut < 0:
        return {}
    profondeur, fin = 0, -1
    for i, ch in enumerate(t[debut:], start=debut):
        if ch == "{":
            profondeur += 1
        elif ch == "}":
            profondeur -= 1
            if profondeur == 0:
                fin = i + 1
                break
    if fin < 0:
        return {}
    try:
        data = json.loads(t[debut:fin])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# Clients Bedrock partages : on ne recree pas un client par agent ni par cycle.
_CLIENTS: dict[tuple, Any] = {}


def _client(cfg: AgentConfig, model_id: str, temperature: float, max_tokens: int):
    """Client ChatBedrockConverse mis en cache. Leve DeskUnavailable si Bedrock/LangChain
    est indisponible — c'est le signal de repli vers le pilote deterministe."""
    if not _LC_OK:
        raise DeskUnavailable(f"LangChain/Bedrock indisponible: {_LC_ERR}")
    key = (model_id, round(float(temperature), 3), int(max_tokens))
    client = _CLIENTS.get(key)
    if client is None:
        b = cfg.bedrock
        kwargs = dict(model_id=model_id, region_name=b.region,
                      temperature=temperature, max_tokens=max_tokens)
        if b.aws_profile:
            kwargs["credentials_profile_name"] = b.aws_profile
        try:
            client = ChatBedrockConverse(**kwargs)
        except Exception as exc:  # pragma: no cover - dependance runtime
            raise DeskUnavailable(f"initialisation Bedrock impossible: {exc}") from exc
        _CLIENTS[key] = client
    return client


def reset_clients():
    """Vide le cache de clients (utile aux tests qui injectent des doublures)."""
    _CLIENTS.clear()


class DeskAgent:
    """Agent-employe generique. Sous-classe : definir `role`, `title`, un `system(...)`
    et une methode metier qui appelle `self.ask_json(...)`."""

    role = "desk"          # identite de l'employe + cle de config modele (par defaut)
    model_role = ""        # cle de config modele SI differente du rol : les 4 analystes
                           # ont des rols distincts (revue separee) mais partagent
                           # le meme reglage `DESK_MODEL_ANALYSTE`.
    title = "Employe"      # libelle lisible (logs, journal)

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        d = cfg.desk
        self.model_id = d.model_for(self.model_role or self.role, cfg.bedrock.model_id)
        self.temperature = d.temperature
        self.max_tokens = d.max_tokens

    # -- appel LLM (messages bruts : le dossier contient du JSON) -----------------
    def _invoke(self, system: str, human: str) -> str:
        client = _client(self.cfg, self.model_id, self.temperature, self.max_tokens)
        try:
            resp = client.invoke([("system", system), ("human", human)])
        except Exception as exc:  # Bedrock down, quota, timeout, credentials...
            raise DeskUnavailable(f"{self.title}: appel LLM en echec ({exc})") from exc
        return resp.content if isinstance(resp.content, str) else str(resp.content)

    def ask_json(self, system: str, human: str) -> dict:
        """Un tour LLM -> dict. {} si le modele n'a pas rendu de JSON exploitable
        (ce n'est PAS une panne : l'appelant applique un defaut prudent)."""
        data = extract_json(self._invoke(system, human))
        if not data:
            log.warning("%s: reponse sans JSON exploitable (defaut prudent applique).", self.title)
        return data

    def ask_text(self, system: str, human: str) -> str:
        return self._invoke(system, human).strip()
