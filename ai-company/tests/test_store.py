"""Persistance SQLite : l'etat survit a un arret brutal et a un redemarrage."""
import _isolation  # noqa: F401  (base SQLite temporaire)
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store import Store
from brain.memory import Memory


def _tmp() -> Path:
    return Path(tempfile.mkdtemp()) / "agent.db"


def test_etat_relu_apres_redemarrage():
    db = _tmp()
    mem = Memory(Store(db))
    mem.save_session({"phase": 1, "day": "2026-08-11", "trades_today": 2,
                      "trading_days": ["2026-08-10", "2026-08-11"]})
    mem.save_meta({"77": {"ticket": 77, "symbol": "EURUSD", "strategy": "trend_follow",
                          "risk_dollars": 990.0, "mfe_R": 1.8, "mae_R": -0.4,
                          "trail": {"enabled": True, "atr_mult": 2.0}}})
    mem.log_event("order_sent", {"symbol": "EURUSD", "lot": 0.5})
    mem.store.close()                                  # simule l'arret du process

    repris = Memory(Store(db))                          # redemarrage
    assert repris.load_session()["trades_today"] == 2
    m = repris.load_meta()["77"]
    assert m["strategy"] == "trend_follow" and m["mfe_R"] == 1.8
    assert m["trail"]["enabled"] is True                # le trailing arme est retrouve
    assert repris.store.events(kind="order_sent")[0]["lot"] == 0.5


def test_ecriture_des_positions_atomique():
    """meta_replace est transactionnel : jamais d'etat a moitie ecrit."""
    db = _tmp()
    st = Store(db)
    st.meta_replace({"1": {"ticket": 1}, "2": {"ticket": 2}})
    assert set(st.meta_all()) == {"1", "2"}
    st.meta_replace({"3": {"ticket": 3}})               # remplacement complet
    assert set(st.meta_all()) == {"3"}
    st.meta_replace({})                                 # book vide
    assert st.meta_all() == {}


def test_journal_filtrable_et_horodate():
    st = Store(_tmp())
    st.log_event("trade_closed", {"symbol": "EURUSD", "R": 1.5})
    st.log_event("risk_veto", {"symbol": "XAUUSD"})
    st.log_event("trade_closed", {"symbol": "GBPUSD", "R": -1.0})
    closed = st.events(kind="trade_closed")
    assert [t["symbol"] for t in closed] == ["EURUSD", "GBPUSD"]
    assert all(t["ts"] for t in closed)
    assert len(st.events()) == 3
    assert len(st.events(limit=2)) == 2


def test_kv_persiste_les_etats_divers():
    st = Store(_tmp())
    assert st.kv_get("safe_mode", {}) == {}
    st.kv_set("safe_mode", {"actif": True, "cycles": 3})
    assert st.kv_get("safe_mode")["cycles"] == 3
    st.kv_set("safe_mode", {"actif": True, "cycles": 4})    # upsert
    assert st.kv_get("safe_mode")["cycles"] == 4
    st.kv_delete("safe_mode")
    assert st.kv_get("safe_mode", {}) == {}


def test_migration_des_anciens_fichiers_json():
    """L'etat JSON existant est importe puis mis de cote (rien n'est perdu)."""
    d = Path(tempfile.mkdtemp())
    (d / "session.json").write_text(json.dumps({"phase": 2, "trades_today": 1}), encoding="utf-8")
    (d / "open_meta.json").write_text(json.dumps({"9": {"ticket": 9, "symbol": "XAUUSD"}}),
                                      encoding="utf-8")
    (d / "lessons.json").write_text(json.dumps([{"ts": "2026-08-01T00:00:00+00:00",
                                                 "symbol": "EURUSD", "outcome": "loss",
                                                 "text": "Ne pas surtrader", "tags": ["momentum"]}]),
                                    encoding="utf-8")
    (d / "trades.jsonl").write_text(
        json.dumps({"ts": "2026-08-02T00:00:00+00:00", "kind": "trade_closed",
                    "symbol": "EURUSD", "R": 2.0}) + "\n"
        + "{ligne corrompue\n", encoding="utf-8")

    mem = Memory(Store(d / "agent.db"))
    assert mem.load_session()["phase"] == 2
    assert mem.load_meta()["9"]["symbol"] == "XAUUSD"
    assert mem.closed_trades()[0]["R"] == 2.0            # ligne corrompue ignoree
    assert (d / "session.json.migrated").exists()        # ancien fichier conserve
    assert not (d / "session.json").exists()

    Memory(Store(d / "agent.db"))                        # 2e demarrage : pas de doublon
    assert len(Memory(Store(d / "agent.db")).closed_trades()) == 1


def test_status_ne_plante_pas_sur_une_base_vide():
    import run as R
    o = R.Orchestrator()
    o.mem = Memory(Store(_tmp()))
    o.print_status()                                     # doit s'afficher sans exception
