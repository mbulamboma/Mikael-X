# -*- coding: utf-8 -*-
"""Notifications email — savoir ce que fait l'agent sans surveiller la console.

Quatre moments comptent :
  1. une position est OUVERTE  -> ce qu'il a pris, pourquoi, avec quel risque,
  2. une position est FERMEE   -> resultat en R, PnL, lecon retenue,
  3. une URGENCE se declenche  -> perte du jour proche de la limite, fermeture totale,
  4. l'IA tombe / le script s'arrete -> il faut intervenir a la main.

Chaque message porte l'ETAT DU PORTEFEUILLE (equity, PnL, perte du jour, objectif,
positions ouvertes) : un seul coup d'oeil suffit pour savoir ou on en est.

MISE EN FORME : message multipart (texte + HTML). Le HTML est concu pour les clients
mail, pas pour un navigateur : tableaux, styles EN LIGNE (Gmail/Outlook suppriment les
feuilles de style), largeur 600 px, aucune image externe (elles sont bloquees par
defaut), barres de progression faites en cellules de tableau.

REGLE ABSOLUE : l'email ne doit jamais bloquer ni casser la boucle de trading.
File d'attente bornee + un worker + timeout court ; toute erreur est journalisee. Mais
une alerte ne doit pas non plus se PERDRE : d'ou le reessai et la vidange a l'arret
(`flush()`, appele par l'orchestrateur dans son `finally`).
"""
from __future__ import annotations

import html as _html
import logging
import queue
import smtplib
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Optional

from config import MailConfig

try:
    # Une demande d'arret ecourte les attentes entre deux tentatives : on ne retarde
    # pas la fermeture du process pour un serveur SMTP qui ne repond plus.
    from process import ARRET as _ARRET
except ImportError:                    # pragma: no cover - notify utilisable seul
    _ARRET = threading.Event()

log = logging.getLogger("mail")

# --- palette : claire par defaut, adaptee au sombre via <style> @media pour les
#     clients qui savent lire prefers-color-scheme (Apple Mail, iOS, Outlook mobile).
#     Les couleurs d'accent (vert/rouge/ambre/bleu) restent lisibles sur les deux fonds.
ENCRE = "#0f172a"   # texte principal (slate-900)
DOUX = "#64748b"    # texte secondaire (slate-500)
FAINT = "#94a3b8"   # texte discret : horodatage, pied (slate-400)
BORD = "#e7ebf0"    # filet / hairline
PAGE = "#eef1f6"    # fond de page
FOND = "#f6f8fc"    # fond des tuiles KPI
CARTE = "#ffffff"   # carte
VERT = "#12a150"    # gain
ROUGE = "#e5484d"   # perte
AMBRE = "#c2740a"   # alerte
BLEU = "#3b6df5"    # accent neutre
# pastilles / tuiles teintees par statut (fond clair)
VERT_BG = "#e7f6ee"
ROUGE_BG = "#fdecec"
AMBRE_BG = "#fbf1e0"
BLEU_BG = "#eaf0fe"
SLATE_BG = "#eef1f6"
POLICE = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,"
          "'Noto Sans',sans-serif")

# Bloc <style> injecte dans le <head> : dark-mode pour les clients compatibles. Les
# styles EN LIGNE gerent le theme clair ; ces regles !important assombrissent les
# grandes surfaces (les styles en ligne l'emportent sinon). Les elements themables
# portent une classe (.eb .card .ink .mut .hair .tile .badge .track .foot).
_STYLE = """
  :root { color-scheme: light dark; supported-color-schemes: light dark; }
  @media (prefers-color-scheme: dark) {
    .eb    { background:#0b0f17 !important; }
    .card  { background:#151b26 !important; border-color:#26303f !important; }
    .ink   { color:#f1f5f9 !important; }
    .mut   { color:#94a3b8 !important; }
    .hair  { border-color:#26303f !important; }
    .tile  { background:#1b2330 !important; border-color:#26303f !important; }
    .badge { background:#1b2330 !important; }
    .track { background:#26303f !important; }
    .foot  { color:#64748b !important; }
  }
"""


