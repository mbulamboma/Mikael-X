# -*- coding: utf-8 -*-
"""LE DESK — orchestration de l'entreprise d'agents. Drop-in de brain.agent.TraderAgent.

Expose EXACTEMENT la meme interface que l'agent solo pour que run.Orchestrator ne change
presque pas : `decide(summary) -> list[actions]`, attributs `degraded` / `last_error`.
Tout l'aval (moteur FTMO deterministe, execution MT5, notifications, journal) reste
identique.

Enchainement d'un cycle (cf. ai-company/IMPLEMENTATION.md) :
  1. GERANT -> mandat (posture, convoquer le desk ?, candidats, consignes).       [critique]
  2. TRADE MANAGER -> gere le book ouvert (close/modify/trail).                    [critique]
  3. si le Gerant convoque ET les ouvertures sont possibles :
       4 ANALYSTES independants -> DEBAT Bull/Bear arbitre par un JUGE -> TRADER ->
       ouvertures (celles qui contredisent le juge OU qui ne citent aucune donnee du
       dossier sont supprimees) ;
       RISK MANAGER -> filtre/durcit les ouvertures.                          [non critique]
  + entre deux cycles : VIGIE -> GERANT -> session extraordinaire (`watch`, cf. run.py).
  4. actions = book + ouvertures retenues.

CE QUE LE DESK NE FAIT PLUS : il n'ecrit plus de "lecons" apres une cloture et n'en relit
plus. Ce que les employes savent du passe vient de faits calcules — bilan chiffre
(brain/postmortem.py), revue de performance par employe (desk/bilan_roles.py), cas passes
comparables (desk/situation.py). En echange, ce qu'ils AFFIRMENT est verifie : le filtre de
preuves (desk/preuves.py) retire toute affirmation qui ne renvoie a aucune donnee du dossier,
et une ouverture non sourcee est supprimee avant meme le controle du risque.

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
from desk.base import DeskUnavailable
from desk import context as C
from desk import preuves as P
from desk import journal as J
from desk import situation as SIT
from desk import bilan_roles as BILAN
from desk import trace
from desk import reflexion as R
from desk.checkpoint import Checkpoint, signature as ckpt_sig
from desk.gerant import Gerant
from desk.trade_manager import TradeManager
from desk.trader import Trader
from desk.risk_manager import RiskManager
from desk.vigie import Vigie
from desk.analysts import Analystes
from desk.debat import Debat
from desk.risque import CollegeRisque

log = logging.getLogger("desk")


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
        self._last_mandate: dict = {}
        self.last_debate: dict = {}          # dernier debat du cycle, expose au journal
        trace.configure(cfg.desk.trace_enabled)
        self._ckpt: Checkpoint | None = None

    # ------------------------------------------------------------------ decision cycle
    def decide(self, summary: dict) -> list[dict]:
        cycle_id = trace.new_cycle_id()
        token = trace.start_cycle(cycle_id)
        try:
            return self._decide(summary)
        finally:
            if self.cfg.desk.trace_enabled:
                log.info("Trace cycle %s: %s", cycle_id, trace.summary(cycle_id))
            trace.end_cycle(token)

    def _decide(self, summary: dict) -> list[dict]:
        self.degraded = False
        self.last_error = ""
        ctx = C.read()
        positions = ctx.get("positions") or []
        actions: list[dict] = []

        # CHECKPOINT : filet anti-crash indexe sur une signature stable du cycle. Un
        # redemarrage immediat sur le meme etat reprend les phases deja calculees.
        acc = (summary.get("account") or {}) if isinstance(summary, dict) else {}
        self._ckpt = Checkpoint(
            ckpt_sig(str(summary.get("jour") or summary.get("server_day") or ""),
                     C.candidate_symbols(ctx), float(acc.get("equity") or 0.0)),
            enabled=self.cfg.desk.checkpoint_enabled)

        # 1. GERANT : mandat du cycle (CRITIQUE). Repris du checkpoint si un crash a suivi.
        mandate = self._ckpt.get("mandate")
        if mandate is None:
            try:
                mandate = self.gerant.mandate(summary, self._revue())
            except DeskUnavailable as e:
                return self._degrade(f"Gerant indisponible: {e}")
            self._ckpt.set("mandate", mandate)
        self._last_mandate = mandate
        log.info("Gerant | posture=%s convoquer=%s candidats=%s | %s",
                 mandate.get("posture"), mandate.get("convoquer_desk"),
                 mandate.get("candidats"), (mandate.get("revue") or "")[:160])

        # 2. TRADE MANAGER : gestion du book ouvert (CRITIQUE si des positions existent)
        if positions:
            try:
                actions += self.trade_manager.manage(
                    summary, mandate.get("consignes", ""))
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

        # cycle abouti : le filet anti-crash n'a plus lieu d'etre.
        if self._ckpt is not None:
            self._ckpt.clear()
        return actions

    def _research_and_open(self, summary: dict, mandate: dict) -> list[dict]:
        """Chaine de recherche -> ouvertures. NON critique : tout echec ici = 0 ouverture,
        jamais de bascule pilote (le book reste gere par le Trade Manager)."""
        candidats = mandate.get("candidats") or []
        ckpt = self._ckpt
        try:
            # Phase 2 : 4 analystes independants par candidat. Phase 3 : debat Bull/Bear.
            # Chaque phase couteuse est mise sous checkpoint : un crash apres le debat ne
            # refait pas les 4 analystes x N candidats au redemarrage.
            briefs: dict = (ckpt.get("briefs") if ckpt else None) or {}
            if not briefs and self.cfg.desk.use_analysts:
                briefs = self.analystes.briefs(candidats, mandate)
                if ckpt:
                    ckpt.set("briefs", briefs)
            debate: dict = (ckpt.get("debate") if ckpt else None) or {}
            if not debate and self.cfg.desk.use_debate:
                debate = self.debat.sur(candidats, briefs, mandate)
                if ckpt:
                    ckpt.set("debate", debate)
            # expose au journal l'issue de CHAQUE debat du cycle, abstentions comprises
            # (elles ne deviennent jamais un trade -> sinon aucune trace). Cf. run._journal_cycle.
            self.last_debate = debate
            situations = self._situations(candidats, debate)
            trades = self.mem.closed_trades()
            reflexions = self.mem.reflexions() if self.cfg.desk.reflexion_enabled else []
            # MEMOIRE SITUATIONNELLE : cas passes comparables (stats) + (Point 3) la note
            # QUALITATIVE des reflexions les plus similaires. Les deux sont sourcees.
            memoire = {}
            for s, sig in situations.items():
                bloc = SIT.bloc_prompt(sig, trades)
                if reflexions:
                    refl = R.bloc_prompt(sig, reflexions, self.cfg.desk.reflexion_k)
                    if refl:
                        bloc = bloc + "\n" + refl
                memoire[s] = bloc
            opens = self.trader.decide(summary, mandate, briefs, debate, memoire)
            opens = self._appliquer_verdicts(opens, debate)
            opens = self._exiger_preuves(opens, briefs)
            if not opens:
                return []
            if self.cfg.desk.use_risk_debate:
                opens = self.college_risque.review(opens, summary, self.gerant)
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

    def _exiger_preuves(self, opens: list[dict], briefs: dict) -> list[dict]:
        """DERNIER FILTRE AVANT LE RISQUE : une ouverture dont la these ne cite aucune
        donnee du dossier est supprimee.

        Les niveaux (entry/sl/tp) sont deja verifies par ailleurs — ce qu'on controle ici
        est le RAISONNEMENT : `rationale` doit renvoyer a quelque chose d'observable (un
        niveau du chart, un ATR, un ecart de taux, une date d'annonce), pas a une
        anticipation. Une decision de trader qu'on ne peut rattacher a aucun fait n'est pas
        une decision, c'est un pari — et le desk n'en prend pas.

        Le sens du filtre est unique : il ne peut que RETIRER des ouvertures."""
        if not opens or not self.cfg.desk.exiger_preuves:
            return opens
        ctx = C.read()
        retenues = []
        for a in opens:
            sym = str(a.get("symbol", "")).upper()
            try:
                dossier = C.symbol_dossier(sym, ctx, C.live())
            except Exception:               # source indisponible : on ne relaxe pas le filtre
                dossier = (ctx.get("snapshots") or {}).get(sym) or {}
            faits = P.faits(dossier, briefs.get(sym) or {})
            citations = P.citations(str(a.get("rationale") or ""), faits)
            if not citations:
                log.info("Ouverture %s supprimee : these non sourcee (%s).",
                         sym, str(a.get("rationale") or "")[:120])
                continue
            retenues.append({**a, "preuves": citations[:5]})
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
            actions = self.trade_manager.manage(summary, consignes)
        except DeskUnavailable as e:
            log.warning("Trade Manager injoignable pendant la session (%s) — rien fait.", e)
            return []
        ticket = pos.get("ticket")
        retenues = [a for a in actions if a.get("ticket") == ticket]
        if len(retenues) != len(actions):
            log.info("Session extraordinaire: %d action(s) hors ticket %s ignoree(s).",
                     len(actions) - len(retenues), ticket)
        return retenues

    # ------------------------------------------------------------------ reflexion a la cloture
    def reflect_on_close(self, trade: dict) -> dict | None:
        """POINT 3 (cote ecriture) : a la cloture d'un trade, ecrit une note factuelle bornee
        et la journalise (kind `reflexion`). Appelee par l'orchestrateur apres `trade_closed`.

        Ne leve jamais et ne bloque jamais la cloture : toute panne LLM retombe sur une note
        deterministe (cf. desk/reflexion.py). Rend l'enregistrement ecrit, ou None si desactive."""
        if not self.cfg.desk.reflexion_enabled:
            return None
        try:
            record = R.ecrire(self.cfg, trade)
            self.mem.log_event("reflexion", record)
            log.info("Reflexion %s R=%s: %s", trade.get("symbol"), record.get("R"),
                     (record.get("note") or "")[:160])
            return record
        except Exception as e:                   # une note ne doit jamais casser une cloture
            log.warning("Reflexion non ecrite pour %s (%s).", trade.get("symbol"), e)
            return None

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
                       "rationale": J.clip(action.get("rationale", ""), 400),
                       # donnees du dossier que la these citait : c'est ce qui distingue,
                       # a la relecture, une decision fondee d'une intuition bien ecrite
                       "preuves": action.get("preuves") or []},
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
                "mesure": debate.get("mesure") or {},
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

