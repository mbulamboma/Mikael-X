# -*- coding: utf-8 -*-
"""LE COLLEGE DU RISQUE — trois temperaments, un arbitre.

Un officier de risque unique a un defaut structurel : son humeur devient la politique de
la maison. Un modele prudent ce jour-la refuse tout ; un modele complaisant laisse tout
passer. On remplace donc la voix unique par trois voix qui ne peuvent pas etre d'accord
par construction — AGRESSIF, NEUTRE, PRUDENT — puis le GERANT arbitre.

Ordre de passage : le Prudent parle en DERNIER, en voyant les deux autres. C'est lui qui
doit pouvoir dire « vous avez tous les deux oublie que ces deux paires sont le meme pari ».

DEUX GARDE-FOUS DETERMINISTES, qui ne dependent d'aucun LLM :
  - UNANIMITE NEGATIVE : si les trois refusent un trade, il est supprime sans meme
    deranger le Gerant (economie d'un appel, et suppression d'un mode de defaillance) ;
  - PLANCHER INTACT : l'arbitrage passe par `appliquer_verdicts`, donc `reduce` PLAFONNE
    le risque et ne peut jamais l'augmenter ; ensuite le moteur FTMO deterministe
    (`risk/ftmo.py`) valide et dimensionne comme avant. Le college est un JUGEMENT
    au-dessus du plancher, jamais a sa place.

PANNE : toute indisponibilite dans le college ou a l'arbitrage leve `DeskUnavailable`
jusqu'a `TradingDesk` — qui n'ouvre alors rien ce cycle (pas de prise de risque sans
officier de risque), tout en continuant a gerer le book. Contrat inchange depuis la Phase 1.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from risk.ftmo import currencies_of
from desk.base import DeskAgent
from desk import context as C
from desk.risk_manager import appliquer_verdicts

log = logging.getLogger("desk.risque")

_COMMUN = """Entreprise de trading FTMO (compte {account_size:.0f} USD, ETAPE {phase}).
Limites FATALES : -{max_daily:.0f} %/jour, -{max_total:.0f} % total. Budget : risque
<= {risk_pct:.1f} %/trade, {max_pos} positions simultanees au maximum.

Tu sieges au COLLEGE DU RISQUE. Le Trader propose des ouvertures ; tu donnes ton avis sur
CHACUNE. Tu ne decides pas seul : le Directeur General arbitrera entre les trois membres du
college. Defends ton point de vue avec des chiffres, sans caricature.

Rappels que personne ne doit oublier : deux paires partageant une devise cumulent le risque
(EURUSD long + GBPUSD long = un seul pari contre le dollar) ; une annonce a fort impact
imminente change la nature du risque ; le nombre de places est limite.

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"avis": [
   {{"symbol": "EURUSD", "direction": "buy", "avis": "approve|reduce|reject",
     "risk_pct": 0.5, "raison": "1 phrase chiffree"}}
 ]}}
`risk_pct` n'a de sens que pour "reduce" (entre 0.1 et {risk_pct:.1f})."""

AGRESSIF = _COMMUN + """

TU ES LE MEMBRE AGRESSIF. Ton angle : un compte qui ne prend jamais de risque ne passe
jamais le challenge. Une occasion nette avec un edge documente merite d'etre prise a taille
pleine. Tu combats la sur-prudence qui fait rater l'objectif — mais tu ne defends JAMAIS un
setup sans preuve chiffree, et tu n'inventes pas d'edge."""

NEUTRE = _COMMUN + """

TU ES LE MEMBRE NEUTRE. Ton angle : le rapport rendement/risque, froidement. Ni peur ni
appat du gain. Tu regardes l'esperance du setup, son cout reel (spread, commission, swap),
sa place dans le portefeuille existant, et tu dis ce qu'un gestionnaire raisonnable ferait."""

