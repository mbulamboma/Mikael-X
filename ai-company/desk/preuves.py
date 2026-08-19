# -*- coding: utf-8 -*-
"""LES PREUVES — une affirmation non verifiable ne fonde aucune decision.

Un desk d'agents LLM produit naturellement de la prose convaincante : « le dollar
devrait se renforcer », « la structure semble haussiere », « le marche va chercher la
liquidite au-dessus ». Ces phrases n'ont AUCUN contenu verifiable. Elles se propagent
pourtant jusqu'a un ordre reel, parce que rien dans la chaine ne les distingue d'un
fait. Ce module est ce filtre, et il est DETERMINISTE (aucun LLM ici) :

  1. `faits(dossier)` extrait de la matiere premiere du cycle (scan, chart, indicateurs,
     couts, macro, news, briefs...) l'ensemble des VALEURS reellement observees ;
  2. `sourcee(texte, faits)` dit si une affirmation cite au moins une de ces valeurs ;
  3. `filtrer(...)` separe les affirmations sourcees des autres.

Ce qui compte comme preuve : un NOMBRE precis qui existe dans le dossier (un niveau de
prix, un ATR, un RSI, un spread, un ecart de taux, un %), a 1 % pres — les modeles
arrondissent — ou une DATE ISO du dossier. Ce qui ne compte pas : un nombre trop banal
pour prouver quoi que ce soit (« 2 » se retrouve partout), et evidemment une phrase sans
le moindre chiffre.

REGLE DE SECURITE appliquee par les appelants : une affirmation non sourcee ne peut
jamais AUGMENTER le risque. Elle peut toujours le reduire — un doute n'a pas besoin de
preuve pour justifier de s'abstenir, alors qu'une prise de position, elle, en a besoin.
C'est pourquoi le college du risque n'est pas filtre (il ne peut que durcir) tandis que
les analystes, le debat, le verdict du juge et les ouvertures du Trader le sont.

Coupe-circuit : `DESK_EXIGER_PREUVES=0` (cf. config.DeskConfig) desactive le filtre si
un jour il se revele trop strict — les prompts, eux, continuent d'exiger des chiffres.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

#: tolerance relative entre un chiffre cite et la valeur du dossier (les LLM arrondissent :
#: 1.08503 devient « 1.085 », un ATR de 0.00412 devient « 0.0041 »).
TOLERANCE = 0.01

#: un nombre « banal » ne prouve rien : il se retrouve dans n'importe quel dossier. On
#: n'accepte comme citation qu'un nombre PRECIS : soit decimal, soit >= 10.
_NOMBRE = re.compile(r"[-+]?\d{1,3}(?:[   ]?\d{3})*(?:[.,]\d+)?")
_DATE_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")

#: cles du dossier qui ne sont PAS des observations de marche : les citer ne prouve rien
#: (un ticket, un magic number ou une taille de compte n'est l'indice de rien).
CLES_IGNOREES = {"ticket", "magic", "id", "account_size", "solde_initial", "phase",
                 "deviation", "lot_step", "digits"}


class Faits:
    """Ce que le dossier permet d'affirmer : des valeurs numeriques + le texte brut."""

    def __init__(self, valeurs: Iterable[float], texte: str = ""):
        self.valeurs = sorted({round(float(v), 10) for v in valeurs})
        self.texte = texte

    def __len__(self) -> int:
        return len(self.valeurs)

    def __bool__(self) -> bool:
        return bool(self.valeurs) or bool(self.texte)

    def contient(self, valeur: float, tolerance: float = TOLERANCE) -> bool:
        """La valeur citee existe-t-elle dans le dossier (a la tolerance pres) ?"""
        for f in self.valeurs:
            if f == valeur:
                return True
            ref = max(abs(f), abs(valeur))
            if ref and abs(f - valeur) / ref <= tolerance:
                return True
        return False


def _collecte(obj: Any, valeurs: list[float], morceaux: list[str], profondeur: int = 0):
    if profondeur > 8:
        return
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        valeurs.append(float(obj))
        return
    if isinstance(obj, str):
        morceaux.append(obj)
        # un dossier serialise contient des nombres dans des chaines ("1.0850", "62.4%")
        for m in _NOMBRE.finditer(obj):
            v = _valeur(m.group(0))
            if v is not None:
                valeurs.append(v)
        return
    if isinstance(obj, dict):
        for cle, val in obj.items():
            if str(cle).lower() in CLES_IGNOREES:
                continue
            morceaux.append(str(cle))
            _collecte(val, valeurs, morceaux, profondeur + 1)
        return
    if isinstance(obj, (list, tuple, set)):
        for val in obj:
            _collecte(val, valeurs, morceaux, profondeur + 1)


def faits(*dossiers: Any) -> Faits:
    """Toutes les valeurs observables des dossiers fournis (scan, chart, briefs, couts...).

    On agrege plusieurs sources parce qu'un debatteur argumente a partir des briefs des
    analystes AUTANT qu'a partir du marche : un chiffre repris d'un brief reste un chiffre
    du dossier, pas une invention."""
    valeurs: list[float] = []
    morceaux: list[str] = []
    for d in dossiers:
        _collecte(d, valeurs, morceaux)
    return Faits(valeurs, " ".join(morceaux))


def _valeur(token: str) -> float | None:
    t = token.replace(" ", "").replace(" ", "").replace(",", ".")
    # "1.085.50" (separateur de milliers) : on ne devine pas, on jette
    if t.count(".") > 1:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _precis(token: str, valeur: float) -> bool:
    """Un nombre ne vaut comme CITATION que s'il est assez precis pour designer une
    observation : soit il a une partie decimale, soit il vaut au moins 10. « 2 » ou « 3 »
    se retrouvent dans tous les dossiers et ne prouveraient rien."""
    return ("." in token or "," in token) or abs(valeur) >= 10


def citations(texte: str, f: Faits) -> list[str]:
    """Les elements du texte qui correspondent VRAIMENT a une donnee du dossier."""
    if not texte or not f:
        return []
    trouvees: list[str] = []
    for m in _DATE_ISO.finditer(texte):
        if m.group(0) in (f.texte or ""):
            trouvees.append(m.group(0))
    for m in _NOMBRE.finditer(texte):
        token = m.group(0)
        v = _valeur(token)
        if v is None or not _precis(token, v):
            continue
        if f.contient(v):
            trouvees.append(token)
    # dedoublonne en gardant l'ordre
    vues, out = set(), []
    for c in trouvees:
        if c not in vues:
            vues.add(c)
            out.append(c)
    return out


def sourcee(texte: str, f: Faits, minimum: int = 1) -> bool:
    """L'affirmation cite-t-elle au moins `minimum` donnee(s) du dossier ?"""
    return len(citations(texte, f)) >= max(1, minimum)


def filtrer(items: Iterable[str], f: Faits) -> tuple[list[str], list[str]]:
    """(sourcees, speculations) — l'ordre d'origine est conserve dans chaque liste."""
    gardees, rejetees = [], []
    for item in items or []:
        texte = str(item)
        (gardees if sourcee(texte, f) else rejetees).append(texte)
    return gardees, rejetees
