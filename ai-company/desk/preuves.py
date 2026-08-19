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
_NOMBRE = re.compile(
    r"[-+]?\d{1,3}(?:[  ,]\d{3})+(?:\.\d+)?"      # 4,402.35 | 1 234 567
    r"|[-+]?\d+(?:[.,]\d+)?")                          # 4402.35 | 1,085 | 2451
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
            valeurs.extend(_valeurs(m.group(0)))
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


def _valeurs(token: str) -> list[float]:
    """Lectures POSSIBLES d'un jeton, car « 1,085 » est ambigu : 1085 en notation
    anglaise, 1.085 en notation francaise. Le filtre tranche en acceptant la citation
    si l'UNE des lectures existe dans le dossier — se tromper en acceptant coute une
    citation fortuite ; se tromper en rejetant fait taire un analyste qui avait raison.
    """
    t = token.replace(" ", "").replace(" ", "")
    if not t or t in ("+", "-"):
        return []
    virgule, point = "," in t, "." in t
    out: list[float] = []

    def _f(x: str):
        try:
            out.append(float(x))
        except ValueError:
            pass

    if virgule and point:
        # le SEPARATEUR DECIMAL est le dernier des deux ; l'autre groupe les milliers
        if t.rfind(",") > t.rfind("."):
            _f(t.replace(".", "").replace(",", "."))
        else:
            _f(t.replace(",", ""))
    elif virgule:
        _f(t.replace(",", ""))                      # anglais : 4,402 -> 4402
        if t.count(",") == 1:
            _f(t.replace(",", "."))                 # francais : 1,085 -> 1.085
    elif point:
        _f(t)                                       # decimal
        if t.count(".") == 1 and len(t.split(".")[1]) == 3:
            _f(t.replace(".", ""))                  # 4.402 pourrait etre 4402
    else:
        _f(t)
    # dedoublonne en gardant l'ordre
    vues, uniques = set(), []
    for v in out:
        if v not in vues:
            vues.add(v)
            uniques.append(v)
    return uniques


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
        if any(_precis(token, v) and f.contient(v) for v in _valeurs(token)):
            trouvees.append(token)
    # dedoublonne en gardant l'ordre
    vues, out = set(), []
    for c in trouvees:
        if c not in vues:
            vues.add(c)
            out.append(c)
    return out


#: nombre de mots consecutifs qu'une affirmation doit reprendre TELS QUELS du dossier
#: pour valoir citation textuelle. Six mots ne s'inventent pas : c'est un acte de copie.
_MOTS_VERBATIM = 6


def _normalise(texte: str) -> str:
    return " ".join((texte or "").lower().split())


def citation_texte(texte: str, f: Faits) -> str:
    """Le plus long extrait du texte repris VERBATIM du dossier, "" si aucun.

    Certains analystes n'ont pas de chiffres a citer : l'analyste ACTUALITE travaille sur
    des titres, l'analyste SENTIMENT sur des formulations. Leur imposer un nombre les
    condamnait a etre neutralises a CHAQUE cycle, quel que soit leur travail — le filtre
    ne mesurait plus leur rigueur, il mesurait la nature de leur matiere. Reprendre six
    mots consecutifs du dossier reste un acte de sourcage verifiable, et cela n'ouvre
    aucune porte a l'invention."""
    if not texte or not (f.texte or ""):
        return ""
    aiguille, meule = _normalise(texte), _normalise(f.texte)
    mots = aiguille.split()
    for taille in range(len(mots), _MOTS_VERBATIM - 1, -1):
        for i in range(len(mots) - taille + 1):
            extrait = " ".join(mots[i:i + taille])
            if extrait in meule:
                return extrait
    return ""


def sourcee(texte: str, f: Faits, minimum: int = 1, texte_ok: bool = False) -> bool:
    """L'affirmation cite-t-elle au moins `minimum` donnee(s) du dossier ?

    `texte_ok` autorise EN PLUS la citation verbatim (voir `citation_texte`). Il est
    reserve aux analystes : le Trader et le juge, eux, engagent le risque et restent
    tenus au chiffre — une phrase recopiee ne justifie pas d'ouvrir une position."""
    if len(citations(texte, f)) >= max(1, minimum):
        return True
    return bool(texte_ok and minimum == 1 and citation_texte(texte, f))


def filtrer(items: Iterable[str], f: Faits,
            texte_ok: bool = False) -> tuple[list[str], list[str]]:
    """(sourcees, speculations) — l'ordre d'origine est conserve dans chaque liste."""
    gardees, rejetees = [], []
    for item in items or []:
        texte = str(item)
        (gardees if sourcee(texte, f, texte_ok=texte_ok) else rejetees).append(texte)
    return gardees, rejetees
