# -*- coding: utf-8 -*-
"""Ce qui ne se voit qu'EN PRODUCTION : arret propre, instance unique, file d'emails,
retention du journal.

Le dossier est deploye seul sur une machine et doit y survivre a un `systemctl stop`,
a un double lancement, a une coupure SMTP et a des mois de fonctionnement continu.
Chacun de ces quatre points a ete un defaut reel du code avant ces tests.
"""
import _isolation  # noqa: F401  (base SQLite temporaire + SMTP coupe)
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import notify
import process
from config import MailConfig
from notify import Mailer
from store import Store

CFG_MAIL = dict(enabled=True, to=("dest@exemple.com",), host="smtp.exemple.com", port=465,
                secure="ssl", user="u", password="p", sender="Agent <a@b.c>",
                on_trade=True, on_alert=True, timeout=2)


@pytest.fixture(autouse=True)
def _process_propre():
    """Les drapeaux de `process` sont globaux : on les remet a zero entre les tests."""
    process.reset_pour_tests()
    yield
    process.reset_pour_tests()


# ============================================================ file d'emails
class _MailerTest(Mailer):
    """Mailer dont l'envoi reussit (ou echoue) a la demande, sans reseau."""
    def __init__(self, echoue=False, lent=0.0):
        super().__init__(MailConfig(**CFG_MAIL))
        self.echoue = echoue
        self.lent = lent
        self.recus = []
        self.tentatives = 0

    def _send_sync(self, sujet, corps, html=""):
        self.tentatives += 1
        if self.lent:
            time.sleep(self.lent)
        if self.echoue:
            raise ConnectionRefusedError("SMTP indisponible (test)")
        self.recus.append(sujet)


def test_send_ne_bloque_pas_et_flush_livre_tout():
    m = _MailerTest()
    for i in range(5):
        m.send(f"message {i}", "corps")
    assert m.flush(5) is True
    assert len(m.recus) == 5 and m.envoyes == 5


def test_un_seul_worker_quel_que_soit_le_volume():
    """Un thread par email n'avait aucune borne : une rafale d'alertes en creait autant."""
    m = _MailerTest()
    for i in range(20):
        m.send(f"message {i}", "corps")
    workers = [t for t in threading.enumerate() if t.name == "mailer" and t.is_alive()]
    assert len(workers) == 1
    m.flush(5)


def test_reessai_avant_d_abandonner():
    """Une coupure SMTP de trois secondes ne doit pas perdre une alerte en silence."""
    m = _MailerTest(echoue=True)
    m.send("alerte", "corps")
    m.flush(5)
    assert m.tentatives == notify.ESSAIS
    assert m.echoues == 1 and m.envoyes == 0


def test_file_saturee_jette_sans_bloquer(monkeypatch):
    monkeypatch.setattr(notify, "FILE_MAX", 3)
    m = Mailer(MailConfig(**CFG_MAIL))
    m._file = notify.queue.Queue(maxsize=3)
    m._demarrer_worker = lambda: None            # personne ne consomme : la file sature
    for i in range(6):
        m.send(f"message {i}", "corps")          # ne doit ni lever ni bloquer
    assert m._file.qsize() == 3 and m.jetes == 3


def test_une_alerte_urgente_chasse_un_message_ordinaire(monkeypatch):
    monkeypatch.setattr(notify, "FILE_MAX", 2)
    m = Mailer(MailConfig(**CFG_MAIL))
    m._file = notify.queue.Queue(maxsize=2)
    m._demarrer_worker = lambda: None
    m.send("ordinaire 1", "corps")
    m.send("ordinaire 2", "corps")
    m.send("URGENCE", "corps", urgent=True)
    restants = [m._file.get_nowait()[0] for _ in range(m._file.qsize())]
    assert any("URGENCE" in s for s in restants)
    assert not any("ordinaire 1" in s for s in restants)   # le plus ancien a ete sacrifie


def test_flush_sans_worker_ne_bloque_pas():
    m = _MailerTest()
    assert m.flush(1) is True                    # rien n'a jamais ete envoye


def test_arret_demande_ecourte_les_reessais():
    """Un SIGTERM ne doit pas etre retarde par un serveur SMTP qui ne repond plus."""
    m = _MailerTest(echoue=True)
    monkey_backoff = notify.BACKOFF
    notify.BACKOFF = (30.0, 30.0)                # attente longue... sauf si arret demande
    try:
        process.demander_arret("test")
        debut = time.monotonic()
        m.send("alerte", "corps")
        m.flush(5)
        assert time.monotonic() - debut < 3      # on n'a pas attendu 30 s
        assert m.tentatives == 1                 # on n'insiste pas pendant un arret
    finally:
        notify.BACKOFF = monkey_backoff


# ============================================================ arret propre
def test_demander_arret_leve_le_drapeau():
    assert process.arret_demande() is False
    process.demander_arret("test")
    assert process.arret_demande() is True and process.ARRET.is_set()


def test_handlers_installables_sans_exception():
    process.install_signal_handlers()            # thread principal : doit passer
    process.install_signal_handlers()            # idempotent


