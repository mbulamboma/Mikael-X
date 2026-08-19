# -*- coding: utf-8 -*-
"""REVUE DE PERFORMANCE DES EMPLOYES — qui, dans l'entreprise, apporte vraiment du signal ?

Le dossier de decision (Phase 1C) est archive avec chaque trade. En le croisant avec le R
realise, on peut enfin poser la question qui derange : *cet employe sert-il a quelque chose ?*

Ce qu'on mesure, employe par employe :
  - ANALYSTES : leur biais etait-il ALIGNE avec la direction prise ? On compare l'esperance
    des trades ou l'analyste etait d'accord a celle des trades ou il etait contre. Un
    analyste dont les deux chiffres sont identiques n'apporte rien ; un analyste dont le
    desaccord predit mieux que l'accord est un CONTRE-INDICATEUR (information precieuse).
  - JUGE : esperance par niveau de conviction. Une conviction qui ne discrimine pas est
    du bruit deguise en chiffre.
  - DEBAT : esperance selon qu'il a fallu 1 ou 2 tours (les dossiers contestes sont-ils
    moins bons ?).
  - COLLEGE DU RISQUE : esperance des trades approuves vs reduits — le durcissement
    protege-t-il, ou coupe-t-il les gagnants ?
  - GERANT : esperance par posture, et par niveau de confiance du Trader.

HONNETETE STATISTIQUE — la partie la plus importante de ce module. Sur quelques trades,
tous ces chiffres sont du bruit. Chaque ligne porte donc son effectif, et toute cellule
sous `MIN_N` est explicitement marquee « echantillon insuffisant » au lieu d'etre affichee
comme un resultat. Un desk qui apprend d'un echantillon de 3 apprend des faussetes.

Aucun appel LLM ici : on lit des trades clotures, on rend des chiffres et un bloc de texte.
"""
from __future__ import annotations

from typing import Any, Optional

#: en dessous de cet effectif, on refuse de conclure (et on le dit)
MIN_N = 5

ANALYSTES = ("technique", "fondamental", "sentiment", "actualite")
_BIAIS_DIRECTION = {"haussier": "buy", "baissier": "sell"}


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _stats(Rs: list[float]) -> dict:
    if not Rs:
        return {"n": 0}
    gagnants = sum(1 for r in Rs if r > 0)
    return {"n": len(Rs), "expectancy_R": round(sum(Rs) / len(Rs), 3),
            "winrate_pct": round(gagnants / len(Rs) * 100, 1),
            "somme_R": round(sum(Rs), 2), "suffisant": len(Rs) >= MIN_N}


def _avec_dossier(trades: list[dict]) -> list[dict]:
    """Seuls les trades ouverts par le desk depuis la Phase 1C portent la chaine de decision."""
    return [t for t in (trades or [])
            if isinstance(t, dict) and (t.get("dossier") or {}) and _f(t.get("R")) is not None]


