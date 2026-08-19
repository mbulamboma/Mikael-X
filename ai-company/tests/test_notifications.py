"""Notifications email : contenu, declenchement, et innocuite en cas de panne SMTP."""
import _isolation  # noqa: F401  (base SQLite temporaire)
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import MailConfig
from notify import Mailer

CFG = dict(enabled=True, host="smtp.test", port=465, user="u", password="p",
           sender="Agent FTMO <bot@test>", to=("moi@test",))


def _mailer(**kw):
    m = Mailer(MailConfig(**{**CFG, **kw}))
    m.envois = []
    # `send(sujet, corps_texte, html, urgent)` : on capture le repli TEXTE (corps) et le
    # HTML separement — les assertions de contenu portent sur le texte, plus stable.
    m.send = lambda sujet, corps, html="", urgent=False: m.envois.append(
        (sujet, corps, urgent, html))
    return m


SUMMARY = {"equity": 101_500.0, "pnl_total_pct": 1.5, "perte_jour_pct": 0.4,
           "objectif_etape_pct": 10, "reste_avant_objectif_pct": 8.5,
           "stop_jour_agent_pct": 4, "etape": 1, "jours_ecoules": 3,
           "jours_trades": 2, "jours_trades_min": 4, "trades_du_jour": 1}
POSITIONS = [{"ticket": 77, "symbol": "EURUSD", "direction": "buy", "volume": 0.5,
              "entry": 1.1000, "sl": 1.0950, "tp": 1.1200, "floating_pnl": 120.0,
              "floating_R": 0.4, "strategy": "trend_follow", "trailing": {"enabled": True}}]


def test_bloc_portefeuille_dans_chaque_message():
    corps = Mailer.portefeuille(SUMMARY, POSITIONS)
    for attendu in ("ETAT DU PORTEFEUILLE", "Equity", "101 500", "Perte du jour",
                    "POSITIONS OUVERTES (1)", "#77 EURUSD BUY", "trend_follow",
                    "trailing arme"):
        assert attendu in corps, attendu


def test_portefeuille_vide_lisible():
    corps = Mailer.portefeuille({}, [])
    assert "(aucune)" in corps and "ETAT DU PORTEFEUILLE" in corps


def test_mail_ouverture_contient_le_risque_et_la_these():
    m = _mailer()
    m.trade_ouvert({"symbol": "XAUUSD", "direction": "buy", "strategy": "donchian_breakout",
                    "lot": 0.12, "entry": 2400.5, "entry_planifiee": 2400.0,
                    "slippage_pips": 0.5, "sl": 2380.0, "tp": 2450.0,
                    "risk_dollars": 980.0, "risk_pct": 0.98, "rr": 2.5, "rr_net": 2.2,
                    "couts_estimes": 18.0, "spread_pips_entree": 1.5, "confidence": 0.72,
                    "rationale": "Cassure Donchian avec dollar faible."},
                   summary=SUMMARY, positions=POSITIONS)
    sujet, corps, urgent, html = m.envois[0]
    assert "Ouverture BUY XAUUSD" in sujet and not urgent
    assert "donchian_breakout" in corps and "0.98 %" in corps
    assert "Cassure Donchian" in corps and "ETAT DU PORTEFEUILLE" in corps
    # version HTML : document complet, statut et metrique phare presents
    assert html.startswith("<!DOCTYPE html>") and "color-scheme" in html
    assert "Achat" in html and "de risque" in html


def test_mail_cloture_marque_les_pertes_comme_urgentes():
    m = _mailer()
    trade = {"symbol": "EURUSD", "direction": "buy", "strategy": "trend_follow",
             "R": -1.0, "pnl": -1000.0, "entry": 1.10, "exit": 1.095,
             "rr_planifie": 2.0, "rr_net_planifie": 1.8, "mfe_R": 0.3, "mae_R": -1.0,
             "duree_h": 30.0}
    m.trade_ferme(trade, summary=SUMMARY, positions=[])
    sujet, corps, urgent, html = m.envois[0]
    assert "PERTE" in sujet and urgent is True
    assert "MFE / MAE" in corps

    m2 = _mailer()
    m2.trade_ferme({**trade, "R": 2.0, "pnl": 2000.0}, summary=SUMMARY, positions=[])
    assert "GAIN" in m2.envois[0][0] and m2.envois[0][2] is False


def test_alerte_porte_l_action_requise():
    m = _mailer()
    m.alerte("IA indisponible — pilote de secours actif", "Bedrock: quota depasse",
             summary=SUMMARY, positions=POSITIONS,
             action_requise="Verifier la cle Bedrock.")
    sujet, corps, urgent, html = m.envois[0]
    assert urgent and "IA indisponible" in sujet
    assert "ACTION REQUISE" in corps and "Verifier la cle Bedrock." in corps
    assert "Action requise" in html and "Verifier la cle Bedrock." in html


def test_notifications_desactivables():
    m = _mailer(on_trade=False)
    m.trade_ouvert({"symbol": "EURUSD"}, summary={}, positions=[])
    assert m.envois == []
    m2 = _mailer(on_alert=False)
    m2.alerte("x", "y", summary={}, positions=[])
    assert m2.envois == []
    m3 = _mailer(host="")                       # configuration incomplete
    assert m3.cfg.ready is False


def test_mode_smtp_deduit_du_port():
    assert MailConfig(**{**CFG, "secure": ""}).mode == "ssl"          # port 465
    assert MailConfig(**{**CFG, "secure": "", "port": 587}).mode == "tls"
    assert MailConfig(**{**CFG, "secure": "none"}).mode == "none"
    assert MailConfig(**{**CFG, "secure": "true", "port": 587}).mode == "tls"


def test_panne_smtp_ne_casse_jamais_le_trading():
    """Un serveur mail injoignable ne doit jamais remonter dans la boucle de trading.

    `_send_sync` LEVE desormais (c'est la couche de reessai qui decide de la suite) ;
    l'API publique `send()`, elle, ne doit ni lever ni bloquer."""
    import pytest
    m = Mailer(MailConfig(**{**CFG, "host": "127.0.0.1", "port": 1, "timeout": 1}))
    with pytest.raises(Exception):
        m._send_sync("test", "corps")
    m.send("test", "corps")                     # ne doit PAS lever
    m.flush(5)
    assert m.echoues == 1 and m.envoyes == 0


def test_orchestrateur_notifie_sans_planter_sans_smtp():
    import run as R
    o = R.Orchestrator()
    o.mail = _mailer()
    o._last_summary = SUMMARY
    o.mem = type("M", (), {"load_meta": lambda self: {}})()
    o.broker = type("B", (), {"positions": lambda self, own_only=None: []})()
    o._notifier("alerte", "Test", "detail", action_requise="rien")
    assert o.mail.envois and o.mail.envois[0][0] == "Test"
