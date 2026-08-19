# -*- coding: utf-8 -*-
"""POINT 1 — tracing pas-a-pas + checkpoint de cycle.

Verifie :
  - le tracer ecrit une ligne par etape LLM et sait resumer un cycle (nb, latence, echecs) ;
  - une trace ratee (repertoire impossible) ne leve jamais ;
  - le checkpoint reprend une phase du MEME cycle, ignore un autre cycle, et perime au TTL.
"""
import _isolation  # noqa: F401
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime, timedelta, timezone

from desk import trace
from desk.checkpoint import Checkpoint, signature


# ------------------------------------------------------------------ tracer
def test_trace_ecrit_une_ligne_par_etape(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "TRACES_DIR", tmp_path)
    trace.configure(True)
    cid = trace.new_cycle_id()
    tok = trace.start_cycle(cid)
    try:
        trace.record("gerant", "Gerant", "modelX", "sys", "hum", 12.3, output="ok")
        trace.record("trader", "Trader", "modelX", "sys", "hum", 45.6, error="boom")
    finally:
        trace.end_cycle(tok)

    etapes = trace.read_cycle(cid)
    assert len(etapes) == 2
    assert etapes[0]["role"] == "gerant" and etapes[0]["ok"] is True
    assert etapes[1]["ok"] is False and "boom" in etapes[1]["error"]

    s = trace.summary(cid)
    assert s["etapes"] == 2 and s["echecs"] == 1
    assert s["par_role"]["trader"]["echecs"] == 1
    assert s["latence_ms"] > 0


def test_trace_desactivee_n_ecrit_rien(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "TRACES_DIR", tmp_path)
    trace.configure(False)
    cid = trace.new_cycle_id()
    tok = trace.start_cycle(cid)
    try:
        trace.record("gerant", "Gerant", "m", "s", "h", 1.0)
    finally:
        trace.end_cycle(tok)
    assert trace.read_cycle(cid) == []
    trace.configure(True)                        # rétablit pour les autres tests


def test_trace_ratee_ne_leve_pas(monkeypatch):
    # repertoire impossible a creer -> l'ecriture echoue en silence, jamais d'exception
    monkeypatch.setattr(trace, "TRACES_DIR", Path("\x00invalide"))
    trace.configure(True)
    trace.record("x", "X", "m", "s", "h", 1.0)   # ne doit pas lever


# ------------------------------------------------------------------ checkpoint
def test_checkpoint_reprend_la_meme_phase():
    ck = Checkpoint("sig-A", enabled=True)
    assert ck.get("mandate") is None
    ck.set("mandate", {"posture": "prudent"})
    assert ck.get("mandate") == {"posture": "prudent"}
    ck.clear()
    assert ck.get("mandate") is None


def test_checkpoint_ignore_un_autre_cycle():
    Checkpoint("sig-B", enabled=True).set("mandate", {"x": 1})
    # une autre signature ne voit pas les artefacts du cycle precedent
    assert Checkpoint("sig-C", enabled=True).get("mandate") is None


def test_checkpoint_perime_au_ttl():
    ck = Checkpoint("sig-D", enabled=True, ttl_s=0)
    ck.set("mandate", {"x": 1})
    # ttl_s=0 : tout artefact est deja perime -> pas de reprise d'une decision rassie
    assert ck.get("mandate") is None


def test_checkpoint_desactive_est_noop():
    ck = Checkpoint("sig-E", enabled=False)
    ck.set("mandate", {"x": 1})
    assert ck.get("mandate") is None


def test_signature_stable_et_discriminante():
    s1 = signature("2026-08-19", ["EURUSD", "GBPUSD"], 100_000.0)
    s2 = signature("2026-08-19", ["GBPUSD", "EURUSD"], 100_049.0)   # ordre + bruit d'equity
    s3 = signature("2026-08-19", ["EURUSD"], 100_000.0)             # autres candidats
    assert s1 == s2                              # stable a l'ordre et au bucket d'equity
    assert s1 != s3                              # discrimine un cycle reellement different