def bilan(trades: list[dict]) -> dict:
    """Statistiques par employe. `{}` tant qu'aucun trade ne porte de dossier."""
    trades = _avec_dossier(trades)
    if not trades:
        return {}
    out: dict[str, Any] = {"n_trades_traces": len(trades)}

    # --- analystes : aligne avec la direction prise, ou pas
    analystes = {}
    for role in ANALYSTES:
        accord, desaccord = [], []
        for t in trades:
            brief = ((t["dossier"].get("briefs") or {}).get(role) or {})
            attendu = _BIAIS_DIRECTION.get(str(brief.get("biais") or "").lower())
            R = _f(t.get("R"))
            if attendu is None or R is None:          # biais neutre ou brief absent : neutre
                continue
            (accord if attendu == str(t.get("direction", "")).lower() else desaccord).append(R)
        if accord or desaccord:
            analystes[role] = {"aligne": _stats(accord), "oppose": _stats(desaccord)}
    if analystes:
        out["analystes"] = analystes

    # --- juge : la conviction discrimine-t-elle ?
    forte, faible, par_tours = [], [], {}
    for t in trades:
        debat = t["dossier"].get("debat") or {}
        R = _f(t.get("R"))
        if not debat or R is None:
            continue
        conviction = _f((debat.get("verdict") or {}).get("conviction"))
        if conviction is not None:
            (forte if conviction >= 0.7 else faible).append(R)
        tours = debat.get("tours")
        if tours:
            par_tours.setdefault(f"{tours} tour(s)", []).append(R)
    if forte or faible:
        out["juge"] = {"conviction>=0.7": _stats(forte), "conviction<0.7": _stats(faible)}
    if par_tours:
        out["debat"] = {k: _stats(v) for k, v in sorted(par_tours.items())}

    # --- college du risque : durcir protege-t-il ?
    par_verdict: dict[str, list[float]] = {}
    for t in trades:
        verdict = str((t["dossier"].get("risque") or {}).get("verdict") or "").lower()
        R = _f(t.get("R"))
        if verdict and R is not None:
            par_verdict.setdefault(verdict, []).append(R)
    if par_verdict:
        out["college_risque"] = {k: _stats(v) for k, v in sorted(par_verdict.items())}

    # --- gerant : la posture du cycle, et la confiance du Trader
    par_posture: dict[str, list[float]] = {}
    haute, basse = [], []
    for t in trades:
        R = _f(t.get("R"))
        if R is None:
            continue
        posture = str((t["dossier"].get("mandat") or {}).get("posture") or "").lower()
        if posture:
            par_posture.setdefault(posture, []).append(R)
        conf = _f((t["dossier"].get("trader") or {}).get("confidence"))
        if conf is not None:
            (haute if conf >= 0.7 else basse).append(R)
    if par_posture:
        out["gerant_par_posture"] = {k: _stats(v) for k, v in sorted(par_posture.items())}
    if haute or basse:
        out["trader_par_confiance"] = {"confiance>=0.7": _stats(haute),
                                       "confiance<0.7": _stats(basse)}
    return out


def _mediane(xs: list[float]) -> float:
    xs = sorted(xs)
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def bilan_debat(debates: list[dict], seuil_gap: Optional[float] = None) -> dict:
    """CALIBRATION de DESK_DEBATE_GAP a partir des debats JOURNALISES (desk/journal.debates),
    abstentions comprises — l'echantillon que `bilan()` ne voit pas, puisqu'une abstention ne
    devient jamais un trade.

    On mesure deux choses, et deux seulement :
      - l'ecart initial (tour 1) entre les convictions : sa distribution dit si le seuil est
        bien place (trop de debats juste au-dessus = on rate des seconds tours utiles ; trop
        en-dessous = on paie des tours pour rien) ;
      - la SEPARATION apportee par un second tour (gap_final - gap_initial) : si elle est
        quasi nulle, le second tour n'apprend rien et le seuil est trop genereux.

    Sous MIN_N debats, on REFUSE de conclure (comme `bilan`). {} si rien de journalise."""
    debates = [d for d in (debates or []) if isinstance(d, dict)]
    if not debates:
        return {}
    n = len(debates)
    verdicts: dict[str, int] = {}
    gaps: list[float] = []
    separations: list[float] = []            # gap_final - gap_initial, debats a >= 2 tours
    n_second_tour = 0
    for d in debates:
        verdicts[str(d.get("direction") or "?").lower()] = \
            verdicts.get(str(d.get("direction") or "?").lower(), 0) + 1
        gi, gf = _f(d.get("gap_initial")), _f(d.get("gap_final"))
        if gi is not None:
            gaps.append(gi)
        if (_f(d.get("tours")) or 1) >= 2:
            n_second_tour += 1
            if gi is not None and gf is not None:
                separations.append(round(gf - gi, 3))
    out: dict[str, Any] = {"n_debats": n, "suffisant": n >= MIN_N, "verdicts": verdicts,
                           "second_tour": {"n": n_second_tour,
                                           "pct": round(100 * n_second_tour / n, 1)}}
    if gaps:
        out["gap_initial"] = {"min": round(min(gaps), 2), "mediane": round(_mediane(gaps), 2),
                              "max": round(max(gaps), 2)}
    if separations:
        out["separation_2e_tour_moyenne"] = round(sum(separations) / len(separations), 3)
    if seuil_gap is not None and gaps:
        sous = sum(1 for g in gaps if g <= seuil_gap)
        out["sous_le_seuil"] = {"seuil": round(seuil_gap, 2), "n": sous,
                                "pct": round(100 * sous / len(gaps), 1)}
    return out


