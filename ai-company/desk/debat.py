# -*- coding: utf-8 -*-
"""LE DEBAT BULL / BEAR — et surtout SON JUGE.

Deux debatteurs recoivent les memes briefs d'analystes et defendent la these opposee :
le BULL plaide l'achat, le BEAR plaide la vente ou l'abstention. Le BEAR parle en second :
il voit la these du Bull et doit la REFUTER, pas simplement dire l'inverse.

Le point important — et c'est ce qui manquait au desk — est le **JUGE**. Sans arbitre, le
Trader lisait deux plaidoiries et choisissait celle qui l'arrangeait : le biais rentre par
la fenetre juste apres avoir ete chasse par la porte. Le juge tranche, ecrit un PLAN
(direction retenue, invalidation, risques non resolus), et ce plan s'impose : le desk
supprime deterministiquement toute ouverture qui le contredit (cf. desk/desk.py).

DEBAT ADAPTATIF (cout) : un seul tour par defaut. Le second tour n'a lieu QUE si le debat
est SERRE — convictions proches (`DESK_DEBATE_GAP`) — c'est-a-dire quand la contradiction
a encore quelque chose a apprendre. Payer un second tour quand un camp ecrase l'autre est
de l'argent jete.

PANNE : toute indisponibilite pendant le debat d'un symbole retire le debat de CE symbole
(on ne garde jamais une plaidoirie sans son contradicteur, ni un debat sans juge). Les
briefs des analystes continuent d'aller au Trader. Aucune bascule pilote : c'est de la
recherche, pas de la gestion de position.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from desk.base import DeskAgent, DeskUnavailable
from desk import context as C

log = logging.getLogger("desk.debat")

_COMMUN = """Entreprise de trading FTMO (compte {account_size:.0f} USD), profil SWING
(position tenue plusieurs jours). Symbole en discussion : {symbol}.

Tu recois les briefs des 4 analystes (technique, fondamental, sentiment, actualite). Tu
n'es PAS la pour resumer : tu es la pour PLAIDER, avec les chiffres du dossier. Une
plaidoiree sans chiffre ne vaut rien. Ta conviction doit etre honnete : si le dossier ne
soutient pas ta position, dis-le et rends une conviction basse — c'est une information
precieuse pour le juge, pas un aveu de faiblesse."""

BULL_SYSTEM = _COMMUN + """

TU ES LE BULL. Tu plaides l'ACHAT de {symbol}. Montre ce qui porte le prix a la hausse sur
plusieurs jours, ou se situerait une entree defendable, et pourquoi le risque vaut la peine.
{contradiction}

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"conviction": 0.6,
  "these": "2-4 phrases chiffrees defendant l'achat",
  "arguments": ["fait chiffre 1", "fait chiffre 2"],
  "risque_principal": "le meilleur argument CONTRE toi, formule honnetement",
  "refutation": "ce que tu reponds au Bear (vide au premier tour)"}}"""

BEAR_SYSTEM = _COMMUN + """

TU ES LE BEAR. Tu plaides la VENTE de {symbol} — ou, si rien ne justifie une position dans
un sens comme dans l'autre, l'ABSTENTION (ne pas trader est une conclusion parfaitement
valable et souvent la bonne). Tu parles en SECOND : tu vois la these du Bull et tu dois la
REFUTER point par point, pas seulement affirmer l'inverse.
{contradiction}

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"conviction": 0.6,
  "these": "2-4 phrases chiffrees defendant la vente ou l'abstention",
  "arguments": ["fait chiffre 1", "fait chiffre 2"],
  "risque_principal": "le meilleur argument CONTRE toi, formule honnetement",
  "refutation": "ta refutation directe de la these du Bull"}}"""

JUGE_SYSTEM = """Tu es le DIRECTEUR DE LA RECHERCHE d'une entreprise de trading FTMO (compte
{account_size:.0f} USD). Tu viens d'entendre le Bull et le Bear sur {symbol}, apres lecture
des memes briefs. Tu TRANCHES, et ton plan s'impose au Trader.

Comment juger :
- pese les ARGUMENTS CHIFFRES, pas le ton ni la longueur ; une conviction elevee sans
  chiffre ne pese rien ;
- regarde ce que chacun concede : le `risque_principal` d'un camp est souvent le vrai sujet ;
- **l'abstention est un verdict a part entiere**. Deux plaidoiries faibles ne font pas un
  trade. En FTMO, ne pas trader ne coute rien ; trader un dossier flou coute le compte ;
