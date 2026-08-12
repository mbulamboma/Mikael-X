# -*- coding: utf-8 -*-
"""LE DESK — orchestration de l'entreprise d'agents. Drop-in de brain.agent.TraderAgent.

Expose EXACTEMENT la meme interface que l'agent solo pour que run.Orchestrator ne change
presque pas : `decide(summary) -> list[actions]`, `reflect(trade, postmortem) -> str`,
attributs `degraded` / `last_error`. Tout l'aval (moteur FTMO deterministe, execution MT5,
notifications, journal, apprentissage) reste identique.

Enchainement d'un cycle (cf. ai-company/IMPLEMENTATION.md) :
  1. GERANT -> mandat (posture, convoquer le desk ?, candidats, consignes).       [critique]
  2. TRADE MANAGER -> gere le book ouvert (close/modify/trail).                    [critique]
  3. si le Gerant convoque ET les ouvertures sont possibles :
       4 ANALYSTES independants -> DEBAT Bull/Bear arbitre par un JUGE -> TRADER ->
       ouvertures (celles qui contredisent le juge sont supprimees) ;
       RISK MANAGER -> filtre/durcit les ouvertures.                          [non critique]
  + entre deux cycles : VIGIE -> GERANT -> session extraordinaire (`watch`, cf. run.py).
  4. actions = book + ouvertures retenues.

Politique de DEGRADATION (preserve le filet existant) :
  - Gerant ou Trade Manager injoignable (LLM down) -> `degraded=True`, retourne [] :
    l'orchestrateur bascule sur le pilote deterministe (SafePilot). Identique au solo.
  - Echec dans la chaine de recherche (Trader / Risk Manager / analystes / debat) ->
    PAS de panique : on garde la gestion du book, on saute simplement les ouvertures.
"""
from __future__ import annotations

import logging

from config import AgentConfig
from brain.memory import Memory
from desk.base import DeskAgent, DeskUnavailable
from desk import context as C
from desk import journal as J
from desk import situation as SIT
from desk import bilan_roles as BILAN
from desk.gerant import Gerant
from desk.trade_manager import TradeManager
from desk.trader import Trader
from desk.risk_manager import RiskManager
from desk.vigie import Vigie
from desk.analysts import Analystes
from desk.debat import Debat
from desk.risque import CollegeRisque

log = logging.getLogger("desk")


COACH_SYSTEM = """Tu es le COACH de l'entreprise de trading. On te donne un trade cloture (avec
strategie, regime, R:R PLANIFIE, R REELLEMENT encaisse, MFE/MAE, slippage/spread payes) puis le
BILAN CHIFFRE de tous les trades passes.

Ecris UNE lecon actionnable en 1-2 phrases (francais), fondee sur les CHIFFRES du trade et du
bilan, pas sur des generalites. Cherche la cause : stop dans le bruit ? sortie trop tot alors
que le MFE etait a +2R ? entree payee trop cher ? strategie inadaptee au regime ? sur-confiance ?
Interdits : repeter une lecon deja presente dans le bilan, les banalites, les conseils sans
chiffre. Formule un correctif concret et verifiable."""


ATTRIBUTION_SYSTEM = """Tu es le COACH d'une entreprise de trading. On te donne un trade
cloture avec son DOSSIER DE DECISION : le mandat du Gerant, les briefs des 4 analystes, le
debat Bull/Bear et le verdict du juge, l'avis du college du risque, la these du Trader — puis
le bilan chiffre de tous les trades passes.

Ton travail : dire QUI, dans cette chaine, a reellement pese sur le resultat, et ecrire a
chacun une lecon qu'il pourra relire la prochaine fois.

Regles, importantes :
- **Un trade perdant n'est pas forcement une erreur** et un trade gagnant pas forcement une
  reussite : juge le PROCESSUS, pas le resultat. Un stop touche sur une these correcte et un
  R:R sain n'appelle aucune lecon.
- N'accuse pas tout le monde : 0 a 3 lecons MAXIMUM, uniquement les rols dont la
  contribution a vraiment change l'issue. Zero lecon est une reponse valable.
- Chaque lecon est ADRESSEE a un rol, chiffree, et formule un correctif verifiable.
  Interdits : les banalites, les conseils sans chiffre, repeter une lecon deja au bilan.

Rols possibles : gerant, technique, fondamental, sentiment, actualite, bull, bear, juge,
trader, agressif, neutre, prudent, suivi.

On te donne aussi la REVUE DE PERFORMANCE de chaque employe (esperance selon qu'il etait
aligne ou non, par conviction, par verdict...). Appuie-toi dessus — mais si une ligne est
marquee ECHANTILLON INSUFFISANT, elle ne prouve RIEN : ne fonde pas une lecon dessus.

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{"synthese": "1-2 phrases: ce que cette cloture apprend a l'entreprise",
 "lecons": [{"role": "juge", "lecon": "correctif concret et chiffre"}]}"""