def bloc_debat(debates: list[dict], seuil_gap: Optional[float] = None) -> str:
    """Bloc lisible de la calibration du debat, pour `replay.py --roles`."""
    b = bilan_debat(debates, seuil_gap)
    if not b:
        return ("(aucun debat journalise — activer EVAL_JOURNAL_CYCLES=1 ou EVAL_SHADOW=1 et "
                "laisser tourner le desk ; la calibration du seuil apparaitra alors)")
    lignes = [f"Debats journalises : {b['n_debats']}"]
    if not b["suffisant"]:
        lignes.append(f"ATTENTION : moins de {MIN_N} debats. BRUIT statistique — a lire, "
                      f"pas a suivre pour regler DESK_DEBATE_GAP.")
    verd = ", ".join(f"{k}={v}" for k, v in sorted(b["verdicts"].items()))
    lignes.append(f"  verdicts : {verd}")
    st = b["second_tour"]
    lignes.append(f"  second tour : {st['n']} debat(s) ({st['pct']}%)")
    if "gap_initial" in b:
        g = b["gap_initial"]
        lignes.append(f"  ecart tour 1 (conviction) : min={g['min']} mediane={g['mediane']} "
                      f"max={g['max']}")
    if "sous_le_seuil" in b:
        s = b["sous_le_seuil"]
        lignes.append(f"  sous le seuil {s['seuil']} (declenche un 2e tour) : {s['n']} "
                      f"({s['pct']}%)")
    if "separation_2e_tour_moyenne" in b:
        sep = b["separation_2e_tour_moyenne"]
        verdict = ("le 2e tour SEPARE les camps" if sep > 0.05 else
                   "le 2e tour n'apporte quasi rien -> seuil peut-etre trop genereux")
        lignes.append(f"  separation moyenne au 2e tour : {sep:+.3f} — {verdict}")
    return "\n".join(lignes)


def _ligne(nom: str, s: dict) -> str:
    if not s.get("n"):
        return f"    {nom:<18} (aucun cas)"
    if not s.get("suffisant"):
        return (f"    {nom:<18} n={s['n']} — ECHANTILLON INSUFFISANT (< {MIN_N}), "
                f"ne rien en conclure")
    return (f"    {nom:<18} n={s['n']:<3} expectancy={s['expectancy_R']:+.3f}R  "
            f"winrate={s['winrate_pct']}%  cumul={s['somme_R']:+.2f}R")


def bloc_prompt(trades: list[dict]) -> str:
    """Bloc lisible pour les prompts du Gerant et du coach."""
    b = bilan(trades)
    if not b:
        return ("(aucun trade tracé pour l'instant — la revue de performance apparaitra "
                "quand des trades ouverts par le desk auront ete clotures)")
    lignes = [f"Trades avec chaine de decision tracee : {b['n_trades_traces']}"]
    if b["n_trades_traces"] < MIN_N:
        lignes.append(f"ATTENTION : moins de {MIN_N} trades traces. Tout ce qui suit est du "
                      f"BRUIT statistique — a lire, pas a suivre.")
    for role, valeurs in (b.get("analystes") or {}).items():
        lignes.append(f"  ANALYSTE {role.upper()} (son biais vs la direction prise) :")
        lignes.append(_ligne("aligne", valeurs["aligne"]))
        lignes.append(_ligne("oppose", valeurs["oppose"]))
    for cle, titre in (("juge", "JUGE (par conviction)"), ("debat", "DEBAT (par nb de tours)"),
                       ("college_risque", "COLLEGE DU RISQUE (par verdict)"),
                       ("gerant_par_posture", "GERANT (par posture)"),
                       ("trader_par_confiance", "TRADER (par confiance)")):
        groupe = b.get(cle)
        if not groupe:
            continue
        lignes.append(f"  {titre} :")
        for nom, s in groupe.items():
            lignes.append(_ligne(nom, s))
    return "\n".join(lignes)