def test_le_watchdog_rend_la_main_des_qu_un_arret_est_demande():
    """`wait` au lieu de `sleep` : sinon SIGTERM attendait la fin du pas de watchdog."""
    import run as R
    o = R.Orchestrator.__new__(R.Orchestrator)   # sans toucher au broker
    o.cfg = type("C", (), {"execution": type("E", (), {"watch_seconds": 60})()})()
    process.demander_arret("test")
    debut = time.monotonic()
    assert o._sleep_with_watchdog(3600) is False
    assert time.monotonic() - debut < 1          # retour immediat, pas 60 s


# ============================================================ instance unique
def test_deux_instances_ne_peuvent_pas_tourner(tmp_path):
    verrou1 = process.VerrouInstance(tmp_path / "agent.lock")
    verrou2 = process.VerrouInstance(tmp_path / "agent.lock")
    assert verrou1.acquerir() is True
    assert verrou2.acquerir() is False           # deux agents = risque double, compte fausse
    verrou1.liberer()
    assert verrou2.acquerir() is True            # le verrou tombe avec son proprietaire
    verrou2.liberer()


def test_le_verrou_note_son_proprietaire(tmp_path):
    import os
    v = process.VerrouInstance(tmp_path / "agent.lock")
    assert v.acquerir() is True
    try:
        assert v.proprietaire() == str(os.getpid())
    finally:
        v.liberer()


def test_le_verrou_ne_grossit_pas_a_chaque_demarrage(tmp_path):
    """Un fichier ouvert en 'a+' aurait accumule un PID par lancement."""
    chemin = tmp_path / "agent.lock"
    for _ in range(3):
        v = process.VerrouInstance(chemin)
        assert v.acquerir() is True
        v.liberer()
    assert len(chemin.read_text(encoding="utf-8").strip().splitlines()) == 1


def test_run_refuse_de_demarrer_si_une_instance_tourne(tmp_path, monkeypatch):
    import run as R
    monkeypatch.setattr(R, "STATE_DIR", tmp_path)
    tenant = process.VerrouInstance(tmp_path / "agent.lock")
    assert tenant.acquerir() is True
    try:
        o = R.Orchestrator()
        assert o.run(loop=False) == 3            # code de sortie dedie, avant tout trade
    finally:
        tenant.liberer()


# ============================================================ journal SQLite
def _store(tmp_path):
    return Store(tmp_path / "purge.db", migrate=False)


def _vieil_event(st, kind, jours):
    ts = (datetime.now(timezone.utc) - timedelta(days=jours)).isoformat()
    st._exec("INSERT INTO events (ts, kind, payload) VALUES (?, ?, '{}')", (ts, kind))


def test_purge_epargne_la_memoire_du_compte(tmp_path):
    """Perdre un `trade_closed`, c'est effacer l'experience de l'agent."""
    st = _store(tmp_path)
    for kind in ("plan", "modify", "gate_block", "trade_closed", "order_sent", "risk_veto"):
        _vieil_event(st, kind, 200)
    supprimes = st.purge_events(90)
    restants = {e["kind"] for e in st.events()}
    assert supprimes == 3
    assert restants == {"trade_closed", "order_sent", "risk_veto"}
    st.close()


def test_purge_epargne_les_evenements_recents(tmp_path):
    st = _store(tmp_path)
    _vieil_event(st, "plan", 10)
    _vieil_event(st, "plan", 200)
    assert st.purge_events(90) == 1
    assert len(st.events(kind="plan")) == 1
    st.close()


def test_purge_desactivable(tmp_path):
    st = _store(tmp_path)
    _vieil_event(st, "plan", 500)
    assert st.purge_events(0) == 0
    assert len(st.events()) == 1
    st.close()


def test_fermeture_replie_le_wal(tmp_path):
    """Sans checkpoint, `agent.db-wal` grossit indefiniment et une sauvegarde du seul
    `.db` serait incomplete."""
    st = Store(tmp_path / "wal.db", migrate=False)
    for i in range(200):
        st.log_event("plan", {"i": i})
    st.close()
    wal = tmp_path / "wal.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    # les donnees sont bien dans le fichier principal apres fermeture
    relu = Store(tmp_path / "wal.db", migrate=False)
    assert len(relu.events(kind="plan")) == 200
    relu.close()


def test_store_partage_cree_une_seule_fois(tmp_path, monkeypatch):
    """Le worker email, le watchdog et la boucle peuvent le demander en meme temps."""
    import store as store_mod

    class _StoreLent(Store):
        """Creation volontairement lente : sans verrou, plusieurs threads en creeraient
        chacun un, et deux connexions SQLite se partageraient l'etat du compte."""
        def __init__(self, path=None, migrate=True):
            time.sleep(0.02)
            super().__init__(tmp_path / "concurrent.db", migrate=False)

    monkeypatch.setattr(store_mod, "_DEFAULT", None)
    monkeypatch.setattr(store_mod, "Store", _StoreLent)
    obtenus = []
    fils = [threading.Thread(target=lambda: obtenus.append(store_mod.default_store()))
            for _ in range(8)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    assert len(obtenus) == 8 and len({id(s) for s in obtenus}) == 1
    obtenus[0].close()