class TradingDesk:
    """Entreprise d'agents. Compatible avec run.Orchestrator (meme contrat que TraderAgent)."""

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.degraded = False
        self.last_error = ""
        self.mem = Memory()
        # employes du noyau (Phase 1) ; analystes/debat/vigie branches aux phases suivantes
        self.gerant = Gerant(cfg)
        self.trade_manager = TradeManager(cfg)
        self.trader = Trader(cfg)
        self.risk_manager = RiskManager(cfg)
        self.vigie = Vigie(cfg)
        self.analystes = Analystes(cfg)
        self.debat = Debat(cfg)
        self.college_risque = CollegeRisque(cfg)
        self._coach = DeskAgent(cfg)          # role generique -> modele partage (reflexion)
        self._last_mandate: dict = {}

    # ------------------------------------------------------------------ helpers lecons
    def _lessons(self, roles, symbols=None):
        watch = list(symbols or [])
        ctx = C.read()
        watch += [p.get("symbol") for p in (ctx.get("positions") or [])]
        return self.mem.relevant_lessons_text(symbols=[s for s in watch if s], roles=roles)

    # ------------------------------------------------------------------ decision cycle
    def decide(self, summary: dict) -> list[dict]:
        self.degraded = False
        self.last_error = ""
        ctx = C.read()
        positions = ctx.get("positions") or []
        actions: list[dict] = []

        # 1. GERANT : mandat du cycle (CRITIQUE)
        try:
            mandate = self.gerant.mandate(summary, self._lessons(["gerant"]), self._revue())
        except DeskUnavailable as e:
            return self._degrade(f"Gerant indisponible: {e}")
        self._last_mandate = mandate
        log.info("Gerant | posture=%s convoquer=%s candidats=%s | %s",
                 mandate.get("posture"), mandate.get("convoquer_desk"),
                 mandate.get("candidats"), (mandate.get("revue") or "")[:160])

        # 2. TRADE MANAGER : gestion du book ouvert (CRITIQUE si des positions existent)
        if positions:
            try:
                actions += self.trade_manager.manage(
                    summary, mandate.get("consignes", ""), self._lessons(["suivi"]))
            except DeskUnavailable as e:
                return self._degrade(f"Trade Manager indisponible: {e}")

        # 3. NOUVELLES ENTREES : seulement si le Gerant convoque ET les ouvertures sont
        #    deterministiquement possibles ce cycle (le gate FTMO reste l'autorite finale).
        ouvertures_bloquees = bool(summary.get("ouvertures_bloquees"))
        if mandate.get("convoquer_desk") and not ouvertures_bloquees:
            actions += self._research_and_open(summary, mandate)
        elif mandate.get("convoquer_desk") and ouvertures_bloquees:
            log.info("Desk non convoque : ouvertures bloquees par le gate FTMO (%s).",
                     summary.get("gate_raisons"))

        return actions

    def _research_and_open(self, summary: dict, mandate: dict) -> list[dict]:
        """Chaine de recherche -> ouvertures. NON critique : tout echec ici = 0 ouverture,
        jamais de bascule pilote (le book reste gere par le Trade Manager)."""
        candidats = mandate.get("candidats") or []
        try:
            # Phase 2 : 4 analystes independants par candidat. Phase 3 : debat Bull/Bear.
            briefs: dict = {}
            if self.cfg.desk.use_analysts:
                briefs = self.analystes.briefs(candidats, mandate, self._lessons)
            debate: dict = {}
            if self.cfg.desk.use_debate:
                debate = self.debat.sur(candidats, briefs, mandate, self._lessons)
            situations = self._situations(candidats, debate)
            memoire = {s: SIT.bloc_prompt(sig, self.mem.closed_trades())
                       for s, sig in situations.items()}
            opens = self.trader.decide(summary, mandate, briefs, debate,
                                       self._lessons(None), memoire)
            opens = self._appliquer_verdicts(opens, debate)
            if not opens:
                return []
            if self.cfg.desk.use_risk_debate:
                opens = self.college_risque.review(opens, summary, self.gerant, self._lessons)
            else:
                opens = self.risk_manager.review(opens, summary)
            log.info("Desk -> %d ouverture(s) retenue(s) apres controle du risque.", len(opens))
            return [self._attach_dossier(a, mandate,
                                         briefs.get(str(a.get("symbol", "")).upper()),
                                         debate.get(str(a.get("symbol", "")).upper()),
                                         situations.get(str(a.get("symbol", "")).upper()))
                    for a in opens]
        except DeskUnavailable as e:
            # LLM injoignable pendant la recherche : on n'ouvre rien, on ne panique pas.
            self.last_error = f"chaine de recherche indisponible: {e}"
            log.warning("Aucune ouverture ce cycle (%s) — le book reste gere.", e)
            return []

    @staticmethod
    def _appliquer_verdicts(opens: list[dict], debate: dict) -> list[dict]:
        """Le plan du JUGE s'impose au Trader — deterministiquement, pas par la priere.

        Un prompt qui dit « respecte le verdict » se fait ignorer tot ou tard. Ici, une
        ouverture qui contredit le juge (abstention prononcee, ou direction opposee) est
        SUPPRIMEE. Sans debat sur un symbole (debat coupe ou en panne), rien n'est filtre :
        c'est le Trader qui reste responsable, comme avant la Phase 3.
        """
        if not debate:
            return opens
        retenues = []
        for a in opens:
            v = (debate.get(str(a.get("symbol", "")).upper()) or {}).get("verdict")
            if not v:
                retenues.append(a)
                continue
            if v["direction"] == "abstention":
                log.info("Ouverture %s supprimee : le juge a conclu a l'abstention (%s).",
                         a.get("symbol"), v["plan"][:120])
                continue
            if v["direction"] != str(a.get("direction", "")).lower():
                log.info("Ouverture %s %s supprimee : le juge a tranche '%s'.",
                         a.get("symbol"), a.get("direction"), v["direction"])
                continue
            retenues.append(a)
        return retenues

    # ------------------------------------------------------------------ vigie & escalade
    def watch(self, alertes: list[dict], summary: dict) -> list[dict]:
        """BOUCLE D'ESCALADE, appelee par le watchdog entre deux cycles de decision.

        `alertes` = [{"position": <vue enrichie>, "declencheurs": ["perte flottante -0.8R"]}],
        deja filtrees par des seuils DETERMINISTES et rate-limitees par ticket (run.py).

        Chaine : VIGIE evalue le trade -> si "escalader", le GERANT decide s'il convoque une
        SESSION EXTRAORDINAIRE -> le TRADE MANAGER rend des actions sur CE ticket seulement.

        Ne rend QUE des actions de reduction de risque (close/modify/trail) : une session
        extraordinaire ne peut jamais ouvrir. Toute panne LLM interrompt la chaine sans
        rien casser — les protections deterministes du watchdog ont deja tourne.
        """
        if not self.cfg.desk.vigie_enabled or not alertes:
            return []
        actions: list[dict] = []
        for alerte in alertes:
            pos = alerte.get("position") or {}
            declencheurs = alerte.get("declencheurs") or []
            try:
                verdict = self.vigie.evaluate(pos, declencheurs, summary)
            except DeskUnavailable as e:
                log.warning("Vigie injoignable (%s) — protections deterministes seules.", e)
                return actions
            log.info("Vigie ticket %s: %s | %s", pos.get("ticket"), verdict["gravite"],
                     verdict["raison"][:160])
            if verdict["gravite"] != "escalader":
                continue
            try:
                decision = self.gerant.escalade(pos, verdict, declencheurs, summary)
            except DeskUnavailable as e:
                log.warning("Gerant injoignable pour l'escalade (%s) — pas de session.", e)
                return actions
            if not decision["session_extraordinaire"]:
                log.info("Gerant: pas de session extraordinaire sur %s (%s).",
                         pos.get("ticket"), decision["decision"][:160])
                continue
            log.warning("SESSION EXTRAORDINAIRE sur le ticket %s: %s",
                        pos.get("ticket"), decision["decision"][:200])
            actions += self._session_extraordinaire(pos, verdict, decision, summary)
        return actions

    def _session_extraordinaire(self, pos: dict, verdict: dict, decision: dict,
                                summary: dict) -> list[dict]:
        """Revue ciblee d'UNE position par le Trade Manager. On ne garde que ses actions
        sur le ticket vise : la session ne doit pas devenir une revue generale du book."""
        consignes = (f"SESSION EXTRAORDINAIRE sur le ticket {pos.get('ticket')} "
                     f"({pos.get('symbol')}). Alerte de la vigie : {verdict['raison']} "
                     f"Recommandation : {verdict['recommandation']} "
                     f"Consignes du DG : {decision['consignes']}")
        try:
            actions = self.trade_manager.manage(summary, consignes, self._lessons(["suivi"]))
        except DeskUnavailable as e:
            log.warning("Trade Manager injoignable pendant la session (%s) — rien fait.", e)
            return []
        ticket = pos.get("ticket")
        retenues = [a for a in actions if a.get("ticket") == ticket]
        if len(retenues) != len(actions):
            log.info("Session extraordinaire: %d action(s) hors ticket %s ignoree(s).",
                     len(actions) - len(retenues), ticket)
        return retenues

    def _revue(self) -> str:
        """Revue de performance par employe (cf. desk/bilan_roles.py). Le Gerant DIRIGE :
        il lui faut savoir qui apporte du signal, pas seulement ce que le compte a fait."""
        try:
            return BILAN.bloc_prompt(self.mem.closed_trades())
        except Exception as e:            # une statistique ne doit jamais bloquer un cycle
            log.warning("Revue de performance indisponible (%s).", e)
            return ""

    # ------------------------------------------------------------------ memoire situationnelle
    def _situations(self, candidats: list[str], debate: dict) -> dict:
        """Signature deterministe de la situation de chaque candidat (cf. desk/situation.py).

        La DIRECTION vient du verdict du juge quand il y en a un : « acheter EURUSD dans ce
        regime » et « vendre EURUSD dans ce regime » n'ont pas la meme histoire. Sans debat,
        on cherche sans direction plutot que d'en inventer une."""
        from strategy import regime as regime_mod
        ctx = C.read()
        snaps = ctx.get("snapshots") or {}
        news = ctx.get("news") or {}
        bo = (news.get("blackout") or {}) if isinstance(news, dict) else {}
        out = {}
        for sym in candidats:
            sym = str(sym).upper()
            snap = snaps.get(sym) or {}
            verdict = (debate.get(sym) or {}).get("verdict") or {}
            direction = verdict.get("direction") or ""
            out[sym] = SIT.signature(
                sym, snap, regime=regime_mod.classify(snap) if snap else "",
                direction=direction if direction in ("buy", "sell") else "",
                news_proche=bool((bo.get(sym) or {}).get("active")))
        return out

    # ------------------------------------------------------------------ dossier de decision
    @staticmethod
    def _attach_dossier(action: dict, mandate: dict, briefs: dict, debate: dict,
                        situation: dict | None = None) -> dict:
        """Attache a une ouverture la trace de QUI a decide quoi.

        L'orchestrateur la range dans le meta du ticket ; a la cloture elle revient avec le
        trade. Sans elle, on ne peut reprocher a personne son erreur : on sait qu'un trade a
        perdu, pas si c'est le mandat du Gerant, la these du Bull ou le laxisme du Risk
        Manager qui l'a produit. C'est le prerequis de l'attribution par rol (Phase 4).

        Volontairement COMPACT (champs tronques) : il est stocke par trade et relu par un LLM.
        """
        dossier = {
            "mandat": {"posture": mandate.get("posture"),
                       "revue": J.clip(mandate.get("revue", ""), 300),
                       "consignes": J.clip(mandate.get("consignes", ""), 300)},
            "trader": {"strategy": action.get("strategy"),
                       "confidence": action.get("confidence"),
                       "rationale": J.clip(action.get("rationale", ""), 400)},
            "risque": action.get("verdict_risque") or {},
        }
        if situation:
            # signature de la configuration au moment d'entrer : c'est elle qui permettra,
            # dans six mois, de retrouver « les fois ou le marche ressemblait a ca »
            dossier["situation"] = {**situation,
                                    "direction": situation.get("direction")
                                    or str(action.get("direction", "")).lower()}
        if briefs:                       # Phase 2 : {rol: brief} du symbole concerne
            dossier["briefs"] = {r: {"biais": b.get("biais"), "confiance": b.get("confiance"),
                                     "resume": J.clip(b.get("resume", ""), 220)}
                                 for r, b in briefs.items() if isinstance(b, dict)}
        if debate:                       # Phase 3 : debat + verdict du juge sur ce symbole
            verdict = debate.get("verdict") or {}
            dossier["debat"] = {
                "tours": debate.get("tours"),
                "conviction_bull": (debate.get("bull") or {}).get("conviction"),
                "conviction_bear": (debate.get("bear") or {}).get("conviction"),
                "verdict": {"direction": verdict.get("direction"),
                            "conviction": verdict.get("conviction"),
                            "gagnant": verdict.get("gagnant"),
                            "plan": J.clip(verdict.get("plan", ""), 300),
                            "invalidation": J.clip(verdict.get("invalidation", ""), 200)}}
        return {**action, "dossier": dossier}

    def _degrade(self, why: str) -> list[dict]:
        self.degraded = True
        self.last_error = why
        log.error("DESK DEGRADE -> pilote deterministe. %s", why)
        return []

    # ------------------------------------------------------------------ apprentissage
    def reflect(self, trade: dict, postmortem: str = "") -> str:
        """Leçon de clôture. Meme contrat que TraderAgent.reflect (rend UNE chaine, que
        l'orchestrateur enregistre comme lecon generale).

        En plus du contrat, quand le trade porte son DOSSIER DE DECISION (Phase 1C), on
        fait l'ATTRIBUTION PAR ROLE : le coach dit qui, dans la chaine, a reellement pese
        sur le resultat, et chaque lecon est ecrite TAGUEE de ce rol. C'est ce qui permet
        au Bull de relire ses erreurs de Bull, et au juge les siennes — au lieu d'une
        soupe de conseils que tout le monde ignore.
        """
        import json
        dossier = trade.get("dossier") or {}
        human = ("Trade cloture :\n" + json.dumps(trade, ensure_ascii=False, default=str)
                 + (f"\n\nBILAN CHIFFRE ACTUEL :\n{postmortem}" if postmortem else ""))
        if not dossier:
            # trade d'avant la Phase 1C, ou ouvert par l'agent solo : pas de chaine a juger
            try:
                return self._coach.ask_text(COACH_SYSTEM, human)
            except DeskUnavailable as e:
                log.warning("Reflexion desk impossible (%s) — lecon generique.", e)
                return self._fallback_reflect(trade)
        try:
            data = self._coach.ask_json(
                ATTRIBUTION_SYSTEM,
                human + "\n\nREVUE DE PERFORMANCE DES EMPLOYES :\n" + self._revue()
                + "\n\nRends UNIQUEMENT ton analyse JSON.")
        except DeskUnavailable as e:
            log.warning("Attribution impossible (%s) — lecon generique.", e)
            return self._fallback_reflect(trade)
        return self._enregistrer_attribution(trade, data)

    def _enregistrer_attribution(self, trade: dict, data: dict) -> str:
        """Ecrit les lecons par rol et rend la synthese (lecon generale de l'orchestrateur)."""
        symbole = trade.get("symbol", "?")
        outcome = "win" if (trade.get("R") or 0) > 0 else "loss"
        base_tags = [str(trade.get("strategy") or ""), str(trade.get("regime") or "")]
        ecrites = 0
        for item in (data.get("lecons") or [])[:3]:      # 3 max : on ne blame pas tout le monde
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            texte = str(item.get("lecon") or "").strip()
            if role not in SIT.ROLES_CONNUS or not texte:
                continue
            self.mem.add_lesson(symbole, outcome, texte, tags=[role] + base_tags)
            log.info("Lecon [%s]: %s", role, texte[:200])
            ecrites += 1
        synthese = str(data.get("synthese") or "").strip()
        if not synthese:
            synthese = self._fallback_reflect(trade)
        log.info("Attribution: %d lecon(s) par rol ecrite(s).", ecrites)
        return synthese

    @staticmethod
    def _fallback_reflect(trade: dict) -> str:
        strat = trade.get("strategy", "unknown")
        result = trade.get("result")
        if result == "tp":
            return f"{strat}: conserver la discipline, ne pas forcer le risque apres un gain."
        if result == "sl":
            return f"{strat}: reevaluer l'adaptation au regime avant de retenter ce setup."
        return f"{strat}: rester selectif, ne pas trader par impatience."
