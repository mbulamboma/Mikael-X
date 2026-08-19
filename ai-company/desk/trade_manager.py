# -*- coding: utf-8 -*-
"""LE TRADE MANAGER — il gere les positions OUVERTES (le book), a chaque cycle.

Sa devise : "gerer l'existant AVANT de chercher du neuf". Pour chaque position il decide :
securiser (break-even), laisser courir (trailing), reduire (prise partielle), ou sortir
(these cassee, biais macro contre, news imminente, trade qui traine). Il doit respecter le
Risk Manager, les regles FTMO et l'objectif fixe par le Gerant.

Il NE PEUT PAS ouvrir de position : il ne rend que des actions close / modify / trail. Ces
actions REDUISENT le risque -> l'orchestrateur les applique sans repasser par le sizing FTMO
(un simple garde-fou empeche un SL d'AUGMENTER le risque au-dela du budget).

CRITIQUE : si le Trade Manager est injoignable, le desk se declare `degraded` et le pilote
deterministe (brain/autopilot.py) protege le book. On ne laisse jamais le book sans pilote.
"""
from __future__ import annotations

import logging

from brain import tools as T
from desk.base import DeskAgent
from desk import context as C

log = logging.getLogger("desk.trade_manager")

SYSTEM = """Tu es le TRADE MANAGER (gestion de position) d'une entreprise de trading FTMO
(compte {account_size:.0f} USD, ETAPE {phase}, objectif +{target:.0f} %). Tu geres UNIQUEMENT
les positions DEJA OUVERTES. Tu ne cherches pas de nouveau trade (ce n'est pas ton role).

Pour CHAQUE position ouverte, decide l'action juste, dans cet esprit :
- THESE CASSEE ou biais macro devenu contraire -> `close` (fraction 1.0).
- Profit >= +1R -> securise : `modify` (stop au break-even = prix d'entree) ET/OU `trail`
  (armer un trailing stop : atr_mult ~2.0, activate_r ~1.0, timeframe D1) pour laisser courir
  en verrouillant les gains. C'est le remede n1 au defaut "gains rendus".
- Gros gain deja rendu partiellement, ou news a fort impact imminente -> `close` partiel
  (fraction 0.5) pour securiser, ou resserre le stop.
- Position qui traine longtemps sans rien donner -> envisage une sortie (time-stop).
- Un `modify` ne doit JAMAIS eloigner le stop au point d'augmenter le risque (l'orchestrateur
  refusera). Un stop ne se deplace que pour REDUIRE le risque ou verrouiller du profit.

Respecte les CONSIGNES du Gerant et corrige d'abord les DEFAUTS RECURRENTS du bilan
chiffre (gains rendus, stop trop serre...). Chaque `reason` doit citer un CHIFFRE de la
position (R flottant, MFE/MAE, distance au stop, age) : une action justifiee par une
impression n'est pas une gestion de position. Si une position va bien et
n'appelle aucune action, ne fais RIEN dessus (ne bouge pas un stop pour bouger).

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"analyse": "1-2 phrases sur l'etat du book",
  "actions": [
    {{"type": "modify", "ticket": 123, "sl": 1.0950, "tp": 0, "reason": "break-even"}},
    {{"type": "trail",  "ticket": 123, "atr_mult": 2.0, "activate_r": 1.0, "timeframe": "D1",
      "enabled": true, "reason": "laisser courir en verrouillant"}},
    {{"type": "close",  "ticket": 456, "fraction": 1.0, "reason": "these cassee"}}
  ]}}
"actions" vide = ne rien faire (parfaitement acceptable si le book est sain). Les prix sont
des PRIX reels tires des positions. Coherence des stops obligatoire."""


class TradeManager(DeskAgent):
    role = "suivi"
    title = "Trade Manager"

    def manage(self, summary: dict, consignes: str) -> list[dict]:
        ctx = C.read()
        positions = ctx.get("positions") or []
        if not positions:
            return []
        f = self.cfg.ftmo
        system = SYSTEM.format(account_size=f.account_size, phase=f.phase,
                               target=f.profit_target_pct)
        news = ctx.get("news") or {}
        dossier = "\n\n".join([
            "== COMPTE / FTMO ==\n" + C.fmt(summary),
            "== POSITIONS OUVERTES (a gerer) ==\n" + C.fmt(positions),
            "== CONSIGNES DU GERANT ==\n" + (consignes or "(aucune)"),
            "== NEWS / BLACK-OUT ==\n" + C.fmt({"blackout": news.get("blackout", {})}
                                               if isinstance(news, dict) else {}),
            "== BILAN / DEFAUTS RECURRENTS ==\n" + (ctx.get("postmortem") or "(aucun)"),
        ])
        plan = self.ask_json(system, dossier + "\n\nRends UNIQUEMENT le plan d'actions JSON.")
        if plan.get("analyse"):
            log.info("Trade Manager: %s", str(plan["analyse"])[:300])
        # on ne garde QUE les actions de gestion (jamais d'ouverture ici)
        plan["actions"] = [a for a in (plan.get("actions") or [])
                           if isinstance(a, dict) and str(a.get("type")).lower() in
                           {"close", "modify", "trail"}]
        return T.apply_plan(plan)
