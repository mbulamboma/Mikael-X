# -*- coding: utf-8 -*-
"""LE RISK MANAGER — il controle l'EXPOSITION avant l'execution. Expert FTMO + risk mgmt.

Il passe en revue les ouvertures proposees par le Trader et rend un verdict par trade :
  - "approve" : rien a redire (le moteur FTMO deterministe validera quand meme) ;
  - "reduce"  : trade acceptable mais taille a reduire -> `risk_pct` (<= budget cfg) ;
  - "reject"  : refuse (correlation, sur-concentration, news, mauvais moment, slot manquant).

PRINCIPE : il ne peut que DURCIR. Un "reduce" plafonne le risque/trade transmis au moteur ;
le moteur reste libre d'aller encore plus bas, jamais au-dessus. Il ne remplace PAS le moteur
deterministe (risk/ftmo.py) : il ajoute un jugement humain-expert par-dessus.

SECURITE : si le Risk Manager est injoignable, on N'OUVRE RIEN ce cycle (pas de prise de
risque sans officier de risque) — mais le book reste gere (pas de bascule pilote).
"""
from __future__ import annotations

import logging

from risk.ftmo import currencies_of
from desk.base import DeskAgent
from desk import context as C

log = logging.getLogger("desk.risk_manager")

SYSTEM = """Tu es le RISK MANAGER d'une entreprise de trading FTMO (compte {account_size:.0f}
USD, ETAPE {phase}). Tu es EXPERT du risk management et de TOUS les criteres FTMO. Ton unique
job : proteger le compte. Tu passes en revue les ouvertures que le Trader veut executer et tu
n'as le droit que de les DURCIR (refuser ou reduire), jamais de les agrandir.

REGLES FTMO A FAIRE RESPECTER (le moteur deterministe applique deja les chiffres durs ; toi,
tu apportes le jugement) :
- Perte journaliere max -{max_daily:.0f} % et perte totale max -{max_total:.0f} % : si la perte
  du jour deja approche {daily_stop:.0f} %, refuse tout nouveau risque.
- Risque <= {risk_pct:.1f} %/trade, {max_pos} positions simultanees max.
- CORRELATION : deux paires portant la meme devise (EURUSD long + GBPUSD long = pari USD)
  cumulent le risque. Si des candidats sont correles entre eux ou avec le book, garde le
  MEILLEUR et refuse (ou reduis) les autres. Ne laisse pas 3 trades devenir un seul gros pari.
- SLOTS : ne laisse pas passer plus d'ouvertures qu'il n'y a de place (positions ouvertes +
  proposees <= {max_pos}).
- NEWS : refuse une entree si une annonce a fort impact est imminente sur la devise.
- QUALITE : confiance faible + edge douteux -> reduis ou refuse.

Pour CHAQUE ouverture proposee, rends un verdict. "reduce" impose un `risk_pct` (entre 0.1 et
{risk_pct:.1f}). Sois strict mais pas paralysant : un bon setup non correle et dans le budget
merite "approve".

Reponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"verdicts": [
   {{"symbol": "EURUSD", "direction": "buy", "verdict": "approve", "reason": "non correle, budget ok"}},
   {{"symbol": "GBPUSD", "direction": "buy", "verdict": "reject",
     "reason": "correle a EURUSD long deja pris (pari USD double)"}},
   {{"symbol": "XAUUSD", "direction": "sell", "verdict": "reduce", "risk_pct": 0.5,
     "reason": "volatilite elevee, on entre plus petit"}}
 ]}}"""