PRUDENT = _COMMUN + """

TU ES LE MEMBRE PRUDENT. Ton angle : la survie du compte passe avant tout gain. Tu parles en
DERNIER et tu vois les avis de tes deux collegues : ton travail est de reperer ce qu'ils ont
sous-estime — correlation cachee entre les candidats, concentration, annonce imminente,
perte du jour deja entamee, taille qui ne laisserait pas de seconde chance. Tu peux etre
minoritaire : dis-le clairement, le DG tranchera."""

ARBITRAGE_SYSTEM = """Tu es le DIRECTEUR GENERAL d'une entreprise de trading FTMO (compte
{account_size:.0f} USD, ETAPE {phase}). Ton COLLEGE DU RISQUE (agressif, neutre, prudent)
vient de rendre trois avis sur les ouvertures proposees par le Trader. Tu ARBITRES.

Regles de ton arbitrage :
- tu ne peux que DURCIR ce que propose le Trader : approuver, reduire la taille, ou refuser.
  Tu ne peux jamais augmenter le risque ni depasser {risk_pct:.1f} %/trade ;
- pese les ARGUMENTS, pas les temperaments : l'agressif a raison quand l'edge est documente,
  le prudent a raison quand le risque est structurel (correlation, news, perte du jour) ;
- un desaccord franc sur un trade est un signal en soi : dans le doute, REDUIS plutot que de
  trancher au hasard ;
- la preservation du capital prime sur l'objectif. Refuser n'a jamais fait perdre un compte.

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"verdicts": [
   {{"symbol": "EURUSD", "direction": "buy", "verdict": "approve|reduce|reject",
     "risk_pct": 0.5, "reason": "ton arbitrage en 1 phrase"}}
 ],
 "synthese": "1-2 phrases sur ce que le college a fait ressortir"}}"""


class MembreRisque(DeskAgent):
    """Un temperament du college. Meme dossier pour tous : seul l'angle change."""
    model_role = "risk"
    system = ""

    def avis(self, proposes: list[dict], book: list[dict], summary: dict,
             blackout: dict, autres: Optional[dict]) -> list[dict]:
        f, cfg = self.cfg.ftmo, self.cfg
        system = self.system.format(
            account_size=f.account_size, phase=f.phase, max_daily=f.max_daily_loss_pct,
            max_total=f.max_total_loss_pct, risk_pct=f.risk_per_trade_pct,
            max_pos=f.max_open_positions)
        blocs = [
            "== COMPTE / FTMO ==\n" + C.fmt(summary),
            "== OUVERTURES PROPOSEES PAR LE TRADER ==\n" + C.fmt(proposes),
            "== POSITIONS DEJA OUVERTES (exposition existante) ==\n" + C.fmt(book),
            "== NEWS / BLACK-OUT ==\n" + C.fmt(blackout),
        ]
        if autres:
            blocs.append("== AVIS DEJA RENDUS PAR TES COLLEGUES ==\n" + C.fmt(autres))
        data = self.ask_json(system, "\n\n".join(blocs) + "\n\nRends UNIQUEMENT tes avis JSON.")
        return _normalise_avis(data, cfg.ftmo.risk_per_trade_pct)


class MembreAgressif(MembreRisque):
    role, title, system = "agressif", "Risque AGRESSIF", AGRESSIF


class MembreNeutre(MembreRisque):
    role, title, system = "neutre", "Risque NEUTRE", NEUTRE


class MembrePrudent(MembreRisque):
    role, title, system = "prudent", "Risque PRUDENT", PRUDENT


