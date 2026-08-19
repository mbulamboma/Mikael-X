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

RIEN NE SE PLAIDE SANS PREUVE. Chaque argument est confronte au dossier (desk/preuves.py) :
celui qui ne cite aucune donnee observable est ECARTE avant d'etre lu, et un camp qui n'en
garde aucun voit sa conviction ramenee a zero — plaider sans chiffre ne doit pas payer. Le
verdict du juge subit le meme controle : une direction (buy/sell) dont le plan et
l'invalidation ne renvoient a aucune donnee du dossier devient une ABSTENTION. C'est la
consequence directe du garde-fou : une affirmation invérifiable peut faire renoncer a un
trade, jamais en declencher un.

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
from desk import preuves as P

log = logging.getLogger("desk.debat")

_COMMUN = """Entreprise de trading FTMO (compte {account_size:.0f} USD), profil SWING
(position tenue plusieurs jours). Symbole en discussion : {symbol}.

Tu recois les briefs des 4 analystes (technique, fondamental, sentiment, actualite). Tu
n'es PAS la pour resumer : tu es la pour PLAIDER, avec les chiffres du dossier.

REGLE ABSOLUE : chaque argument doit CITER une donnee presente dans les briefs ou le dossier
(niveau de prix, ATR, RSI, spread, taux, date d'annonce). Un argument sans donnee du dossier
est SUPPRIME automatiquement avant que le juge te lise, et un camp qui n'a plus aucun argument
voit sa conviction ramenee a 0. Ce qui « devrait », ce qui « semble », ce que « le marche va
chercher » ne sont pas des arguments. N'invente jamais un chiffre pour passer le filtre : le
dossier fait foi.

Ta conviction doit etre honnete : si le dossier ne soutient pas ta position, dis-le et rends
une conviction basse — c'est une information precieuse pour le juge, pas un aveu de
faiblesse."""

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
- les arguments non sourcés ont deja ete retires des plaidoiries : ce que tu lis est ce qui
  a survecu au dossier. Ton `plan` et ton `invalidation` doivent EUX AUSSI citer des donnees
  du dossier — sinon ton verdict est automatiquement ramene a l'abstention ;
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
               faits: P.Faits) -> dict:
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
        data = self.ask_json(system, "\n\n".join(blocs) + "\n\nRends UNIQUEMENT ta plaidoirie JSON.")
        arguments, ecartes = _liste(data, "arguments"), []
        conviction = _conviction(data)
        if self.cfg.desk.exiger_preuves:
            arguments, ecartes = P.filtrer(arguments, faits)
            if ecartes:
                log.info("%s %s: %d argument(s) sans preuve ecarte(s).",
                         self.title, symbol, len(ecartes))
            if not arguments:
                # Plaider sans chiffre ne doit pas payer : la conviction tombe a zero.
                # La plaidoirie reste lisible (le juge doit voir qu'elle etait vide).
                log.info("%s %s: aucune preuve -> conviction annulee.", self.title, symbol)
                conviction = 0.0
        plaidoirie = {"camp": self.role, "conviction": conviction,
                      "these": str(data.get("these") or "").strip()[:800],
                      "arguments": arguments,
                      "risque_principal": str(data.get("risque_principal") or "").strip()[:300],
                      "refutation": str(data.get("refutation") or "").strip()[:500]}
        if ecartes:
            plaidoirie["ecartes_sans_preuve"] = ecartes[:5]
        return plaidoirie


class Bull(Debatteur):
    role, title, system = "bull", "Bull", BULL_SYSTEM


class Bear(Debatteur):
    role, title, system = "bear", "Bear", BEAR_SYSTEM


class Juge(DeskAgent):
    role, title, model_role = "juge", "Juge du debat", "debat"

    def trancher(self, symbol: str, briefs: dict, tours: list[dict], faits: P.Faits) -> dict:
        system = JUGE_SYSTEM.format(account_size=self.cfg.ftmo.account_size, symbol=symbol)
        human = "\n\n".join([
            "== BRIEFS DES ANALYSTES ==\n" + C.fmt(briefs),
            "== DEBAT (dans l'ordre) ==\n" + C.fmt(tours),
        ])
        data = self.ask_json(system, human + "\n\nRends UNIQUEMENT ton verdict JSON.")
        direction = str(data.get("direction") or "").strip().lower()
        if direction not in ("buy", "sell", "abstention"):
            # Verdict illisible : on ne devine pas une direction. L'abstention est le seul
            # defaut sur : elle ne peut pas faire perdre d'argent.
            log.info("Juge: verdict illisible sur %s -> abstention.", symbol)
            direction = "abstention"
        verdict = {"direction": direction, "conviction": _conviction(data),
                   "gagnant": str(data.get("gagnant") or "").strip().lower(),
                   "plan": str(data.get("plan") or "").strip()[:600],
                   "invalidation": str(data.get("invalidation") or "").strip()[:300],
                   "risques_non_resolus": _liste(data, "risques_non_resolus")}
        return self._exiger_preuves(symbol, verdict, tours, faits)

    def _exiger_preuves(self, symbol: str, verdict: dict, tours: list[dict],
                        faits: P.Faits) -> dict:
        """Un verdict qui engage de l'argent doit reposer sur des donnees, pas sur une
        conclusion bien tournee. Deux conditions, verifiees en dur :

          1. le debat a produit au moins UN argument sourcé (sinon il n'y a rien a juger) ;
          2. le plan retenu et son invalidation citent des donnees du dossier — une these
             qu'on ne peut pas invalider par un fait observable n'est pas une these.

        A defaut : ABSTENTION. Ce sens unique est voulu — le doute n'a pas besoin de preuve,
        la prise de risque, si. On garde la trace (`preuves`, `raison_abstention`) pour que
        la decision reste auditable."""
        if verdict["direction"] == "abstention" or not self.cfg.desk.exiger_preuves:
            return verdict
        citations = P.citations(verdict["plan"] + " " + verdict["invalidation"], faits)
        if not any(t.get("arguments") for t in tours):
            motif = "aucune plaidoirie n'a produit d'argument sourcé"
        elif not citations:
            motif = "le plan et l'invalidation ne citent aucune donnee du dossier"
        else:
            verdict["preuves"] = citations[:5]
            return verdict
        log.info("Juge %s: verdict '%s' non fonde (%s) -> abstention.",
                 symbol, verdict["direction"], motif)
        return {**verdict, "direction": "abstention", "conviction": 0.0,
                "raison_abstention": motif}