def _cls_ink(couleur: str) -> str:
    """Classe .ink SEULEMENT pour le texte neutre (a eclaircir en sombre) — jamais
    pour une couleur d'accent, qui reste elle-meme sur fond sombre."""
    return "ink" if couleur == ENCRE else ""


def _fmt(x, suffixe: str = "", digits: int = 2) -> str:
    try:
        return f"{float(x):,.{digits}f}{suffixe}".replace(",", " ")
    except (TypeError, ValueError):
        return "-"


def _signe(x, suffixe: str = "", digits: int = 2) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "-"
    return f"{v:+,.{digits}f}{suffixe}".replace(",", " ")


def _couleur_valeur(x, neutre: str = ENCRE) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return neutre
    return VERT if v > 0 else (ROUGE if v < 0 else neutre)


def _e(t) -> str:
    return _html.escape(str(t if t is not None else ""))


# ------------------------------------------------------------------ briques HTML
def _kpi(label: str, valeur: str, couleur: str = ENCRE, note: str = "") -> str:
    return (
        f'<td class="tile" style="padding:12px 13px;background:{FOND};'
        f'border:1px solid {BORD};border-radius:12px;vertical-align:top;">'
        f'<div class="mut" style="font:600 10px/1.4 {POLICE};letter-spacing:.07em;'
        f'color:{DOUX};text-transform:uppercase;">{_e(label)}</div>'
        f'<div class="{_cls_ink(couleur)}" style="font:800 19px/1.3 {POLICE};'
        f'color:{couleur};white-space:nowrap;margin-top:4px;letter-spacing:-.01em;">'
        f'{_e(valeur)}</div>'
        + (f'<div class="mut" style="font:500 11px/1.45 {POLICE};color:{DOUX};'
           f'margin-top:2px;">{_e(note)}</div>' if note else "")
        + '</td>')


def _rangee_kpis(cellules: list[str]) -> str:
    """Grille de KPI : 2 par ligne (rendu correct meme sur mobile)."""
    lignes = []
    for i in range(0, len(cellules), 2):
        paire = cellules[i:i + 2]
        if len(paire) == 1:
            paire.append('<td style="width:50%"></td>')
        lignes.append('<tr>' + f'<td style="width:8px"></td>'.join(paire) + '</tr>')
    espaceur = '<tr><td colspan="3" style="height:8px"></td></tr>'
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="table-layout:fixed;">' + espaceur.join(lignes) + '</table>')


def _barre(pct: float, couleur: str) -> str:
    """Barre de progression en cellules de tableau (aucune image, aucun CSS externe)."""
    p = max(0.0, min(100.0, float(pct or 0)))
    reste = 100.0 - p
    plein = (f'<td width="{p:.0f}%" style="background:{couleur};height:7px;'
             f'border-radius:6px;font-size:0;line-height:0;">&nbsp;</td>') if p > 0 else ""
    vide = (f'<td class="track" width="{reste:.0f}%" style="background:{BORD};height:7px;'
            f'border-radius:6px;font-size:0;line-height:0;">&nbsp;</td>') if reste > 0 else ""
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="margin:7px 0 2px;"><tr>' + plein + vide + '</tr></table>')


def _titre_section(t: str) -> str:
    return (f'<div class="mut" style="font:700 11px/1.6 {POLICE};letter-spacing:.08em;'
            f'color:{DOUX};text-transform:uppercase;margin:24px 0 10px;">{_e(t)}</div>')