def _normalise_avis(data: dict, max_risk_pct: float) -> list[dict]:
    out = []
    for a in (data.get("avis") or []):
        if not isinstance(a, dict):
            continue
        avis = str(a.get("avis") or "").strip().lower()
        if avis not in ("approve", "reduce", "reject"):
            avis = "approve"          # avis illisible : neutre, l'arbitrage tranchera
        ligne = {"symbol": str(a.get("symbol", "")).strip().upper(),
                 "direction": str(a.get("direction", "")).strip().lower(),
                 "avis": avis, "raison": str(a.get("raison") or "").strip()[:300]}
        if avis == "reduce":
            try:
                ligne["risk_pct"] = round(min(max_risk_pct, max(0.1, float(a.get("risk_pct")))), 2)
            except (TypeError, ValueError):
                pass
        out.append(ligne)
    return out


class CollegeRisque:
    """Les trois temperaments + l'arbitrage du Gerant."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.membres = [MembreAgressif(cfg), MembreNeutre(cfg), MembrePrudent(cfg)]

    def review(self, opens: list[dict], summary: dict, gerant) -> list[dict]:
        """Renvoie les ouvertures RETENUES (avec leur eventuel plafond `risk_pct`).
        Peut lever DeskUnavailable : sans officier de risque, on n'ouvre rien."""
        if not opens:
            return []
        ctx = C.read()
        proposes = [{"symbol": a.get("symbol"), "direction": a.get("direction"),
                     "strategy": a.get("strategy"), "confidence": a.get("confidence"),
                     "devises": currencies_of(str(a.get("symbol", ""))),
                     "entry": a.get("entry"), "sl": a.get("sl"), "tp": a.get("tp")}
                    for a in opens]
        book = [{"symbol": p.get("symbol"), "direction": p.get("direction"),
                 "devises": currencies_of(str(p.get("symbol", "")))}
                for p in (ctx.get("positions") or [])]
        news = ctx.get("news") or {}
        blackout = {"blackout": news.get("blackout", {})} if isinstance(news, dict) else {}

        avis: dict[str, list[dict]] = {}
        for membre in self.membres:
            # le Prudent (dernier) voit les avis deja rendus : il doit pouvoir les contredire
            avis[membre.role] = membre.avis(proposes, book, summary, blackout,
                                            dict(avis) or None)
            log.info("College %s: %s", membre.title,
                     [f"{x['symbol']}:{x['avis']}" for x in avis[membre.role]])

        opens, unanimes = self._unanimite_negative(opens, avis)
        if unanimes:
            log.info("College: %d ouverture(s) refusee(s) a l'unanimite (sans arbitrage).",
                     unanimes)
        if not opens:
            return []
        data = gerant.arbitrer_risque(proposes, avis, summary)
        retenues = appliquer_verdicts(opens, data, self.cfg.ftmo.risk_per_trade_pct,
                                      qui="Arbitrage DG")
        return [{**a, "verdict_risque": {**(a.get("verdict_risque") or {}),
                                         "college": _resume_avis(a, avis)}}
                for a in retenues]

    @staticmethod
    def _unanimite_negative(opens: list[dict], avis: dict) -> tuple[list[dict], int]:
        """Trois refus = refus. Deterministe : aucun arbitrage ne peut le renverser."""
        garde, refuses = [], 0
        for a in opens:
            cle = (str(a.get("symbol", "")).upper(), str(a.get("direction", "")).lower())
            votes = [_vote(avis.get(r) or [], cle) for r in ("agressif", "neutre", "prudent")]
            if votes and all(v == "reject" for v in votes):
                log.info("College UNANIME contre %s — supprime sans arbitrage.", cle)
                refuses += 1
                continue
            garde.append(a)
        return garde, refuses


def _vote(lignes: list[dict], cle: tuple[str, str]) -> str:
    for l in lignes:
        if (l.get("symbol"), l.get("direction")) == cle:
            return l.get("avis", "")
    return ""


def _resume_avis(action: dict, avis: dict) -> dict:
    """Trace compacte du college pour le dossier de decision du trade."""
    cle = (str(action.get("symbol", "")).upper(), str(action.get("direction", "")).lower())
    return {role: _vote(lignes, cle) or "sans_avis" for role, lignes in avis.items()}