- ta CONVICTION doit refleter la solidite du dossier, pas ton envie de conclure.

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"direction": "buy|sell|abstention",
  "conviction": 0.6,
  "gagnant": "bull|bear|aucun",
  "plan": "2-3 phrases: la these retenue et ce qu'on cherche a capturer",
  "invalidation": "le fait precis qui prouverait que la these retenue est fausse",
  "risques_non_resolus": ["ce que le debat n'a PAS tranche"]}}"""


def _conviction(data: dict) -> float:
    try:
        return round(min(1.0, max(0.0, float(data.get("conviction", 0.0)))), 2)
    except (TypeError, ValueError):
        return 0.0


def _liste(data: dict, cle: str, n: int = 5) -> list[str]:
    brut = data.get(cle)
    return [str(x)[:220] for x in (brut if isinstance(brut, list) else [])][:n]


class Debatteur(DeskAgent):
    model_role = "debat"
    system = ""

    def plaide(self, symbol: str, briefs: dict, mandate: dict, adverse: Optional[dict],
               lessons: str) -> dict:
        """Une plaidoirie. `adverse` = la these deja posee par l'autre camp (None au
        premier tour du Bull)."""
        contradiction = ""
        if adverse:
            contradiction = ("\nTu dois traiter FRONTALEMENT la these adverse ci-dessous : "
                             "attaque ses chiffres, pas son intention.")
        system = self.system.format(account_size=self.cfg.ftmo.account_size, symbol=symbol,
                                    contradiction=contradiction)
        blocs = ["== BRIEFS DES ANALYSTES ==\n" + C.fmt(briefs),
                 "== CONSIGNES DU GERANT ==\n" + (mandate.get("consignes") or "(aucune)")]
        if adverse:
            blocs.append("== THESE ADVERSE (a refuter) ==\n" + C.fmt(adverse))
        blocs.append("== TES LECONS PASSEES ==\n" + (lessons or "(aucune)"))
        data = self.ask_json(system, "\n\n".join(blocs) + "\n\nRends UNIQUEMENT ta plaidoirie JSON.")
        return {"camp": self.role, "conviction": _conviction(data),
                "these": str(data.get("these") or "").strip()[:800],
                "arguments": _liste(data, "arguments"),
                "risque_principal": str(data.get("risque_principal") or "").strip()[:300],
                "refutation": str(data.get("refutation") or "").strip()[:500]}


class Bull(Debatteur):
    role, title, system = "bull", "Bull", BULL_SYSTEM


class Bear(Debatteur):
    role, title, system = "bear", "Bear", BEAR_SYSTEM


class Juge(DeskAgent):
    role, title, model_role = "juge", "Juge du debat", "debat"

    def trancher(self, symbol: str, briefs: dict, tours: list[dict], lessons: str) -> dict:
        system = JUGE_SYSTEM.format(account_size=self.cfg.ftmo.account_size, symbol=symbol)
        human = "\n\n".join([
            "== BRIEFS DES ANALYSTES ==\n" + C.fmt(briefs),
            "== DEBAT (dans l'ordre) ==\n" + C.fmt(tours),
            "== TES LECONS DE JUGEMENT ==\n" + (lessons or "(aucune)"),
        ])
        data = self.ask_json(system, human + "\n\nRends UNIQUEMENT ton verdict JSON.")
        direction = str(data.get("direction") or "").strip().lower()
        if direction not in ("buy", "sell", "abstention"):
            # Verdict illisible : on ne devine pas une direction. L'abstention est le seul
            # defaut sur : elle ne peut pas faire perdre d'argent.
            log.info("Juge: verdict illisible sur %s -> abstention.", symbol)
            direction = "abstention"
        return {"direction": direction, "conviction": _conviction(data),
                "gagnant": str(data.get("gagnant") or "").strip().lower(),
                "plan": str(data.get("plan") or "").strip()[:600],
                "invalidation": str(data.get("invalidation") or "").strip()[:300],
                "risques_non_resolus": _liste(data, "risques_non_resolus")}


class Debat:
    """Organise le debat sur chaque candidat et rend {symbole: {bull, bear, verdict, tours}}."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.bull = Bull(cfg)
        self.bear = Bear(cfg)
        self.juge = Juge(cfg)

    def sur(self, symbols: list[str], briefs: dict, mandate: dict,
            lessons_for: Optional[Callable[[list[str]], str]] = None) -> dict:
        if not symbols:
            return {}
        out = {}
        for symbol in symbols:
            try:
                resultat = self._un_symbole(symbol, briefs.get(symbol) or {}, mandate,
                                            lessons_for)
            except DeskUnavailable as e:
                # Jamais de plaidoirie sans contradicteur, ni de debat sans juge.
                log.warning("Debat sur %s abandonne (%s) — le Trader decidera sans lui.",
                            symbol, e)
                continue
            out[symbol] = resultat
            v = resultat["verdict"]
            log.info("Debat %s: %s (conviction %.2f, %d tour(s)) | %s", symbol,
                     v["direction"], v["conviction"], resultat["tours"], v["plan"][:160])
        return out

    def _un_symbole(self, symbol: str, briefs: dict, mandate: dict, lessons_for) -> dict:
        def lecons(role):
            return lessons_for([role]) if lessons_for else ""

        bull = self.bull.plaide(symbol, briefs, mandate, None, lecons("bull"))
        bear = self.bear.plaide(symbol, briefs, mandate, bull, lecons("bear"))
        tours = [bull, bear]
        n = 1
        while n < max(1, self.cfg.desk.debate_rounds) and self._serre(bull, bear):
            # Debat serre : la contradiction a encore quelque chose a apprendre.
            n += 1
            log.info("Debat %s serre (%.2f vs %.2f) -> tour %d.", symbol,
                     bull["conviction"], bear["conviction"], n)
            bull = self.bull.plaide(symbol, briefs, mandate, bear, lecons("bull"))
            bear = self.bear.plaide(symbol, briefs, mandate, bull, lecons("bear"))
            tours += [bull, bear]
        verdict = self.juge.trancher(symbol, briefs, tours, lecons("juge"))
        return {"bull": bull, "bear": bear, "tours": n, "verdict": verdict}

    def _serre(self, bull: dict, bear: dict) -> bool:
        return abs(bull["conviction"] - bear["conviction"]) <= self.cfg.desk.debate_gap
