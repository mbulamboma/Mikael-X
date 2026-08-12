# -*- coding: utf-8 -*-
"""LA VIGIE — elle regarde les positions ouvertes ENTRE deux cycles de decision.

Le cycle du desk est horaire (profil swing). Un trade peut se degrader en quinze
minutes : la these casse, une annonce tombe, le gain de +2R fond. Le watchdog
deterministe protege deja (SL d'urgence, break-even, trailing, panique perte-jour),
mais il ne SAIT pas lire une situation — il applique des seuils.

La Vigie comble ce trou : quand un declencheur DETERMINISTE et bon marche se leve
(perte flottante, gains rendus, news imminente), elle est consultee sur CE trade et
rend une gravite :
    "ok"          -> rien a signaler, on laisse courir ;
    "surveiller"  -> degradation reelle mais pas encore actionnable ;
    "escalader"   -> le Gerant doit trancher (session extraordinaire).

GARDE-FOUS (cf. IMPLEMENTATION.md §2b) :
  - jamais d'appel LLM a vide : il faut un declencheur deterministe ;
  - rate-limitee par ticket (`DESK_VIGIE_MIN_MINUTES`) ;
  - si elle est injoignable, RIEN ne casse : le watchdog deterministe protege deja ;
  - elle ne decide rien elle-meme et ne peut jamais faire OUVRIR une position.
"""
from __future__ import annotations

import logging

from desk.base import DeskAgent
from desk import context as C

log = logging.getLogger("desk.vigie")

SYSTEM = """Tu es la VIGIE d'une entreprise de trading FTMO (compte {account_size:.0f} USD).
Ton unique job : surveiller UNE position ouverte et dire si elle merite qu'on reveille le
Directeur General MAINTENANT, sans attendre le prochain cycle de decision.

On te donne la position (avec son profit flottant en R, son MFE/MAE, sa distance au stop,
son age), les declencheurs deterministes qui t'ont reveillee, l'etat du compte et les news.

GRAVITE a rendre :
- "ok"         : la position respire normalement (un flottant negatif n'est PAS une urgence :
                 un stop est fait pour etre touche). Ne reveille personne pour du bruit.
- "surveiller" : degradation reelle mais la these tient encore ; on repasse plus tard.
- "escalader"  : la these est cassee, ou le risque a change de nature — annonce a fort impact
                 imminente sur la devise, gain significatif en train d'etre integralement
                 rendu, position qui derive vers le stop sans aucun signe de reprise.

Sois AVARE d'escalades : chaque escalade convoque une reunion. Mais ne rate pas une these
cassee — c'est exactement ce qui transforme un -1R en catastrophe.

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"gravite": "ok",
  "raison": "1-2 phrases fondees sur les CHIFFRES de la position",
  "recommandation": "ce que tu ferais si on te suivait (fermer / resserrer / laisser courir)"}}"""


class Vigie(DeskAgent):
    role = "vigie"
    title = "Vigie"

    def evaluate(self, position: dict, declencheurs: list[str], summary: dict) -> dict:
        """Evalue UNE position. Peut lever DeskUnavailable (LLM injoignable) : l'appelant
        se contente alors des protections deterministes."""
        f = self.cfg.ftmo
        ctx = C.read()
        news = ctx.get("news") or {}
        bo = (news.get("blackout") or {}) if isinstance(news, dict) else {}
        dossier = "\n\n".join([
            "== POSITION SURVEILLEE ==\n" + C.fmt(position),
            "== DECLENCHEURS (deterministes) ==\n" + C.fmt(declencheurs),
            "== COMPTE / FTMO ==\n" + C.fmt(summary),
            "== NEWS / BLACK-OUT DU SYMBOLE ==\n"
            + C.fmt(bo.get(position.get("symbol")) or {}),
        ])
        data = self.ask_json(SYSTEM.format(account_size=f.account_size),
                             dossier + "\n\nRends UNIQUEMENT ton verdict JSON.")
        gravite = str(data.get("gravite") or "").strip().lower()
        if gravite not in ("ok", "surveiller", "escalader"):
            # Reponse mal formee : on NE reveille PAS le DG (defaut prudent = statu quo,
            # les protections deterministes restent en place).
            log.info("Vigie: verdict illisible sur le ticket %s -> statu quo.",
                     position.get("ticket"))
            gravite = "surveiller"
        return {"gravite": gravite,
                "raison": str(data.get("raison") or "").strip(),
                "recommandation": str(data.get("recommandation") or "").strip()}
