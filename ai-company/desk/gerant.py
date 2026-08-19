# -*- coding: utf-8 -*-
"""LE GERANT / DIRECTEUR GENERAL — il dirige l'entreprise vers l'objectif FTMO.

Ce n'est PAS lui qui trade. Son role : a chaque cycle, lire l'etat de l'entreprise
(equity, progression vers l'objectif, jours restants, book ouvert, bilan) et fixer le
MANDAT du cycle :
  - la POSTURE (agressif / selectif / prudent / preservation / arret),
  - s'il faut CONVOQUER le desk (analystes + debat + Trader) pour chercher a ouvrir,
  - QUELS candidats meritent cette recherche (borne le cout LLM),
  - des CONSIGNES courtes aux equipes.

Il est aussi le point d'escalade de la Vigie (Phase 1B) : quand un trade se degrade, il
decide de convoquer une SESSION EXTRAORDINAIRE pour l'arreter ou le modifier.

Le Gerant est CRITIQUE : s'il est injoignable, le desk se declare `degraded` et
l'orchestrateur bascule sur le pilote deterministe (aucune entree). Une reponse mal
formee (pas une panne) donne un mandat prudent par defaut : on gere le book, on n'ouvre pas.
"""
from __future__ import annotations

import logging

from desk.base import DeskAgent
from desk import context as C

log = logging.getLogger("desk.gerant")

SYSTEM = """Tu es le DIRECTEUR GENERAL d'une entreprise de trading qui passe un CHALLENGE
FTMO en 2 etapes (compte {account_size:.0f} USD, ETAPE {phase}). Tu ne trades pas toi-meme :
tu DIRIGES des employes (4 analystes, 2 debatteurs Bull/Bear, un Trader, un Risk Manager,
un Trade Manager, une Vigie) pour atteindre l'objectif SANS JAMAIS violer les regles.

OBJECTIF DE L'ENTREPRISE : reussir l'etape en cours (+{target:.0f} % du solde initial) en
respectant -{max_daily:.0f} %/jour et -{max_total:.0f} % total (limites FATALES), en au moins
{min_days} jours de trading, dans la fenetre de 30 jours. La PRESERVATION DU CAPITAL prime
toujours sur l'appat du gain.

TON ROLE CE CYCLE : fixer le mandat. Tu regardes ou en est l'entreprise et tu decides :
1. POSTURE :
   - "preservation" / "arret" si l'objectif est atteint (et le minimum de jours tenu), ou si
     la perte du jour approche la limite, ou si les ouvertures sont deja bloquees.
   - "prudent" si le contexte est incertain (peu de jours restants mais gros ecart = ne pas
     paniquer et sur-trader ; drawdown recent).
   - "selectif" par defaut : on ne prend que des setups de qualite.
   - "agressif" UNIQUEMENT si l'edge est net et le budget de risque intact (jamais > 1 %/trade).
2. CONVOQUER LE DESK : `convoquer_desk` = true seulement si ca vaut le cout de reunir analystes
   + debat + Trader ce cycle — c.-a-d. les ouvertures sont POSSIBLES (pas bloquees, objectif non
   atteint, un slot de position libre, budget de risque dispo) ET au moins un candidat est
   interessant. Sinon false : on se contente de gerer le book (le Trade Manager tourne toujours).
3. CANDIDATS : choisis au plus {max_candidates} symboles a etudier a fond ce cycle, parmi ceux
   pre-scannes (regarde le regime et l'edge des strategies). Qualite > quantite.
4. CONSIGNES : 1-3 phrases de direction concretes aux equipes (sur quoi se concentrer, quoi
   eviter, quel biais macro surveiller), fondees sur le BILAN CHIFFRE et les donnees du
   dossier — jamais sur une intuition invérifiable.

Tu DIRIGES : la revue de performance ci-dessous te dit ce que chaque employe a reellement
apporte (esperance quand l'analyste etait aligne vs oppose, quand le juge etait convaincu,
quand le college a durci...). Sers-t'en pour orienter tes consignes — mais refuse de
conclure sur un echantillon marque INSUFFISANT : diriger d'apres 3 trades est pire que ne
pas diriger du tout.

Regle absolue : chaque phrase de ton mandat doit pouvoir etre rattachee a un CHIFFRE du
dossier (etat du compte, scan, bilan, revue). Une consigne qui n'est appuyee sur rien
n'est pas une direction, c'est de la speculation : ne l'ecris pas.

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"posture": "selectif",
  "revue": "1-2 phrases: ou en est l'entreprise vs l'objectif et le risque",
  "convoquer_desk": true,
  "candidats": ["EURUSD"],
  "budget_risque_cycle_pct": 1.0,
  "consignes": "directives courtes aux equipes"}}"""


ESCALADE_SYSTEM = """Tu es le DIRECTEUR GENERAL d'une entreprise de trading FTMO (compte
{account_size:.0f} USD). Ta VIGIE vient de t'alerter sur une position ouverte, ENTRE deux
cycles de decision. Tu dois trancher une seule chose : convoque-t-on une SESSION
EXTRAORDINAIRE sur ce trade, maintenant ?

Ce qu'une session extraordinaire PEUT faire : fermer (tout ou partie), resserrer le stop,
armer un trailing. Elle ne peut RIEN ouvrir. Elle ne peut que REDUIRE le risque.

Convoque si : la these est cassee, une annonce a fort impact menace la position, un gain
significatif est en train d'etre integralement rendu, ou la perte du jour approche la limite
de -{max_daily:.0f} %. Ne convoque PAS pour du bruit de marche : un flottant negatif dans le
bruit est normal, le stop existe pour ca, et sur-gerer detruit l'esperance.

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"session_extraordinaire": false,
  "decision": "1 phrase: pourquoi on intervient (ou pourquoi on laisse courir)",
  "consignes": "si on convoque: ce que le Trade Manager doit examiner en priorite"}}"""