def _lignes_detail(paires: list[tuple[str, str]]) -> str:
    rows = []
    for label, valeur in paires:
        rows.append(
            f'<tr><td class="mut hair" style="padding:8px 0;font:400 13px/1.5 {POLICE};'
            f'color:{DOUX};border-bottom:1px solid {BORD};width:45%;">{_e(label)}</td>'
            f'<td class="ink hair" style="padding:8px 0;font:600 13px/1.5 {POLICE};'
            f'color:{ENCRE};border-bottom:1px solid {BORD};text-align:right;">{valeur}</td></tr>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            + "".join(rows) + '</table>')


def _citation(texte: str) -> str:
    return (f'<div class="ink hair" style="border-left:3px solid {BORD};'
            f'padding:2px 0 3px 14px;font:400 14px/1.65 {POLICE};color:{ENCRE};">'
            f'{_e(texte)}</div>')


def _tableau_positions(positions: list) -> str:
    if not positions:
        return (f'<div class="mut" style="font:400 13px/1.6 {POLICE};color:{DOUX};'
                f'padding:10px 0;">Aucune position ouverte.</div>')
    entetes = ("Position", "Entree", "SL / TP", "Flottant")
    th = "".join(
        f'<th align="{"right" if i else "left"}" class="mut hair" '
        f'style="font:600 10px/1.5 {POLICE};letter-spacing:.06em;color:{DOUX};'
        f'text-transform:uppercase;padding:0 0 8px;border-bottom:1px solid {BORD};">'
        f'{_e(h)}</th>' for i, h in enumerate(entetes))
    lignes = []
    for p in positions:
        sens = str(p.get("direction", "")).upper()
        couleur_sens = VERT if sens == "BUY" else ROUGE
        pnl_c = _couleur_valeur(p.get("floating_pnl"))
        r = p.get("floating_R")
        lignes.append(
            f'<tr>'
            f'<td class="ink hair" style="padding:10px 0;border-bottom:1px solid {BORD};'
            f'font:400 13px/1.45 {POLICE};color:{ENCRE};">'
            f'<span style="font-weight:700;">{_e(p.get("symbol"))}</span> '
            f'<span style="color:{couleur_sens};font-weight:700;">{_e(sens)}</span>'
            f'<div class="mut" style="font-size:11px;color:{DOUX};margin-top:2px;">'
            f'#{_e(p.get("ticket"))} · {_fmt(p.get("volume"), " lot", 2)} · '
            f'{_e(p.get("strategy", "?"))}'
            + (' · trailing' if p.get("trailing") else '') + '</div></td>'
            f'<td align="right" class="ink hair" style="padding:10px 0;'
            f'border-bottom:1px solid {BORD};font:400 13px/1.45 {POLICE};color:{ENCRE};">'
            f'{_e(p.get("entry"))}</td>'
            f'<td align="right" class="mut hair" style="padding:10px 0;'
            f'border-bottom:1px solid {BORD};font:400 12px/1.45 {POLICE};color:{DOUX};">'
            f'{_e(p.get("sl"))}<br>{_e(p.get("tp"))}</td>'
            f'<td align="right" class="{_cls_ink(pnl_c)} hair" style="padding:10px 0;'
            f'border-bottom:1px solid {BORD};font:700 13px/1.45 {POLICE};color:{pnl_c};">'
            f'{_signe(p.get("floating_pnl"), " $")}'
            f'<div class="mut" style="font-size:11px;font-weight:400;color:{DOUX};">'
            + (f'{_signe(r, " R")}' if r is not None else "&nbsp;") + '</div></td></tr>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
            f'<tr>{th}</tr>' + "".join(lignes) + '</table>')


def _badge(texte: str, couleur: str, fond: str) -> str:
    """Pastille de statut : point colore + libelle, sur fond teinte."""
    return (f'<span class="badge" style="display:inline-block;padding:6px 12px;'
            f'border-radius:999px;background:{fond};color:{couleur};'
            f'font:700 11px/1 {POLICE};letter-spacing:.04em;text-transform:uppercase;">'
            f'&#9679;&nbsp; {_e(texte)}</span>')


def _document(*, accent: str, badge: str, badge_bg: str, titre: str, corps: str,
              hero: str = "", hero_note: str = "", hero_couleur: str = ENCRE) -> str:
    """Enveloppe : carte claire, barre d'accent fine, entete (pastille de statut +
    titre + metrique phare), corps, pied. Full document HTML (avec <head>) pour porter
    la meta color-scheme et le <style> dark-mode. Aucun bandeau colore plein : l'accent
    est reduit a une barre et a la pastille, plus lisible et moins date."""
    horo = f"{datetime.now(timezone.utc):%d/%m/%Y · %H:%M} UTC"
    hero_html = ""
    if hero:
        hero_html = (
            f'<div style="margin-top:16px;">'
            f'<div class="{_cls_ink(hero_couleur)}" style="font:800 30px/1.05 {POLICE};'
            f'color:{hero_couleur};letter-spacing:-.02em;">{_e(hero)}</div>'
            + (f'<div class="mut" style="font:500 13px/1.5 {POLICE};color:{DOUX};'
               f'margin-top:5px;">{_e(hero_note)}</div>' if hero_note else "")
            + '</div>')
    return (
        '<!DOCTYPE html><html lang="fr"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f'<style>{_STYLE}</style></head>'
        f'<body class="eb" style="margin:0;padding:0;background:{PAGE};">'
        f'<div class="eb" style="background:{PAGE};padding:26px 12px;">'
        f'<table role="presentation" align="center" width="600" cellpadding="0" '
        f'cellspacing="0" style="width:100%;max-width:600px;margin:0 auto;">'
        f'<tr><td class="card" style="background:{CARTE};border:1px solid {BORD};'
        f'border-radius:16px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        # barre d'accent fine
        f'<tr><td style="height:4px;background:{accent};font-size:0;line-height:0;'
        f'border-radius:16px 16px 0 0;">&nbsp;</td></tr>'
        # entete : pastille + horodatage, puis titre + metrique phare
        f'<tr><td style="padding:22px 24px 6px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td>{_badge(badge, accent, badge_bg)}</td>'
        f'<td align="right" class="mut" style="font:500 11px/1.5 {POLICE};'
        f'color:{FAINT};">{horo}</td></tr></table>'
        f'<div class="ink" style="font:800 21px/1.3 {POLICE};color:{ENCRE};'
        f'letter-spacing:-.01em;margin-top:16px;">{_e(titre)}</div>'
        f'{hero_html}'
        f'</td></tr>'
        # corps
        f'<tr><td style="padding:8px 24px 26px;">{corps}</td></tr>'
        f'</table></td></tr>'
        # pied (hors carte)
        f'<tr><td class="foot" style="padding:16px 8px 2px;font:500 11px/1.6 {POLICE};'
        f'color:{FAINT};text-align:center;">Agent trader FTMO · message automatique · '
        f'{horo}</td></tr>'
        f'</table></div></body></html>')


#: file d'attente bornee : au-dela, on jette plutot que de laisser la memoire filer.
FILE_MAX = 100
#: tentatives par message (une panne SMTP est le plus souvent transitoire)
ESSAIS = 3
#: attente entre deux tentatives, en secondes (croissante)
BACKOFF = (5.0, 20.0)


class Mailer:
    """Envoi d'emails NON BLOQUANT, avec file bornee, reessai et vidange a l'arret.

    Pourquoi une file et un seul worker plutot qu'un thread par email :
      - un thread par message n'a aucune borne — une rafale d'alertes (watchdog en
        boucle, panne LLM repetee) creait autant de threads que d'alertes, chacun
        ouvrant une socket SMTP avec 20 s de timeout ;
      - un thread daemon meurt avec le process : a l'arret, les alertes en vol etaient
        PERDUES, precisement quand on veut etre prevenu. `flush()` attend maintenant
        que la file soit vide et l'orchestrateur l'appelle dans son `finally` ;
      - sans reessai, une coupure SMTP de trois secondes perdait l'alerte en silence.

    Le trading n'est JAMAIS bloque : `send()` depose et rend la main immediatement, et
    si la file est pleine, on jette le message le plus ancien NON URGENT.
    """

    def __init__(self, cfg: MailConfig):
        self.cfg = cfg
        self._file: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=FILE_MAX)
        self._worker: Optional[threading.Thread] = None
        self._verrou = threading.Lock()
        self.envoyes = 0
        self.echoues = 0
        self.jetes = 0
        if cfg.enabled and not cfg.ready:
            log.warning("Notifications email demandees mais incompletes "
                        "(MAIL_HOST/MAIL_TO) — desactivees.")

    # ------------------------------------------------------------------ envoi
    def send(self, sujet: str, corps: str, html: str = "", urgent: bool = False):
        """Depose un message dans la file. Ne bloque jamais, ne leve jamais."""
        if not self.cfg.ready:
            return
        prefixe = "[AGENT FTMO]" + (" [URGENT]" if urgent else "")
        message = (f"{prefixe} {sujet}", corps, html, bool(urgent))
        self._demarrer_worker()
        try:
            self._file.put_nowait(message)
        except queue.Full:
            if self._faire_de_la_place(urgent):
                try:
                    self._file.put_nowait(message)
                    return
                except queue.Full:                 # course improbable : on abandonne
                    pass
            self.jetes += 1
            log.error("File d'emails saturee (%d) — message JETE : %s", FILE_MAX, sujet)

    def _faire_de_la_place(self, urgent: bool) -> bool:
        """Un message urgent a le droit de chasser un message ordinaire en attente."""
        if not urgent:
            return False
        gardes = []
        jete = False
        try:
            while True:
                m = self._file.get_nowait()
                self._file.task_done()
                if not jete and m is not None and not m[3]:
                    jete = True                     # on sacrifie le plus ancien non urgent
                    continue
                gardes.append(m)
        except queue.Empty:
            pass
        for m in gardes:
            try:
                self._file.put_nowait(m)
            except queue.Full:                      # pragma: no cover - ne devrait pas arriver
                break
        if jete:
            self.jetes += 1
            log.warning("File pleine : un email ordinaire a ete jete pour laisser passer "
                        "une alerte urgente.")
        return jete

    def _demarrer_worker(self):
        with self._verrou:
            if self._worker is not None and self._worker.is_alive():
                return
            # daemon : ne doit jamais empecher le process de mourir. La livraison a
            # l'arret est garantie par `flush()`, pas par la survie du thread.
            self._worker = threading.Thread(target=self._boucle, name="mailer",
                                            daemon=True)
            self._worker.start()

    def _boucle(self):
        while True:
            message = self._file.get()
            try:
                if message is None:                 # sentinelle d'arret
                    return
                self._envoyer_avec_reessai(*message[:3])
            except Exception as e:                  # pragma: no cover - filet ultime
                log.warning("Worker email: %s", e)
            finally:
                self._file.task_done()

    def _envoyer_avec_reessai(self, sujet: str, corps: str, html: str):
        derniere = None
        for essai in range(1, ESSAIS + 1):
            try:
                self._send_sync(sujet, corps, html)
                self.envoyes += 1
                return
            except Exception as e:
                derniere = e
                if essai < ESSAIS:
                    attente = BACKOFF[min(essai - 1, len(BACKOFF) - 1)]
                    log.info("Email '%s' en echec (%s) — nouvelle tentative dans %.0f s "
                             "(%d/%d).", sujet, e, attente, essai, ESSAIS)
                    if _ARRET.wait(attente):        # arret demande : on n'insiste plus
                        break
        self.echoues += 1
        log.warning("Email NON ENVOYE apres %d tentative(s) (%s) : %s",
                    ESSAIS, derniere, sujet)

    def _send_sync(self, sujet: str, corps: str, html: str = ""):
        """Envoi SYNCHRONE. Leve en cas d'echec — c'est le reessai qui decide de la suite."""
        c = self.cfg
        msg = EmailMessage()
        nom, adresse = parseaddr(c.sender or c.user)
        msg["From"] = formataddr((nom or "Agent FTMO", adresse or c.user))
        msg["To"] = ", ".join(c.to)
        msg["Subject"] = sujet
        msg.set_content(corps)                       # repli texte
        if html:
            msg.add_alternative(html, subtype="html")
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

    def flush(self, seconds: float = 15.0):
        """Attend que la file soit vide (a appeler dans le `finally` de l'orchestrateur).

        Renvoie True si tout est parti. On borne l'attente : un serveur SMTP injoignable
        ne doit pas empecher le process de s'arreter."""
        worker = self._worker
        if worker is None or not worker.is_alive():
            return self._file.empty()
        try:
            # sentinelle EN FIN DE FILE : le worker envoie tout ce qui reste, puis sort.
            # C'est ce qui rend l'attente bornee et sans attente inutile une fois vide.
            self._file.put_nowait(None)
        except queue.Full:                          # pragma: no cover - file saturee
            log.warning("File pleine a l'arret : certains emails seront perdus.")
        worker.join(timeout=max(0.0, seconds))
        reste = self._file.qsize()
        if reste:
            log.warning("Arret : %d email(s) encore en file apres %.0f s — abandonnes.",
                        reste, seconds)
            return False
        return True

    # ------------------------------------------------------------------ portefeuille
    @staticmethod
    def portefeuille(summary: dict, positions: list) -> str:
        """Version TEXTE (repli) de l'etat du compte."""
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

    @staticmethod
    def portefeuille_html(summary: dict, positions: list) -> str:
        """Bloc visuel : 4 KPI, progression vers l'objectif, marge de perte du jour,
        puis le tableau des positions."""
        s = summary or {}
        objectif = s.get("objectif_etape_pct") or s.get("objectif_pct") or 0
        pnl = s.get("pnl_total_pct") or 0
        perte = s.get("perte_jour_pct") or 0
        stop = s.get("stop_jour_agent_pct") or 0
        avance = (float(pnl) / float(objectif) * 100) if objectif else 0
        conso = (float(perte) / float(stop) * 100) if stop else 0
        couleur_jour = ROUGE if conso >= 75 else (AMBRE if conso >= 40 else VERT)

        kpis = _rangee_kpis([
            _kpi("Equity", f"{_fmt(s.get('equity'))} $"),
            _kpi("PnL total", _signe(pnl, " %"), _couleur_valeur(pnl),
                 f"objectif +{_fmt(objectif, ' %', 0)}"),
            _kpi("Perte du jour", f"{_fmt(perte, ' %')}", couleur_jour,
                 f"stop agent {_fmt(stop, ' %', 0)}"),
            _kpi("Positions", str(len(positions or [])),
                 note=f"{s.get('trades_du_jour', '?')} trade(s) aujourd'hui"),
        ])
        progression = (
            _titre_section("Progression de l'etape")
            + f'<div class="mut" style="font:400 12px/1.5 {POLICE};color:{DOUX};">'
              f'Objectif : {_fmt(avance, " %", 0)} parcouru · '
              f'reste {_fmt(s.get("reste_avant_objectif_pct"), " %")}</div>'
            + _barre(avance, VERT if avance >= 0 else ROUGE)
            + f'<div class="mut" style="font:400 12px/1.5 {POLICE};color:{DOUX};margin-top:12px;">'
              f'Marge de perte du jour consommee : {_fmt(conso, " %", 0)}</div>'
            + _barre(conso, couleur_jour)
            + f'<div class="mut" style="font:400 11px/1.6 {POLICE};color:{DOUX};margin-top:10px;">'
              f'Etape {_e(s.get("etape", "?"))} · jour {_e(s.get("jours_ecoules", "?"))} · '
              f'jours trades {_e(s.get("jours_trades", "?"))}/'
              f'{_e(s.get("jours_trades_min", "?"))}</div>')
        return (_titre_section("Etat du portefeuille") + kpis + progression
                + _titre_section(f"Positions ouvertes ({len(positions or [])})")
                + _tableau_positions(positions))

    # ------------------------------------------------------------------ evenements
    def trade_ouvert(self, trade: dict, summary: dict, positions: list):
        if not (self.cfg.on_trade and self.cfg.ready):
            return
        d = trade
        sens = str(d.get("direction", "")).upper()
        titre = f"{sens} {d.get('symbol')} · {_fmt(d.get('lot'), ' lot', 2)}"
        texte = "\n".join([
            "POSITION OUVERTE",
            f"  {sens} {d.get('symbol')} — strategie {d.get('strategy')}",
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
        corps_html = (
            _rangee_kpis([
                _kpi("Risque engage", f"{_fmt(d.get('risk_dollars'))} $",
                     note=f"{_fmt(d.get('risk_pct'), ' %')} du compte"),
                _kpi("R:R net", _fmt(d.get("rr_net")), BLEU,
                     note=f"brut {_fmt(d.get('rr'))}"),
                _kpi("Stop", str(d.get("sl")), ROUGE),
                _kpi("Cible", str(d.get("tp")), VERT),
            ])
            + _titre_section("Execution")
            + _lignes_detail([
                ("Strategie", _e(d.get("strategy"))),
                ("Lot", _fmt(d.get("lot"), "", 2)),
                ("Entree reelle", _e(d.get("entry"))),
                ("Entree prevue", f'{_e(d.get("entry_planifiee"))} '
                                  f'<span style="color:{DOUX};font-weight:400">'
                                  f'(slippage {_fmt(d.get("slippage_pips"), " pips", 1)})</span>'),
                ("Spread a l'entree", _fmt(d.get("spread_pips_entree"), " pips", 1)),
                ("Couts estimes", f'{_fmt(d.get("couts_estimes"))} $'),
                ("Confiance", _fmt(d.get("confidence"), "", 2)),
            ])
            + _titre_section("Raisonnement")
            + _citation(str(d.get("rationale") or "(non precise)")[:1200])
            + self.portefeuille_html(summary, positions))
        achat = sens == "BUY"
        self.send(f"Ouverture {titre}", texte,
                  _document(accent=VERT if achat else ROUGE,
                            badge="Achat" if achat else "Vente",
                            badge_bg=VERT_BG if achat else ROUGE_BG,
                            titre=f"{d.get('symbol')} · {_fmt(d.get('lot'), ' lot', 2)}",
                            hero=f"{_fmt(d.get('risk_dollars'))} $ de risque",
                            hero_note=f"{_fmt(d.get('risk_pct'), ' %')} du compte · "
                                      f"R:R net {_fmt(d.get('rr_net'))} · "
                                      f"confiance {_fmt(d.get('confidence'), '', 2)}",
                            corps=corps_html))

    def trade_ferme(self, trade: dict, lecon: str, summary: dict, positions: list):
        if not (self.cfg.on_trade and self.cfg.ready):
            return
        R = trade.get("R", 0.0) or 0.0
        verdict = "GAIN" if R > 0 else ("PERTE" if R < 0 else "NEUTRE")
        couleur = VERT if R > 0 else (ROUGE if R < 0 else DOUX)
        titre = (f"{trade.get('symbol')} · {_signe(R, ' R')} "
                 f"({_signe(trade.get('pnl'), ' $')})")
        texte = "\n".join([
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
        corps_html = (
            _rangee_kpis([
                _kpi("Resultat", _signe(R, " R"), couleur),
                _kpi("PnL", _signe(trade.get("pnl"), " $"),
                     _couleur_valeur(trade.get("pnl"))),
                _kpi("MFE / MAE", f'{_fmt(trade.get("mfe_R"))} / {_fmt(trade.get("mae_R"))} R',
                     note="meilleur / pire point atteint"),
                _kpi("Duree", _fmt(trade.get("duree_h"), " h", 1),
                     note=f'plan {_fmt(trade.get("rr_planifie"))} R:R'),
            ])
            + _titre_section("Deroule du trade")
            + _lignes_detail([
                ("Sens", _e(str(trade.get("direction", "")).upper())),
                ("Strategie", _e(trade.get("strategy"))),
                ("Entree -> Sortie", f'{_e(trade.get("entry"))} &rarr; '
                                     f'{_e(trade.get("exit") or "(marche)")}'),
                ("R:R planifie", f'{_fmt(trade.get("rr_planifie"))} '
                                 f'<span style="color:{DOUX};font-weight:400">'
                                 f'(net {_fmt(trade.get("rr_net_planifie"))})</span>'),
                ("Regime", _e(trade.get("regime", "?"))),
            ])
            + _titre_section("Lecon retenue")
            + _citation(str(lecon or "(aucune)")[:1200])
            + self.portefeuille_html(summary, positions))
        badge_bg = VERT_BG if R > 0 else (ROUGE_BG if R < 0 else SLATE_BG)
        self.send(f"Cloture {trade.get('symbol')} — {verdict} {_fmt(R)} R "
                  f"({_fmt(trade.get('pnl'))} $)", texte,
                  _document(accent=couleur, badge=verdict, badge_bg=badge_bg,
                            titre=f"{trade.get('symbol')} · {trade.get('strategy')}",
                            hero=_signe(R, " R"), hero_couleur=couleur,
                            hero_note=f"{_signe(trade.get('pnl'), ' $')} · "
                                      f"{_fmt(trade.get('duree_h'), ' h', 1)} · "
                                      f"plan {_fmt(trade.get('rr_planifie'))} R:R",
                            corps=corps_html),
                  urgent=R <= -1.0)

    def alerte(self, titre: str, detail: str, summary: dict, positions: list,
               action_requise: str = ""):
        if not (self.cfg.on_alert and self.cfg.ready):
            return
        texte = "\n".join([
            titre.upper(),
            f"  {detail}",
            "",
            ("ACTION REQUISE\n  " + action_requise + "\n") if action_requise else "",
            self.portefeuille(summary, positions),
        ])
        corps_html = (
            f'<div class="ink" style="font:400 14px/1.65 {POLICE};color:{ENCRE};">'
            f'{_e(detail)}</div>'
            + (f'<div class="tile" style="margin-top:16px;padding:13px 15px;'
               f'background:{AMBRE_BG};border:1px solid {BORD};border-radius:12px;">'
               f'<div style="font:700 10px/1.5 {POLICE};letter-spacing:.08em;'
               f'color:{AMBRE};text-transform:uppercase;">Action requise</div>'
               f'<div class="ink" style="font:400 13px/1.6 {POLICE};color:{ENCRE};'
               f'margin-top:4px;">{_e(action_requise)}</div></div>' if action_requise else "")
            + self.portefeuille_html(summary, positions))
        s = summary or {}
        perte = s.get("perte_jour_pct")
        hero = _fmt(perte, " %") if perte is not None else ""
        hero_note = (f"perte du jour · stop agent {_fmt(s.get('stop_jour_agent_pct'), ' %', 0)}"
                     if perte is not None else "")
        self.send(titre, texte,
                  _document(accent=AMBRE, badge="Alerte", badge_bg=AMBRE_BG, titre=titre,
                            hero=hero, hero_couleur=AMBRE, hero_note=hero_note,
                            corps=corps_html),
                  urgent=True)
