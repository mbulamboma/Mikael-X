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
import time
from typing import Any

from desk import trace

try:
    from langchain_aws import ChatBedrockConverse
    _LC_OK = True
    _LC_ERR: Exception | None = None
except Exception as exc:  # pragma: no cover - dependance optionnelle
    ChatBedrockConverse = None
    _LC_OK = False
    _LC_ERR = exc

try:  # outils optionnels : seuls les analystes tool-capables s'en servent (cf. run_tools)
    # LangChain 1.x : `create_agent` (graphe langgraph) remplace AgentExecutor +
    # create_tool_calling_agent, supprimes de langchain.agents.
    from langchain.agents import create_agent
    from langgraph.errors import GraphRecursionError
    _TOOLS_OK = True
except Exception:  # pragma: no cover - dependance optionnelle
    create_agent = None
    GraphRecursionError = ()
    _TOOLS_OK = False

from config import AgentConfig

log = logging.getLogger("desk")


def _texte_final(messages: list) -> str:
    """Dernier message d'assistant non vide d'un graphe `create_agent`. '' si aucun."""
    for m in reversed(messages or []):
        if getattr(m, "type", "") != "ai":
            continue
        c = getattr(m, "content", "")
        if isinstance(c, list):        # contenu en blocs : on ne garde que le texte
            c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
        if isinstance(c, str) and c.strip():
            return c
    return ""


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
        t0 = time.perf_counter()
        try:
            resp = client.invoke([("system", system), ("human", human)])
        except Exception as exc:  # Bedrock down, quota, timeout, credentials...
            trace.record(self.role, self.title, self.model_id, system, human,
                         (time.perf_counter() - t0) * 1000, error=str(exc))
            raise DeskUnavailable(f"{self.title}: appel LLM en echec ({exc})") from exc
        out = resp.content if isinstance(resp.content, str) else str(resp.content)
        trace.record(self.role, self.title, self.model_id, system, human,
                     (time.perf_counter() - t0) * 1000, output=out)
        return out

    def ask_json(self, system: str, human: str) -> dict:
        """Un tour LLM -> dict. {} si le modele n'a pas rendu de JSON exploitable
        (ce n'est PAS une panne : l'appelant applique un defaut prudent)."""
        data = extract_json(self._invoke(system, human))
        if not data:
            log.warning("%s: reponse sans JSON exploitable (defaut prudent applique).", self.title)
        return data

    def ask_text(self, system: str, human: str) -> str:
        return self._invoke(system, human).strip()

    # -- appel LLM AVEC OUTILS (boucle ReAct bornee) ------------------------------
    def tools_available(self) -> bool:
        """Le tool calling est-il possible ? (dependance presente + modele capable).
        `create_tool_calling_agent` echouerait a l'execution sur DeepSeek : on le sait
        d'avance via l'heuristique de la config, ce qui evite un appel rate."""
        return _TOOLS_OK and _LC_OK and self.cfg.bedrock.supports_tools

    def run_tools(self, system: str, human: str, tools: list, max_iter: int = 3,
                  return_steps: bool = False):
        """Fait raisonner l'agent avec un sous-ensemble d'OUTILS lecture seule : il peut
        aller CHERCHER une donnee non pre-chargee avant de repondre. Rend le texte final
        (ou, si `return_steps`, le couple (texte, observations_outils)).

        `system`/`human` sont passes comme DONNEES (variables du template), pas comme gabarit :
        nos prompts contiennent du JSON dont les accolades casseraient un ChatPromptTemplate.
        Leve DeskUnavailable comme `_invoke` pour que l'appelant applique son repli."""
        if not self.tools_available():
            raise DeskUnavailable(f"{self.title}: outils indisponibles (modele/dependance).")
        client = _client(self.cfg, self.model_id, self.temperature, self.max_tokens)
        t0 = time.perf_counter()
        try:
            agent = create_agent(model=client, tools=tools, system_prompt=system)
            # Un tour = appel du modele + execution de l'outil (2 noeuds du graphe) ;
            # +2 pour l'entree et la reponse finale.
            config = {"recursion_limit": 2 * int(max_iter) + 2}
            etat: dict = {}
            try:
                for etape in agent.stream({"messages": [("user", human)]}, config=config,
                                          stream_mode="values"):
                    etat = etape
            except GraphRecursionError:
                # budget d'outils epuise : on garde ce qui a ete lu plutot que de tout perdre
                # (l'ancien AgentExecutor s'arretait de meme a `max_iterations`).
                log.info("%s: budget d'outils atteint (%s tours).", self.title, max_iter)
            messages = etat.get("messages") or []
            out = _texte_final(messages)
            # observations des outils : ce que l'agent a REELLEMENT lu (pour que le filtre de
            # preuves reconnaisse comme sourcees les valeurs qu'il vient de chercher).
            obs = [str(getattr(m, "content", "")) for m in messages
                   if getattr(m, "type", "") == "tool"]
        except Exception as exc:
            trace.record(self.role, self.title, self.model_id, system, human,
                         (time.perf_counter() - t0) * 1000, error=f"tools: {exc}")
            raise DeskUnavailable(f"{self.title}: boucle d'outils en echec ({exc})") from exc
        trace.record(self.role, self.title + " (outils)", self.model_id, system, human,
                     (time.perf_counter() - t0) * 1000, output=out)
        return (out, obs) if return_steps else out
