# -*- coding: utf-8 -*-
"""REJEU & MESURE — repondre a « ce cerveau fait-il vraiment mieux ? » avec des chiffres.

Empiler des employes LLM augmente le cout et la variance ; rien ne garantit que ca
augmente la qualite. Ce module fournit l'instrument de mesure qui manquait :

  1. `simulate_opens()` — pour chaque OUVERTURE proposee (journalisee par
     `desk/journal.py`), on deroule les bougies POSTERIEURES a la decision et on
     regarde ce qui a ete touche en premier, le stop ou la cible -> R realise.
  2. `metrics()` — expectancy, winrate, profit factor, drawdown en R.
  3. `compare()` — propositions du cerveau (mode ombre inclus) vs trades REELLEMENT
     executes : ce que le veto du moteur FTMO a evite... ou coute.
  4. `replay_cycles()` — re-decider sur des dossiers deja archives (couteux : rappelle
     le LLM) pour comparer solo vs desk sur EXACTEMENT les memes donnees.

CE QUE CETTE MESURE N'EST PAS — a garder en tete avant d'en tirer une conclusion :
  - pas de frais : spread, commission et swap ne sont pas deduits (utiliser
    `cout_R` pour en provisionner une louche) ;
  - le chemin INTRA-bougie est inconnu : si stop et cible sont touches dans la meme
    bougie, on tranche pour le STOP (hypothese pessimiste, jamais flatteuse) ;
  - pas de sizing, pas de correlation, pas de regles FTMO : on mesure la QUALITE DU
    SIGNAL, pas la courbe d'equity du compte ;
  - l'echantillon du journal est court (rotation) : quelques dizaines de trades ne
    prouvent rien. C'est un garde-fou contre l'illusion, pas une preuve de performance.

Utilisation :
    python desk/replay.py                 # simule toutes les ouvertures journalisees
    python desk/replay.py --shadow        # uniquement les cycles en mode ombre
    python desk/replay.py --roles         # revue de performance par employe (offline)
    python desk/replay.py --rejouer solo  # re-decider avec l'autre cerveau (LLM facture)
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

# Lance directement en ligne de commande, depuis n'importe quel repertoire de travail.
COMPANY = Path(__file__).resolve().parents[1]
if str(COMPANY) not in sys.path:
    sys.path.insert(0, str(COMPANY))

from desk import journal

log = logging.getLogger("desk.replay")

#: source de bougies : symbole -> sequence de {time, high, low, close} triee par temps
BarsSource = Callable[[str], Sequence[dict]]


@dataclass
class Issue:
    """Ce qu'est devenue une proposition d'ouverture."""
    resultat: str      # "tp" | "sl" | "ouvert" | "sans_donnees" | "invalide"
    R: float           # resultat en multiples du risque (stop = -1)
    bougies: int       # nb de bougies parcourues avant l'issue


# --------------------------------------------------------------------------- temps
def _ts(value: Any) -> Optional[datetime]:
    """Normalise un horodatage (datetime, Timestamp pandas, chaine ISO, epoch)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if hasattr(value, "to_pydatetime"):                       # pandas.Timestamp
        return _ts(value.to_pydatetime())
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    try:
        return _ts(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- simulation
def simulate_open(op: dict, bars: Sequence[dict], max_bars: int = 30,
                  cout_R: float = 0.0) -> Issue:
    """Deroule les bougies posterieures a la decision : stop ou cible en premier ?

    `cout_R` : provision de frais, retranchee au resultat (ex 0.05 = 5 % du risque
    parti en spread/commission/swap). Par defaut 0 -> resultat brut, optimiste.
    """
    entry, sl, tp = _num(op.get("entry")), _num(op.get("sl")), _num(op.get("tp"))
    sens = str(op.get("direction", "")).lower()
    risque = abs(entry - sl)
    if risque <= 0 or sens not in ("buy", "sell") or entry <= 0:
        return Issue("invalide", 0.0, 0)

    t0 = _ts(op.get("ts"))
    futures = [b for b in bars if t0 is None or ((_ts(b.get("time")) or t0) > t0)][:max_bars]
    if not futures:
        return Issue("sans_donnees", 0.0, 0)

    cible_R = abs(tp - entry) / risque if tp else 0.0
    for i, b in enumerate(futures, start=1):
        haut, bas = _num(b.get("high")), _num(b.get("low"))
        if sens == "buy":
            touche_sl, touche_tp = bas <= sl, (tp > 0 and haut >= tp)
        else:
            touche_sl, touche_tp = haut >= sl, (tp > 0 and bas <= tp)
        # Chemin intra-bougie inconnu : si les deux sont touches, on tranche pour le STOP.
        if touche_sl:
            return Issue("sl", round(-1.0 - cout_R, 3), i)
        if touche_tp:
            return Issue("tp", round(cible_R - cout_R, 3), i)

    cloture = _num(futures[-1].get("close"))
    latent = (cloture - entry) / risque if sens == "buy" else (entry - cloture) / risque
    return Issue("ouvert", round(latent - cout_R, 3), len(futures))


def simulate_opens(opens: Iterable[dict], source: BarsSource, max_bars: int = 30,
                   cout_R: float = 0.0) -> list[dict]:
    """Applique `simulate_open` a une liste d'ouvertures (cache de bougies par symbole)."""
    cache: dict[str, Sequence[dict]] = {}
    rows = []
    for op in opens:
        sym = str(op.get("symbol", "")).upper()
        if sym not in cache:
            try:
                cache[sym] = list(source(sym) or [])
            except Exception as e:                    # symbole retire, broker absent...
                log.warning("Bougies indisponibles pour %s: %s", sym, e)
                cache[sym] = []
        issue = simulate_open(op, cache[sym], max_bars=max_bars, cout_R=cout_R)
        rows.append({**op, "resultat": issue.resultat, "R": issue.R, "bougies": issue.bougies})
    return rows


# --------------------------------------------------------------------------- metriques
def metrics(rows: Sequence[dict]) -> dict:
    """Statistiques d'un lot de trades simules. Les issues sans donnees sont exclues."""
    utiles = [r for r in rows if r.get("resultat") in ("tp", "sl", "ouvert")]
    if not utiles:
        return {"n": 0, "exploitables": 0, "ignores": len(rows)}
    Rs = [_num(r.get("R")) for r in utiles]
    gains = [r for r in Rs if r > 0]
    pertes = [r for r in Rs if r < 0]
    cumul, sommet, dd = 0.0, 0.0, 0.0
    for r in Rs:                       # les lignes arrivent dans l'ordre chronologique
        cumul += r
        sommet = max(sommet, cumul)
        dd = min(dd, cumul - sommet)
    return {
        "n": len(rows), "exploitables": len(utiles), "ignores": len(rows) - len(utiles),
        "tp": sum(1 for r in utiles if r["resultat"] == "tp"),
        "sl": sum(1 for r in utiles if r["resultat"] == "sl"),
        "encore_ouverts": sum(1 for r in utiles if r["resultat"] == "ouvert"),
        "winrate_pct": round(len(gains) / len(utiles) * 100, 1),
        "somme_R": round(sum(Rs), 2),
        "expectancy_R": round(sum(Rs) / len(Rs), 3),
        "profit_factor": round(sum(gains) / abs(sum(pertes)), 2) if pertes else None,
        "max_drawdown_R": round(dd, 2),
        "meilleur_R": round(max(Rs), 2), "pire_R": round(min(Rs), 2),
    }


def by_key(rows: Sequence[dict], key: str) -> dict:
    """Meme metriques, ventilees (par strategie, par symbole, par mode...)."""
    groupes: dict[str, list[dict]] = {}
    for r in rows:
        groupes.setdefault(str(r.get(key) or "?"), []).append(r)
    return {k: metrics(v) for k, v in sorted(groupes.items())}


def compare(store, source: BarsSource, limit: Optional[int] = None, max_bars: int = 30,
            cout_R: float = 0.0) -> dict:
    """Propositions du cerveau vs trades REELLEMENT executes.

    L'ecart entre les deux, c'est le travail du moteur FTMO deterministe (veto,
    black-out news, garde week-end, budget de risque). S'il est systematiquement
    negatif, c'est que le moteur coute plus qu'il ne protege — a savoir, pas a croire.
    """
    proposes = simulate_opens(journal.planned_opens(store, limit=limit), source,
                              max_bars=max_bars, cout_R=cout_R)
    executes = simulate_opens(journal.executed_opens(store, limit=limit), source,
                              max_bars=max_bars, cout_R=cout_R)
    return {"proposes": metrics(proposes), "executes": metrics(executes),
            "par_strategie": by_key(proposes, "strategy"),
            "par_symbole": by_key(proposes, "symbol"),
            "lignes": {"proposes": proposes, "executes": executes}}


# --------------------------------------------------------------------------- rejeu LLM
def replay_cycles(store, agent, limit: Optional[int] = None,
                  shadow_only: bool = False) -> list[dict]:
    """Re-decide sur des dossiers de cycle ARCHIVES, avec le cerveau fourni.

    Permet de comparer solo vs desk sur exactement les memes donnees. ATTENTION :
    chaque cycle rejoue rappelle le LLM (cout reel). Les outils « live » ne sont PAS
    rebranches : le cerveau ne voit que le dossier archive (c'est voulu — sinon il
    lirait le marche d'aujourd'hui pour une decision d'hier).
    """
    from brain import tools as T

    sorties = []
    for c in journal.cycles(store, limit=limit, shadow_only=shadow_only):
        T.bind_context(c.get("snapshots") or {}, c.get("charts") or {},
                       c.get("account") or {}, c.get("positions") or [],
                       c.get("lessons") or "", c.get("strategies") or "",
                       c.get("news") or {}, postmortem=c.get("postmortem") or "")
        T.bind_live(None)
        try:
            actions = agent.decide(c.get("account") or {})
        except Exception as e:
            log.warning("Rejeu du cycle %s impossible: %s", c.get("ts"), e)
            actions = []
        for a in actions:
            if str(a.get("type", "")).lower() != "open":
                continue
            sorties.append({"ts": c.get("ts"), "symbol": str(a.get("symbol", "")).upper(),
                            "direction": str(a.get("direction", "")).lower(),
                            "strategy": a.get("strategy"), "confidence": a.get("confidence"),
                            "entry": a.get("entry"), "sl": a.get("sl"), "tp": a.get("tp"),
                            "origine": "rejeu"})
    return sorties


# --------------------------------------------------------------------------- sources
def mt5_source(broker, timeframe: str = "D1", n: int = 500) -> BarsSource:
    """Bougies fournies par MT5 (le broker doit etre connecte au moment du rejeu)."""
    def source(symbol: str) -> Sequence[dict]:
        df = broker.candles(symbol, timeframe, n)
        if df is None or getattr(df, "empty", True):
            return []
        colonnes = [c for c in ("time", "high", "low", "close") if c in df.columns]
        return df[colonnes].to_dict("records")
    return source


# --------------------------------------------------------------------------- rapport
def format_report(res: dict, titre: str = "REJEU") -> str:
    lignes = [f"===== {titre} =====", "",
              "-- OUVERTURES PROPOSEES PAR LE CERVEAU --", _bloc(res["proposes"]), "",
              "-- TRADES REELLEMENT EXECUTES --", _bloc(res["executes"]), ""]
    for titre_grp, cle in (("PAR STRATEGIE", "par_strategie"), ("PAR SYMBOLE", "par_symbole")):
        lignes.append(f"-- {titre_grp} (propositions) --")
        for nom, m in (res.get(cle) or {}).items():
            if m.get("exploitables"):
                lignes.append(f"  {nom:<18} n={m['exploitables']:<3} "
                              f"expectancy={m['expectancy_R']:+.3f}R  "
                              f"winrate={m['winrate_pct']}%  somme={m['somme_R']:+.2f}R")
        lignes.append("")
    lignes.append("Rappel : hors frais, tranche pessimiste en cas d'ambiguite intra-bougie, "
                  "sans sizing ni regles FTMO. Mesure du SIGNAL, pas de l'equity.")
    return "\n".join(lignes)


def _bloc(m: dict) -> str:
    if not m.get("exploitables"):
        return "  (aucun trade exploitable — journal vide ou bougies indisponibles)"
    return ("  n={exploitables} (tp={tp} sl={sl} ouverts={encore_ouverts}, ignores={ignores})\n"
            "  expectancy={expectancy_R:+.3f}R | winrate={winrate_pct}% | somme={somme_R:+.2f}R\n"
            "  profit factor={profit_factor} | drawdown={max_drawdown_R}R | "
            "meilleur={meilleur_R:+.2f}R pire={pire_R:+.2f}R").format(**m)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Rejeu et mesure des decisions journalisees")
    ap.add_argument("--limit", type=int, default=0, help="nb de cycles/evenements a lire")
    ap.add_argument("--shadow", action="store_true", help="uniquement les cycles en mode ombre")
    ap.add_argument("--timeframe", default="", help="timeframe des bougies (defaut: AGENT_TIMEFRAME)")
    ap.add_argument("--max-bars", type=int, default=0, help="bougies max apres la decision")
    ap.add_argument("--cout-r", type=float, default=0.0,
                    help="provision de frais en fraction du risque (ex 0.05)")
    ap.add_argument("--roles", action="store_true",
                    help="revue de performance par employe (aucun LLM, aucune bougie)")
    ap.add_argument("--rejouer", choices=("desk", "solo"), default="",
                    help="RE-DECIDER sur les cycles archives avec ce cerveau "
                         "(APPELS LLM REELS, donc facturés)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")
    from config import CFG
    from store import default_store

    store = default_store()

    # --- revue par employe : purement statistique, ni MT5 ni LLM
    if args.roles:
        from brain.memory import Memory
        from desk import bilan_roles
        print("===== REVUE DE PERFORMANCE DES EMPLOYES =====\n")
        print(bilan_roles.bloc_prompt(Memory().closed_trades()))
        return 0

    from broker.mt5_broker import MT5Broker

    proposees = journal.planned_opens(store, limit=args.limit or None,
                                      shadow_only=args.shadow)
    if not proposees and not args.rejouer:
        print("Aucune ouverture journalisee. Activer EVAL_JOURNAL_CYCLES=1 et laisser "
              "tourner l'agent (ou EVAL_SHADOW=1 pour observer sans executer).")
        return 1

    broker = MT5Broker(CFG.mt5, CFG)
    broker.connect()
    if not broker.connected:
        print("MT5 non connecte : impossible de recuperer les bougies posterieures aux "
              "decisions. Lancer ce rejeu sur la machine ou tourne le terminal MT5.")
        return 2
    source = mt5_source(broker, args.timeframe or CFG.timeframe)
    max_bars = args.max_bars or CFG.eval.replay_max_bars

    # --- rejeu d'un cerveau sur les MEMES dossiers archives (appels LLM reels)
    if args.rejouer:
        agent = _cerveau(args.rejouer, CFG)
        rejouees = replay_cycles(store, agent, limit=args.limit or None,
                                 shadow_only=args.shadow)
        rows = simulate_opens(rejouees, source, max_bars=max_bars, cout_R=args.cout_r)
        reelles = simulate_opens(journal.planned_opens(store, limit=args.limit or None,
                                                      shadow_only=args.shadow),
                                 source, max_bars=max_bars, cout_R=args.cout_r)
        print(f"===== REJEU '{args.rejouer.upper()}' SUR LES CYCLES ARCHIVES =====\n")
        print(f"-- CE QUE '{args.rejouer}' DECIDERAIT ({len(rejouees)} ouverture(s)) --")
        print(_bloc(metrics(rows)))
        print("\n-- CE QUI AVAIT ETE DECIDE A L'EPOQUE --")
        print(_bloc(metrics(reelles)))
        print("\nMeme donnees d'entree, deux cerveaux. Attention : un LLM n'est pas "
              "deterministe — un ecart faible peut n'etre que du hasard d'echantillonnage.")
        return 0

    res = compare(store, source, limit=args.limit or None,
                  max_bars=max_bars, cout_R=args.cout_r)
    print(format_report(res, titre=f"REJEU ({len(proposees)} propositions journalisees)"))
    return 0


def _cerveau(mode: str, cfg):
    """Instancie un cerveau pour le rejeu (meme contrat que run._lazy_agent)."""
    if mode == "solo":
        from brain.agent import TraderAgent
        return TraderAgent(cfg)
    from desk.desk import TradingDesk
    return TradingDesk(cfg)


if __name__ == "__main__":                       # pragma: no cover - point d'entree CLI
    raise SystemExit(main())
