# -*- coding: utf-8 -*-
"""ASSAINISSEMENT DU TEXTE EXTERNE — tout ce qui vient du web est une donnee HOSTILE.

News, posts Reddit, tweets, resumes d'API : ce texte est ecrit par n'importe qui et finit
dans un moteur qui engage du capital. C'est une SURFACE D'INJECTION DE PROMPT. Ce module est
le point unique ou on neutralise un extrait avant qu'un LLM ne le lise :

  - liens et @mentions retires (bruit + hameconnage) ;
  - tout extrait qui contient une tournure IMPERATIVE/INJECTIVE (« ignore tes consignes »,
    « achete maintenant », « you are... ») est ECARTE ENTIER — on ne garde pas un texte qui
    cherche a diriger, meme a moitie nettoye ;
  - longueur plafonnee.

Partage par desk/social.py (sentiment social) et data/sources.py (news/fondamentaux).
"""
from __future__ import annotations

import re
from typing import Optional

_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_MENTION = re.compile(r"[@#]\w+")
#: tournures qui trahissent une tentative de pilotage plutot qu'un avis de marche.
_INJECTION = re.compile(
    r"\b(ignore|disregard|forget|override|system|prompt|instruction|assistant|"
    r"you are|tu es|oublie|ignore[rz]|agis comme|act as|jailbreak|api[_ ]?key|"
    r"buy now|sell now|achete maintenant|vends maintenant)\b", re.I)


def clean_snippet(texte: str, max_len: int = 200) -> Optional[str]:
    """Neutralise un extrait. Rend None si l'extrait tente une injection ou ne contient plus
    rien d'exploitable apres nettoyage."""
    if not texte:
        return None
    t = _URL.sub("", str(texte))
    t = _MENTION.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or _INJECTION.search(t):
        return None
    return t[:max_len]
