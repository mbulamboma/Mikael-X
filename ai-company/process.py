# -*- coding: utf-8 -*-
"""VIE DU PROCESSUS — arret propre et instance unique.

Deux problemes de DEPLOIEMENT que le code n'adressait pas, et qui ne se voient qu'en
production :

1. ARRET BRUTAL. `systemctl stop`, `docker stop` et Ctrl-C ne laissent pas forcement le
   temps de finir. Sans handler, un SIGTERM tue le process au milieu d'un cycle : la base
   n'est pas fermee proprement et les emails d'alerte en vol sont perdus — precisement
   ceux qu'on veut recevoir quand quelque chose se passe mal. On installe donc un
   drapeau d'arret : le signal ne tue plus, il DEMANDE l'arret ; la boucle le voit,
   termine ce qu'elle fait, vide la file d'emails et ferme la base.
   Un second signal, lui, tue tout de suite (l'operateur presse a toujours le dernier mot).

2. DEUX INSTANCES SUR LE MEME COMPTE. SQLite en WAL laisse deux process ouvrir la meme
   base sans broncher : deux agents tradaient le meme compte FTMO, chacun ignorant les
   ordres de l'autre — doublement du risque, comptage de la perte journaliere fausse, et
   trailing qui se battent. On pose donc un VERROU SYSTEME (pas un simple fichier PID :
   un fichier PID survit a un crash et bloque un redemarrage legitime). Le verrou est
   tenu par l'OS pour la duree de vie du process : s'il meurt, meme brutalement, le
   verrou tombe avec lui.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Optional

try:                                  # POSIX (VPS Linux, conteneur)
    import fcntl
except ImportError:                   # pragma: no cover - dependant de la plateforme
    fcntl = None
try:                                  # Windows (poste de dev, VPS Windows + MT5)
    import msvcrt
except ImportError:                   # pragma: no cover - dependant de la plateforme
    msvcrt = None

log = logging.getLogger("process")

#: Leve des qu'un signal d'arret a ete recu. Les boucles longues doivent le consulter.
ARRET = threading.Event()

#: Octet verrouille dans le fichier de verrou, place au-dela du PID ecrit en tete.
ZONE_VERROU = 4096

_DEJA_DEMANDE = False


def arret_demande() -> bool:
    return ARRET.is_set()


def demander_arret(raison: str = ""):
    """Demande l'arret depuis le code (tests, arret volontaire)."""
    if not ARRET.is_set():
        log.warning("ARRET DEMANDE%s — fin du cycle en cours, puis fermeture propre.",
                    f" ({raison})" if raison else "")
    ARRET.set()


def install_signal_handlers():
    """Transforme SIGINT/SIGTERM en demande d'arret. Sans effet hors thread principal
    (les tests, par exemple) — on ne veut pas que ca echoue pour autant."""
    def handler(signum, _frame):
        global _DEJA_DEMANDE
        nom = getattr(signal.Signals(signum), "name", str(signum))
        if _DEJA_DEMANDE:
            # deuxieme signal : l'operateur insiste, on ne discute pas
            log.error("%s recu une seconde fois — arret IMMEDIAT.", nom)
            raise KeyboardInterrupt(f"{nom} (insistant)")
        _DEJA_DEMANDE = True
        log.warning("%s recu — arret propre demande (positions NON fermees : elles restent "
                    "protegees par leur SL/TP chez le broker).", nom)
        ARRET.set()

    for nom in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, nom, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError) as e:   # pas le thread principal, ou signal absent
            log.debug("Handler %s non installe: %s", nom, e)


class VerrouInstance:
    """Verrou d'instance unique, tenu par l'OS (utilisable en `with`).

    `acquerir()` renvoie False si un autre process tient deja le verrou : l'appelant
    doit alors REFUSER de demarrer. On ne force jamais : deux agents sur le meme compte
    est la panne la plus chere du systeme.
    """

    def __init__(self, chemin: Path | str):
        self.chemin = Path(chemin)
        self._fd: Optional[int] = None

    # -- interne ---------------------------------------------------------------
    # On travaille sur un descripteur BRUT (os.open) et non un fichier texte : le mode
    # "a+" de Python force toute ecriture en fin de fichier, ce qui ferait grossir le
    # verrou d'un PID a chaque demarrage.
    def _verrouiller(self) -> bool:
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                # On verrouille un octet LOIN du debut du fichier : sous Windows, la zone
                # verrouillee devient illisible meme pour un autre handle du meme process.
                # En la placant apres le PID, le message d'erreur peut nommer le coupable
                # au lieu d'afficher « pid inconnu ».
                os.lseek(self._fd, ZONE_VERROU, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:                       # pragma: no cover - plateforme exotique
                log.warning("Aucun mecanisme de verrou disponible : instance unique NON "
                            "garantie sur cette plateforme.")
            return True
        except OSError:
            return False

    def _deverrouiller(self):
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(self._fd, ZONE_VERROU, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:                 # deja relache / descripteur ferme
            pass

    def proprietaire(self) -> str:
        """PID inscrit dans le fichier (indicatif, pour le message d'erreur). Peut etre
        illisible si l'OS protege la zone verrouillee — ce n'est pas bloquant."""
        try:
            return self.chemin.read_text(encoding="utf-8").strip() or "inconnu"
        except OSError:
            return "inconnu"

    # -- API -------------------------------------------------------------------
    def acquerir(self) -> bool:
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.chemin, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            log.error("Verrou d'instance inaccessible (%s) — demarrage refuse par prudence.", e)
            return False
        if not self._verrouiller():
            autre = self.proprietaire()
            self._fermer()
            log.error("UNE AUTRE INSTANCE TOURNE DEJA (pid %s, verrou %s). Demarrage "
                      "refuse : deux agents sur le meme compte doublent le risque et "
                      "faussent le calcul de la perte journaliere.", autre, self.chemin)
            return False
        try:                            # PID ecrit une fois le verrou tenu (diagnostic)
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, f"{os.getpid()}\n".encode())
            os.ftruncate(self._fd, len(f"{os.getpid()}\n"))
        except OSError:
            pass
        log.info("Verrou d'instance acquis (pid %s) : %s", os.getpid(), self.chemin)
        return True

    def liberer(self):
        if self._fd is None:
            return
        self._deverrouiller()
        self._fermer()

    def _fermer(self):
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    def __enter__(self):
        self.acquis = self.acquerir()
        return self

    def __exit__(self, *exc):
        self.liberer()
        return False


def reset_pour_tests():
    """Remet l'etat global a zero (les tests partagent le process)."""
    global _DEJA_DEMANDE
    _DEJA_DEMANDE = False
    ARRET.clear()