class Debat:
    """Organise le debat sur chaque candidat et rend {symbole: {bull, bear, verdict, tours}}."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.bull = Bull(cfg)
        self.bear = Bear(cfg)
        self.juge = Juge(cfg)

    def sur(self, symbols: list[str], briefs: dict, mandate: dict) -> dict:
        if not symbols:
            return {}
        out = {}
        for symbol in symbols:
            try:
                resultat = self._un_symbole(symbol, briefs.get(symbol) or {}, mandate)
            except DeskUnavailable as e:
                # Jamais de plaidoirie sans contradicteur, ni de debat sans juge.
                log.warning("Debat sur %s abandonne (%s) — le Trader decidera sans lui.",
                            symbol, e)
                continue
            out[symbol] = resultat
            v = resultat["verdict"]
            # thèses des deux camps AVANT le verdict : on veut voir sur quoi Bull et Bear
            # tranchent, pas seulement la conclusion du juge.
            bull, bear = resultat["bull"], resultat["bear"]
            log.info("  BULL [%.2f] %s", bull["conviction"],
                     str(bull.get("these") or "").strip()[:220])
            log.info("  BEAR [%.2f] %s", bear["conviction"],
                     str(bear.get("these") or "").strip()[:220])
            log.info("Debat %s: %s (conviction %.2f, %d tour(s)) | %s", symbol,
                     v["direction"], v["conviction"], resultat["tours"], v["plan"][:160])
        return out

    def _faits(self, symbol: str, briefs: dict) -> P.Faits:
        """Ce que le dossier autorise a affirmer sur ce symbole.

        On agrege le MARCHE (scan, chart, couts, news du symbole) et les BRIEFS des
        analystes : un chiffre repris d'un brief reste une observation — les briefs ont
        deja subi le meme filtre en amont. Si le contexte de cycle est indisponible
        (rejeu hors-ligne), on retombe sur le seul scan : moins de matiere, donc un
        filtre plus severe, jamais plus laxiste."""
        ctx = C.read()
        try:
            dossier = C.symbol_dossier(symbol, ctx, C.live())
        except Exception:                    # source marche indisponible : on n'invente rien
            dossier = (ctx.get("snapshots") or {}).get(symbol) or {}
        return P.faits(dossier, briefs)

    def _un_symbole(self, symbol: str, briefs: dict, mandate: dict) -> dict:
        faits = self._faits(symbol, briefs)
        bull = self.bull.plaide(symbol, briefs, mandate, None, faits)
        bear = self.bear.plaide(symbol, briefs, mandate, bull, faits)
        # ECART DU TOUR 1, capture AVANT que la boucle n'ecrase bull/bear. C'est le seul
        # chiffre qui dit si le seuil DESK_DEBATE_GAP est bien place : sans lui, on ne peut
        # regler le seuil que sur du ressenti. On le compare plus tard a l'ecart final pour
        # savoir si un second tour a vraiment SEPARE les camps ou n'a rien apporte.
        gap_initial = round(abs(bull["conviction"] - bear["conviction"]), 2)
        tours = [bull, bear]
        n = 1
        while n < max(1, self.cfg.desk.debate_rounds) and self._serre(bull, bear):
            # Debat serre : la contradiction a encore quelque chose a apprendre.
            n += 1
            log.info("Debat %s serre (%.2f vs %.2f) -> tour %d.", symbol,
                     bull["conviction"], bear["conviction"], n)
            bull = self.bull.plaide(symbol, briefs, mandate, bear, faits)
            bear = self.bear.plaide(symbol, briefs, mandate, bull, faits)
            tours += [bull, bear]
        verdict = self.juge.trancher(symbol, briefs, tours, faits)
        mesure = {"gap_initial": gap_initial,
                  "gap_final": round(abs(bull["conviction"] - bear["conviction"]), 2),
                  "tours": n}
        return {"bull": bull, "bear": bear, "tours": n, "verdict": verdict, "mesure": mesure}

    def _serre(self, bull: dict, bear: dict) -> bool:
        return abs(bull["conviction"] - bear["conviction"]) <= self.cfg.desk.debate_gap