class Gerant(DeskAgent):
    role = "gerant"
    title = "Gerant/DG"

    def mandate(self, summary: dict, revue: str = "") -> dict:
        ctx = C.read()
        f = self.cfg.ftmo
        system = SYSTEM.format(
            account_size=f.account_size, phase=f.phase, target=f.profit_target_pct,
            max_daily=f.max_daily_loss_pct, max_total=f.max_total_loss_pct,
            min_days=f.min_trading_days, max_candidates=self.cfg.desk.max_candidates)
        dossier = "\n\n".join([
            "== ETAT DE L'ENTREPRISE (compte / FTMO) ==\n" + C.fmt(summary),
            "== POSITIONS OUVERTES ==\n" + C.fmt(C.positions_brief(ctx.get("positions"))),
            "== CANDIDATS PRE-SCANNES ==\n" + C.fmt(C.candidate_symbols(ctx)),
            "== REGIME / STRATEGIES ==\n" + (ctx.get("strategies") or "(aucun)"),
            "== BILAN / POST-MORTEM ==\n" + (ctx.get("postmortem") or "(aucun)"),
            "== REVUE DE PERFORMANCE DES EMPLOYES ==\n" + (revue or "(aucune)"),
        ])
        data = self.ask_json(system, dossier + "\n\nRends UNIQUEMENT le mandat JSON.")
        return self._normalise(data, ctx)

    # ------------------------------------------------------------------ escalade Vigie
    def escalade(self, position: dict, verdict: dict, declencheurs: list[str],
                 summary: dict) -> dict:
        """La Vigie a escalade un trade : convoque-t-on une SESSION EXTRAORDINAIRE ?

        Rendez-vous cout/benefice : reunir le desk hors cycle coute des appels LLM, mais
        attendre le prochain cycle peut couter bien plus cher. En cas de reponse mal
        formee, on NE convoque PAS (defaut prudent : les protections deterministes tiennent
        deja le trade). Peut lever DeskUnavailable."""
        f = self.cfg.ftmo
        dossier = "\n\n".join([
            "== ALERTE DE LA VIGIE ==\n" + C.fmt(verdict),
            "== DECLENCHEURS (deterministes) ==\n" + C.fmt(declencheurs),
            "== POSITION CONCERNEE ==\n" + C.fmt(position),
            "== ETAT DE L'ENTREPRISE ==\n" + C.fmt(summary),
        ])
        data = self.ask_json(ESCALADE_SYSTEM.format(
            account_size=f.account_size, max_daily=f.max_daily_loss_pct), dossier
            + "\n\nRends UNIQUEMENT ta decision JSON.")
        return {"session_extraordinaire": bool(data.get("session_extraordinaire", False)),
                "consignes": str(data.get("consignes") or "").strip(),
                "decision": str(data.get("decision") or "").strip()}

    # ------------------------------------------------------------------ arbitrage du risque
    def arbitrer_risque(self, proposes: list[dict], avis: dict, summary: dict) -> dict:
        """Tranche entre les trois temperaments du college du risque (Phase 3B).

        Rend `{"verdicts": [...]}` au format attendu par `risk_manager.appliquer_verdicts` :
        le durcissement y est traduit une seule fois, et `reduce` ne peut que PLAFONNER le
        risque. Une reponse vide laisse les ouvertures au moteur FTMO deterministe — jamais
        une approbation implicite plus large que le budget. Peut lever DeskUnavailable."""
        from desk.risque import ARBITRAGE_SYSTEM
        f = self.cfg.ftmo
        dossier = "\n\n".join([
            "== OUVERTURES PROPOSEES ==\n" + C.fmt(proposes),
            "== AVIS DU COLLEGE DU RISQUE ==\n" + C.fmt(avis),
            "== ETAT DE L'ENTREPRISE ==\n" + C.fmt(summary),
        ])
        data = self.ask_json(
            ARBITRAGE_SYSTEM.format(account_size=f.account_size, phase=f.phase,
                                    risk_pct=f.risk_per_trade_pct),
            dossier + "\n\nRends UNIQUEMENT tes verdicts JSON.")
        if data.get("synthese"):
            log.info("Arbitrage DG: %s", str(data["synthese"])[:240])
        return data

    def _normalise(self, data: dict, ctx: dict) -> dict:
        """Defaut PRUDENT si la reponse est vide/mal formee : gerer le book, ne pas ouvrir."""
        valides = set(C.candidate_symbols(ctx))
        candidats = [str(s).strip().upper() for s in (data.get("candidats") or [])
                     if str(s).strip().upper() in valides][: self.cfg.desk.max_candidates]
        convoquer = bool(data.get("convoquer_desk", False)) and bool(candidats)
        posture = str(data.get("posture") or "prudent").strip().lower()
        return {
            "posture": posture,
            "revue": str(data.get("revue") or "").strip(),
            "convoquer_desk": convoquer,
            "candidats": candidats,
            "budget_risque_cycle_pct": data.get("budget_risque_cycle_pct"),
            "consignes": str(data.get("consignes") or "").strip(),
        }
