# -*- coding: utf-8 -*-
"""LE TRADER — il TRANCHE les nouvelles entrees. Expert FTMO, il sait tout du challenge.

Il recoit le mandat du Gerant, (Phase 2) les briefs des 4 analystes, (Phase 3) la synthese
du debat Bull/Bear, plus le regime, le classement des strategies, le bilan chiffre et les cas
passes comparables. Il decide : pour chaque candidat, ouvrir (a des niveaux techniques REELS,
R:R >= 2, stop a un vrai niveau de structure) ou s'abstenir. Il ne calcule JAMAIS le lot
(moteur FTMO).

Il ne gere pas le book (c'est le Trade Manager) : il ne rend que des actions `open`. Ses
ouvertures passent ensuite par le controle de preuves (desk/preuves.py : une these qui ne
cite aucune donnee du dossier est supprimee), par le Risk Manager (qui peut durcir) puis par
le moteur FTMO deterministe (sizing + veto). Tant qu'un DEFAUT RECURRENT figure au bilan
chiffre, le corriger prime sur toute nouvelle idee.

Non critique : si le Trader echoue, on n'ouvre simplement pas ce cycle (le book reste gere).
"""
from __future__ import annotations

import logging

from brain import tools as T
from strategy import playbooks
from desk.base import DeskAgent
from desk import context as C

log = logging.getLogger("desk.trader")

SYSTEM = """Tu es le TRADER en chef d'une entreprise de trading FTMO (compte {account_size:.0f}
USD, ETAPE {phase}). Tu es un EXPERT du challenge FTMO et du swing (tenue sur plusieurs jours).
Ton objectif : faire progresser le compte vers +{target:.0f} % sans jamais menacer -{max_daily:.0f}
%/jour ni -{max_total:.0f} % total. PRESERVATION DU CAPITAL avant tout.

Le Gerant t'a donne un mandat et des candidats. Pour CHAQUE candidat, tu TRANCHES : ouvrir
ou t'abstenir. S'abstenir est une decision valable et frequente.

METHODE (raisonne comme un pro, pas comme un backtest naif) :
- STRATEGIE : choisis un nom EXACT du catalogue selon le regime et son edge historique.
- NIVEAUX REELS : entry/sl/tp a des niveaux techniques du graphique (structure, Donchian,
  swing). Stop LARGE devant le spread (>= 10x spread et >= 1x ATR de structure). R:R NET
  (apres frais) >= 2. Le moteur refuse un R:R net < 1 et un stop mange par les frais.
- COUTS : verifie spread, commission, swap (portage swing) via le dossier. Un stop trop serre
  donne un lot enorme et un R:R net ridicule.
- MACRO & NEWS : aligne la direction sur le biais macro ; n'ouvre RIEN si un black-out est
  actif sur le symbole (news a fort impact imminente).
- VERDICT DU JUGE : quand un debat a eu lieu sur un candidat, le plan du juge S'IMPOSE. S'il
  a conclu a l'abstention, ou dans l'autre sens que toi, tu ne proposes RIEN sur ce candidat
  (le desk supprimerait l'ordre de toute facon). Ton travail est de traduire son plan en
  niveaux executables, pas de rejuger le debat.
- MEMOIRE SITUATIONNELLE : on te donne les trades passes dont la CONFIGURATION ressemblait
  le plus a celle d'aujourd'hui, avec leur resultat reel. Si ces cas-la ont une esperance
  negative, il te faut une raison explicite de croire que cette fois est differente.
- BILAN : corrige d'abord les DEFAUTS RECURRENTS listes dans le bilan chiffre. La confiance
  n'est pas un edge : exige la preuve technique.
- PREUVES (controle automatique) : ta `rationale` doit CITER des donnees du dossier — le
  niveau exact, l'ATR, le RSI, le spread, l'ecart de taux, la date de l'annonce. Une
  ouverture dont la these ne cite aucune donnee du dossier est SUPPRIMEE avant meme le
  controle du risque, sans discussion. Ecris ce que tu peux montrer, pas ce que tu anticipes.

Tu peux proposer PLUSIEURS ouvertures (une par candidat au plus). Le lot est calcule par le
moteur de risque, pas par toi. Confiance dans [0,1], honnete.

CATALOGUE DE STRATEGIES :
{playbooks}

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"analyse": "2-3 phrases: regime, biais, ce que tu fais et pourquoi",
  "actions": [
    {{"type": "open", "strategy": "trend_follow", "symbol": "EURUSD", "direction": "buy",
      "entry": 1.1000, "sl": 1.0900, "tp": 1.1250, "confidence": 0.7,
      "rationale": "these CHIFFREE : niveaux/ATR/taux cites du dossier + ce qui l'invaliderait"}}
  ]}}
"actions" vide = ne rien ouvrir. Coherence : buy -> sl<entry<tp ; sell -> tp<entry<sl."""


class Trader(DeskAgent):
    role = "trader"
    title = "Trader"

    def decide(self, summary: dict, mandate: dict, briefs: dict, debate: dict,
               memoire: dict | None = None) -> list[dict]:
        """Rend une liste d'actions `open` (deja validees en coherence). `briefs` et
        `debate` sont vides en Phase 1 (branches en Phase 2/3)."""
        ctx = C.read()
        candidats = mandate.get("candidats") or []
        if not candidats:
            return []
        f = self.cfg.ftmo
        system = SYSTEM.format(
            account_size=f.account_size, phase=f.phase, target=f.profit_target_pct,
            max_daily=f.max_daily_loss_pct, max_total=f.max_total_loss_pct,
            playbooks=playbooks.as_prompt_block())
        blocs = [
            "== MANDAT DU GERANT ==\n" + C.fmt(mandate),
            "== CANDIDATS A TRANCHER ==\n" + C.fmt(candidats),
            "== DOSSIER DU CYCLE ==\n" + T.context_digest(),
        ]
        if briefs:
            blocs.append("== BRIEFS DES ANALYSTES ==\n" + C.fmt(briefs))
        if debate:
            blocs.append("== DEBAT BULL/BEAR ET VERDICT DU JUGE (contraignant) ==\n"
                         + C.fmt(debate))
        if memoire:
            blocs.append("== CAS PASSES LES PLUS PROCHES (memoire situationnelle) ==\n"
                         + "\n".join(f"[{sym}]\n{bloc}" for sym, bloc in memoire.items()))
        plan = self.ask_json(system, "\n\n".join(blocs)
                             + "\n\nRends UNIQUEMENT le plan d'ouvertures JSON.")
        if plan.get("analyse"):
            log.info("Trader: %s", str(plan["analyse"])[:300])
        # ne garder QUE les ouvertures des candidats retenus par le Gerant
        valides = {str(s).strip().upper() for s in candidats}
        plan["actions"] = [a for a in (plan.get("actions") or [])
                           if isinstance(a, dict) and str(a.get("type")).lower() == "open"
                           and str(a.get("symbol", "")).strip().upper() in valides]
        return T.apply_plan(plan)