class RiskManager(DeskAgent):
    role = "risk"
    title = "Risk Manager"

    def review(self, opens: list[dict], summary: dict) -> list[dict]:
        """Filtre/annote les ouvertures. Renvoie la liste des ouvertures RETENUES, avec un
        eventuel `risk_pct` de plafonnement. Peut lever DeskUnavailable (LLM injoignable)."""
        if not opens:
            return []
        ctx = C.read()
        f, ex = self.cfg.ftmo, self.cfg.execution
        system = SYSTEM.format(
            account_size=f.account_size, phase=f.phase, max_daily=f.max_daily_loss_pct,
            max_total=f.max_total_loss_pct, daily_stop=f.daily_stop_pct,
            risk_pct=f.risk_per_trade_pct, max_pos=f.max_open_positions)
        # on donne au Risk Manager les devises de chaque candidat (aide a la correlation)
        proposes = [{"symbol": a.get("symbol"), "direction": a.get("direction"),
                     "strategy": a.get("strategy"), "confidence": a.get("confidence"),
                     "devises": currencies_of(str(a.get("symbol", ""))),
                     "entry": a.get("entry"), "sl": a.get("sl"), "tp": a.get("tp")}
                    for a in opens]
        book = [{"symbol": p.get("symbol"), "direction": p.get("direction"),
                 "devises": currencies_of(str(p.get("symbol", "")))}
                for p in (ctx.get("positions") or [])]
        news = ctx.get("news") or {}
        dossier = "\n\n".join([
            "== COMPTE / FTMO ==\n" + C.fmt(summary),
            "== OUVERTURES PROPOSEES PAR LE TRADER ==\n" + C.fmt(proposes),
            "== POSITIONS DEJA OUVERTES (exposition existante) ==\n" + C.fmt(book),
            "== NEWS / BLACK-OUT ==\n" + C.fmt({"blackout": news.get("blackout", {})}
                                               if isinstance(news, dict) else {}),
        ])
        data = self.ask_json(system, dossier + "\n\nRends UNIQUEMENT les verdicts JSON.")
        return appliquer_verdicts(opens, data, self.cfg.ftmo.risk_per_trade_pct,
                                  qui=self.title)


def appliquer_verdicts(opens: list[dict], data: dict, max_risk_pct: float,
                       qui: str = "Risk Manager") -> list[dict]:
    """Applique des verdicts {approve|reduce|reject} a une liste d'ouvertures.

    Partage par le Risk Manager mono-voix ET par l'arbitrage du college de risque
    (Phase 3B) : un seul endroit ou le durcissement est traduit en actions, donc une
    seule regle a verifier — `reduce` PLAFONNE le risque (jamais au-dessus du budget),
    `reject` supprime, et une ouverture sans verdict passe au moteur FTMO comme avant.
    """
    verdicts = {}
    for v in (data.get("verdicts") or []):
        if not isinstance(v, dict):
            continue
        key = (str(v.get("symbol", "")).strip().upper(),
               str(v.get("direction", "")).strip().lower())
        verdicts[key] = v
    retenues = []
    for a in opens:
        key = (str(a.get("symbol", "")).strip().upper(),
               str(a.get("direction", "")).strip().lower())
        v = verdicts.get(key)
        if v is None:
            # Pas de verdict explicite : on laisse passer (le moteur FTMO tranche), on note.
            log.info("%s: pas de verdict pour %s -> moteur FTMO seul.", qui, key)
            retenues.append({**a, "verdict_risque": {"verdict": "sans_avis"}})
            continue
        verdict = str(v.get("verdict", "approve")).strip().lower()
        if verdict == "reject":
            log.info("%s REFUSE %s: %s", qui, key, v.get("reason", ""))
            continue
        trace = {"verdict": verdict, "reason": str(v.get("reason", ""))[:300]}
        if verdict == "reduce":
            cap = _clamp(v.get("risk_pct"), 0.1, max_risk_pct)
            if cap:
                a = {**a, "risk_pct": cap}
                trace["risk_pct"] = cap
                log.info("%s REDUIT %s a %.2f%%: %s", qui, key, cap, v.get("reason", ""))
        # trace conservee dans le DOSSIER DE DECISION du trade (attribution par rol)
        retenues.append({**a, "verdict_risque": trace})
    return retenues


def _clamp(x, lo: float, hi: float):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, v))
