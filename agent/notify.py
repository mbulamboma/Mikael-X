# -*- coding: utf-8 -*-
"""Notifications email — savoir ce que fait l'agent sans surveiller la console.

Quatre moments comptent :
  1. une position est OUVERTE  -> ce qu'il a pris, pourquoi, avec quel risque,
  2. une position est FERMEE   -> resultat en R, PnL, lecon retenue,
  3. une URGENCE se declenche  -> perte du jour proche de la limite, fermeture totale,
  4. l'IA tombe / le script s'arrete -> il faut intervenir a la main.

Chaque message porte l'ETAT DU PORTEFEUILLE (equity, PnL, perte du jour, objectif,
positions ouvertes) : un seul coup d'oeil suffit pour savoir ou on en est.

REGLE ABSOLUE : l'email ne doit jamais bloquer ni casser la boucle de trading.
Envoi dans un thread, timeout court, toute erreur est journalisee et oubliee.
"""
from __future__ import annotations

import logging
import smtplib
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from config import MailConfig

log = logging.getLogger("mail")


def _fmt(x, suffixe: str = "", digits: int = 2) -> str:
    try:
        return f"{float(x):,.{digits}f}{suffixe}".replace(",", " ")
    except (TypeError, ValueError):
        return "-"


class Mailer:
    def __init__(self, cfg: MailConfig):
        self.cfg = cfg
        self._threads: list[threading.Thread] = []
        if cfg.enabled and not cfg.ready:
            log.warning("Notifications email demandees mais incompletes "
                        "(MAIL_HOST/MAIL_TO) — desactivees.")

    # ------------------------------------------------------------------ envoi
    def send(self, sujet: str, corps: str, urgent: bool = False):
        """Envoi non bloquant. `urgent` prefixe le sujet pour reperage immediat."""
        if not self.cfg.ready:
            return
        prefixe = "[AGENT FTMO]" + (" [URGENT]" if urgent else "")
        t = threading.Thread(target=self._send_sync, args=(f"{prefixe} {sujet}", corps),
                             daemon=True)
        t.start()
        self._threads = [x for x in self._threads if x.is_alive()][:20] + [t]

    def _send_sync(self, sujet: str, corps: str):
        c = self.cfg
        try:
            msg = EmailMessage()
            nom, adresse = parseaddr(c.sender or c.user)
            msg["From"] = formataddr((nom or "Agent FTMO", adresse or c.user))
            msg["To"] = ", ".join(c.to)
            msg["Subject"] = sujet
            msg.set_content(corps)
            if c.mode == "ssl":
                serveur = smtplib.SMTP_SSL(c.host, c.port, timeout=c.timeout)
            else:
                serveur = smtplib.SMTP(c.host, c.port, timeout=c.timeout)
            with serveur:
                if c.mode == "tls":
                    serveur.starttls()
                if c.user and c.password:
                    serveur.login(c.user, c.password)
                serveur.send_message(msg)
            log.info("Email envoye : %s", sujet)
        except Exception as e:                     # jamais fatal pour le trading
            log.warning("Email non envoye (%s) : %s", sujet, e)

    def flush(self, seconds: float = 10.0):
        """Attend la fin des envois en cours (utilise a l'arret du script)."""
        for t in list(self._threads):
            t.join(timeout=seconds)

    # ------------------------------------------------------------------ contenus
    @staticmethod
    def portefeuille(summary: dict, positions: list) -> str:
        """Bloc etat du compte + positions, present dans TOUS les messages."""
        s = summary or {}
        lignes = [
            "ETAT DU PORTEFEUILLE",
            f"  Equity            : {_fmt(s.get('equity'))} $",
            f"  PnL total         : {_fmt(s.get('pnl_total_pct'))} %"
            f"  (objectif {_fmt(s.get('objectif_etape_pct') or s.get('objectif_pct'), ' %', 0)},"
            f" reste {_fmt(s.get('reste_avant_objectif_pct'))} %)",
            f"  Perte du jour     : {_fmt(s.get('perte_jour_pct'))} % "
            f"(stop agent {_fmt(s.get('stop_jour_agent_pct'), ' %', 0)})",
            f"  Etape             : {s.get('etape', '?')} | jour "
            f"{s.get('jours_ecoules', '?')} | jours trades "
            f"{s.get('jours_trades', '?')}/{s.get('jours_trades_min', '?')}",
            f"  Trades du jour    : {s.get('trades_du_jour', '?')}",
            "",
            f"POSITIONS OUVERTES ({len(positions or [])})",
        ]
        if not positions:
            lignes.append("  (aucune)")
        for p in positions or []:
            lignes.append(
                f"  #{p.get('ticket')} {p.get('symbol')} {str(p.get('direction','')).upper()} "
                f"{_fmt(p.get('volume'), '', 2)} lot | entree {p.get('entry')} | "
                f"SL {p.get('sl')} / TP {p.get('tp')} | "
                f"flottant {_fmt(p.get('floating_pnl'))} $ "
                f"({_fmt(p.get('floating_R'))} R) | [{p.get('strategy', '?')}]"
                + (" | trailing arme" if p.get("trailing") else ""))
        lignes.append("")
        lignes.append(f"Horodatage : {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
        return "\n".join(lignes)

    # ------------------------------------------------------------------ evenements
    def trade_ouvert(self, trade: dict, summary: dict, positions: list):
        if not (self.cfg.on_trade and self.cfg.ready):
            return
        d = trade
        corps = "\n".join([
            "POSITION OUVERTE",
            f"  {str(d.get('direction','')).upper()} {d.get('symbol')} — "
            f"strategie {d.get('strategy')}",
            f"  Lot               : {_fmt(d.get('lot'), '', 2)}",
            f"  Entree            : {d.get('entry')} (prevue {d.get('entry_planifiee')}, "
            f"slippage {_fmt(d.get('slippage_pips'), ' pips', 1)})",
            f"  Stop / Cible      : {d.get('sl')} / {d.get('tp')}",
            f"  Risque            : {_fmt(d.get('risk_dollars'))} $ "
            f"({_fmt(d.get('risk_pct'))} %) | R:R brut {_fmt(d.get('rr'))} / "
            f"net {_fmt(d.get('rr_net'))}",
            f"  Couts estimes     : {_fmt(d.get('couts_estimes'))} $ "
            f"(spread {_fmt(d.get('spread_pips_entree'), ' pips', 1)})",
            f"  Confiance         : {_fmt(d.get('confidence'), '', 2)}",
            "",
            "RAISONNEMENT",
            f"  {str(d.get('rationale') or '(non precise)')[:800]}",
            "",
            self.portefeuille(summary, positions),
        ])
        self.send(f"Ouverture {str(d.get('direction','')).upper()} {d.get('symbol')} "
                  f"({_fmt(d.get('lot'), '', 2)} lot)", corps)

    def trade_ferme(self, trade: dict, lecon: str, summary: dict, positions: list):
        if not (self.cfg.on_trade and self.cfg.ready):
            return
        R = trade.get("R", 0.0)
        verdict = "GAIN" if (R or 0) > 0 else ("PERTE" if (R or 0) < 0 else "NEUTRE")
        corps = "\n".join([
            f"POSITION FERMEE — {verdict}",
            f"  {trade.get('symbol')} {str(trade.get('direction','')).upper()} "
            f"[{trade.get('strategy')}]",
            f"  Resultat          : {_fmt(R)} R | PnL {_fmt(trade.get('pnl'))} $",
            f"  Entree / Sortie   : {trade.get('entry')} -> {trade.get('exit') or '(marche)'}",
            f"  R:R planifie      : {_fmt(trade.get('rr_planifie'))} "
            f"(net {_fmt(trade.get('rr_net_planifie'))})",
            f"  MFE / MAE         : {_fmt(trade.get('mfe_R'))} R / {_fmt(trade.get('mae_R'))} R",
            f"  Duree             : {_fmt(trade.get('duree_h'), ' h', 1)}",
            "",
            "LECON RETENUE",
            f"  {str(lecon or '(aucune)')[:800]}",
            "",
            self.portefeuille(summary, positions),
        ])
        self.send(f"Cloture {trade.get('symbol')} — {verdict} {_fmt(R)} R "
                  f"({_fmt(trade.get('pnl'))} $)", corps, urgent=(R or 0) <= -1.0)

    def alerte(self, titre: str, detail: str, summary: dict, positions: list,
               action_requise: str = ""):
        if not (self.cfg.on_alert and self.cfg.ready):
            return
        corps = "\n".join([
            titre.upper(),
            f"  {detail}",
            "",
            ("ACTION REQUISE\n  " + action_requise + "\n") if action_requise else "",
            self.portefeuille(summary, positions),
        ])
        self.send(titre, corps, urgent=True)
