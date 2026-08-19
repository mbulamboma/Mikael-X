# -*- coding: utf-8 -*-
"""Configuration centrale de l'agent trader FTMO.

Toutes les valeurs sensibles viennent de .env (jamais commite). Les constantes
FTMO sont les GARDE-FOUS DURS : le moteur de risque les fait respecter, le LLM
ne peut pas les contourner.
"""
from __future__ import annotations
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

#: Racine du DEPLOYABLE. Ce dossier se suffit a lui-meme : code, configuration, etat.
#: On le copie sur une machine, on remplit `.env`, il tourne. Rien au-dessus n'est requis.
ROOT = Path(__file__).resolve().parent
COMPANY = ROOT               # alias historique : l'entreprise EST le deployable

# UN SEUL `.env`, a la racine du dossier deploye. Il porte l'ORGANISATION (AGENT_MODE,
# DESK_*, EVAL_*) ET la MACHINE (MT5, Bedrock/AWS, SMTP, FTMO) — les deux sections sont
# separees et commentees dans `.env.example`.
# Une variable deja definie dans l'ENVIRONNEMENT l'emporte toujours sur le fichier
# (`EVAL_SHADOW=1 python run.py`), ce qui permet aussi de tout piloter par variables
# d'environnement dans un conteneur, sans `.env` du tout.
ENV_FILE = ROOT / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# Le paquet est importable meme lance depuis un autre repertoire de travail
# (`python /opt/ai-company/run.py`, service systemd, tache planifiee).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Etat local (SQLite, verrou d'instance). Redirigeable hors du dossier pour un
#: deploiement ou le code est en lecture seule : `AGENT_STATE_DIR=/var/lib/ai-company`.
STATE_DIR = Path(os.environ.get("AGENT_STATE_DIR", "").strip() or (ROOT / "state"))
STATE_DIR.mkdir(parents=True, exist_ok=True)


def _s(name: str, default: str = "") -> str:
    """Chaine d'environnement. Un PLACEHOLDER non remplace (`<votre_cle>`) vaut vide :
    mieux vaut une fonction desactivee qu'une authentification avec une fausse cle."""
    v = os.environ.get(name, default).strip()
    if v.startswith("<") and v.endswith(">"):
        return ""
    return v


def _f(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        if not math.isfinite(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
        if not math.isfinite(float(value)):
            return default
        return value
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class FTMOConfig:
    """Chassis FTMO 2 etapes. Pourcentages sur le SOLDE INITIAL (perte max) et sur
    le SOLDE DU JOUR (perte journaliere).

    Etape 1 (Challenge)     -> objectif +10 %
    Etape 2 (Verification)  -> objectif  +5 %
    Perte journaliere -5 % / perte totale -10 % (fixes) ; 30 jours ; min 4 jours de trading.
    """
    account_size: float = _f("FTMO_ACCOUNT_SIZE", 100_000.0)
    phase: int = _i("FTMO_PHASE", 1)                                # 1 = Challenge, 2 = Verification
    phase1_target_pct: float = _f("FTMO_PHASE1_TARGET_PCT", 10.0)
    phase2_target_pct: float = _f("FTMO_PHASE2_TARGET_PCT", 5.0)
    max_daily_loss_pct: float = _f("FTMO_MAX_DAILY_LOSS_PCT", 5.0)  # -5 % / jour = fail
    max_total_loss_pct: float = _f("FTMO_MAX_TOTAL_LOSS_PCT", 10.0) # -10 % total = fail
    phase_days: int = _i("FTMO_PHASE_DAYS", 30)
    min_trading_days: int = _i("FTMO_MIN_TRADING_DAYS", 4)
    phase_start: str = field(default_factory=lambda: os.environ.get("FTMO_PHASE_START", ""))

    # Garde-fous de l'AGENT (plus stricts que FTMO)
    daily_stop_pct: float = _f("AGENT_DAILY_STOP_PCT", 4.0)         # stop ouvertures a -4 % (marge)
    total_soft_stop_pct: float = _f("AGENT_TOTAL_SOFT_STOP_PCT", 7.0)
    risk_per_trade_pct: float = _f("AGENT_RISK_PER_TRADE_PCT", 1.0) # 1 % du solde / trade
    max_open_positions: int = _i("AGENT_MAX_OPEN_POSITIONS", 3)     # 2-3 max
    max_trades_per_day: int = _i("AGENT_MAX_TRADES_PER_DAY", 3)
    max_risk_per_symbol_pct: float = _f("AGENT_MAX_RISK_PER_SYMBOL_PCT", 1.0)
    cooldown_minutes_after_loss: int = _i("AGENT_COOLDOWN_MIN", 120)

    @property
    def profit_target_pct(self) -> float:
        """Objectif de profit de l'etape en cours (+10 % etape 1, +5 % etape 2)."""
        return self.phase1_target_pct if self.phase == 1 else self.phase2_target_pct


@dataclass(frozen=True)
class ExecutionConfig:
    """COUTS ET FRICTIONS REELS — ce qui separe un backtest naif d'un vrai trader.

    Le risque n'est pas seulement (entree - stop) : il faut payer le spread, la
    commission, subir le slippage, et supporter le swap si on tient plusieurs nuits
    (profil swing). Ces valeurs servent au dimensionnement ET aux vetos.

    ATTENTION : `commission_per_lot` doit correspondre a VOTRE broker (FTMO facture
    typiquement ~3 $/lot/cote sur le FX, soit ~6-7 $/lot aller-retour ; 0 sur indices).
    """
    # commission aller-retour par lot, en devise du compte
    commission_per_lot: float = _f("AGENT_COMMISSION_PER_LOT", 7.0)
    # slippage suppose a l'entree ET a la sortie (en pips), marge de securite du sizing
    slippage_pips: float = _f("AGENT_SLIPPAGE_PIPS", 1.0)
    # spread max tolere pour ENTRER (absolu et en fraction de l'ATR du timeframe)
    max_spread_pips: float = _f("AGENT_MAX_SPREAD_PIPS", 3.0)
    max_spread_atr_ratio: float = _f("AGENT_MAX_SPREAD_ATR", 0.12)
    # deviation acceptee a l'envoi de l'ordre (points) + nb de tentatives sur requote
    deviation_points: int = _i("AGENT_DEVIATION_POINTS", 20)
    order_retries: int = _i("AGENT_ORDER_RETRIES", 2)
    # part maximale de la marge libre engagee par une position
    max_margin_pct_of_free: float = _f("AGENT_MAX_MARGIN_PCT", 20.0)
    # RISQUE DE GAP : plus d'ouverture apres cette heure UTC le vendredi (week-end),
    # et provision de gap (en multiples d'ATR) rappelee au LLM pour le swing.
    friday_cutoff_utc: int = _i("AGENT_FRIDAY_CUTOFF_UTC", 20)
    weekend_guard: bool = field(default_factory=lambda:
                                os.environ.get("AGENT_WEEKEND_GUARD", "1") == "1")
    gap_provision_atr: float = _f("AGENT_GAP_PROVISION_ATR", 1.0)
    # Mise a plat avant le week-end : ferme TOUTES les positions au cutoff du vendredi.
    # Protege du gap du dimanche, mais coupe les swings gagnants -> a vous de trancher.
    weekend_flatten: bool = field(default_factory=lambda:
                                  os.environ.get("AGENT_WEEKEND_FLATTEN", "0") == "1")
    # N'agir QUE sur les positions de l'agent (magic). Les positions d'un autre EA
    # comptent dans le risque (elles pesent sur l'equity FTMO) mais on n'y touche jamais.
    magic: int = _i("AGENT_MAGIC", 770077)
    own_positions_only: bool = field(default_factory=lambda:
                                     os.environ.get("AGENT_OWN_POSITIONS_ONLY", "1") == "1")
    # Surveillance deterministe entre deux cycles LLM (protections seules, sans IA).
    watch_seconds: int = _i("AGENT_WATCH_SECONDS", 60)
    # Budget de risque par DEVISE (corrélation) : EURUSD + GBPUSD longs = meme pari dollar.
    max_risk_per_currency_pct: float = _f("AGENT_MAX_RISK_PER_CURRENCY_PCT", 2.0)
    # NIVEAU D'ENTREE PROPOSE PAR LE LLM vs PRIX REEL. Les ordres partent AU MARCHE :
    # l'`entry` du LLM ne sert qu'au SIZING et au R:R. S'il est loin du prix reel, le lot
    # calcule et le R:R annonce sont des fictions (risque reel different du budget).
    #  - `entry_reprice`        : recaler l'entree sur le prix executable avant le sizing ;
    #  - `max_entry_drift_atr`  : au-dela de N x ATR d'ecart, on REFUSE (niveau perime ou
    #                             invente : la these n'est plus celle qu'on executerait).
    entry_reprice: bool = field(default_factory=lambda:
                                os.environ.get("AGENT_ENTRY_REPRICE", "1") == "1")
    max_entry_drift_atr: float = _f("AGENT_MAX_ENTRY_DRIFT_ATR", 0.5)


@dataclass(frozen=True)
class SafeModeConfig:
    """PILOTE DE SECOURS 100 % DETERMINISTE (aucun LLM).

    Des que l'IA devient indisponible (Bedrock injoignable, credentials, quota,
    dependance manquante...), l'agent ne prend plus AUCUNE nouvelle position : un
    script Python dur prend la main, protege les positions en cours jusqu'a leur
    fermeture dans le respect des regles FTMO, puis le processus s'arrete pour
    inspection manuelle.
    """
    breakeven_at_r: float = _f("SAFE_BE_AT_R", 1.0)          # stop au break-even des +1R
    trail_atr_mult: float = _f("SAFE_TRAIL_ATR", 2.0)        # trailing 2 x ATR
    trail_activate_r: float = _f("SAFE_TRAIL_ACTIVATE_R", 1.0)
    missing_sl_atr: float = _f("SAFE_MISSING_SL_ATR", 1.5)   # SL d'urgence si aucun stop
    time_stop_days: float = _f("SAFE_TIME_STOP_DAYS", 10.0)  # position qui traine -> on sort
    time_stop_min_r: float = _f("SAFE_TIME_STOP_MIN_R", 0.3)
    # fraction du stop journalier agent a partir de laquelle on ferme TOUT
    panic_ratio: float = _f("SAFE_PANIC_RATIO", 0.75)
    # une fois a plat (plus aucune position), on arrete le script
    exit_when_flat: bool = field(default_factory=lambda:
                                 os.environ.get("SAFE_EXIT_WHEN_FLAT", "1") == "1")


@dataclass(frozen=True)
class MT5Config:
    login: int = _i("MT5_LOGIN", 0)
    password: str = field(default_factory=lambda: _s("MT5_PASSWORD"))
    server: str = field(default_factory=lambda: _s("MT5_SERVER"))
    path: str = field(default_factory=lambda: _s("MT5_PATH"))  # optionnel


@dataclass(frozen=True)
class NewsConfig:
    """Flux d'actualite pour un trader swing : calendrier economique MT5, donnees
    Reserve federale (FRED) et titres d'actualite (GDELT), plus le brain macro
    par devise (`macro_features.csv` produit par tools/macro_service.py)."""
    enabled: bool = field(default_factory=lambda: os.environ.get("NEWS_ENABLED", "1") == "1")
    # Dossier MQL5\Files du terminal MT5 (calendar_history.csv, macro_features.csv).
    mt5_files: str = field(default_factory=lambda: os.environ.get("MT5_FILES", ""))
    fred_key: str = field(default_factory=lambda: _s("FRED_API"))
    use_gdelt: bool = field(default_factory=lambda: os.environ.get("NEWS_GDELT", "1") == "1")
    # Calendrier WEB (faireconomy/ForexFactory) : remplace l'export MT5 ExportCalendar.mq5.
    # Meme donnee que la regle news FTMO. Utilise quand aucun calendar_history.csv n'existe.
    use_web_calendar: bool = field(default_factory=lambda:
                                   os.environ.get("NEWS_WEB_CALENDAR", "1") == "1")
    # Fenetre "black-out" : pas de NOUVELLE entree si un event a fort impact touche
    # une devise du symbole dans +/- ces minutes (regle FTMO : 60 min avant news).
    blackout_min: int = _i("NEWS_BLACKOUT_MIN", 60)
    recent_hours: int = _i("NEWS_RECENT_HOURS", 72)      # surprises recentes prises en compte
    upcoming_hours: int = _i("NEWS_UPCOMING_HOURS", 72)  # horizon des events a venir
    cache_min: int = _i("NEWS_CACHE_MIN", 30)            # TTL du cache news (swing = lent)
    min_importance: int = _i("NEWS_MIN_IMPORTANCE", 2)   # 2=moyen, 3=fort
    # 1 = AUCUNE entree tant que le calendrier economique n'est pas lisible (fail-closed).
    # Par defaut 0 : on log une ERREUR bruyante mais on continue de trader.
    fail_closed: bool = field(default_factory=lambda:
                              os.environ.get("NEWS_FAIL_CLOSED", "0") == "1")


@dataclass(frozen=True)
class WebConfig:
    """Recherche et lecture de pages web (analyse macro / d'expert).

    L'agent peut chercher (Google CSE si cle fournie, sinon DuckDuckGo) et LIRE des
    pages publiques (banques centrales, myfxbook, medias financiers) pour approfondir
    son analyse. Garde-fous : budget d'appels par cycle, taille de page plafonnee,
    timeout, filtrage des adresses internes, liste blanche/noire de domaines.
    """
    enabled: bool = field(default_factory=lambda: os.environ.get("WEB_ENABLED", "1") == "1")
    timeout: int = _i("WEB_TIMEOUT", 20)
    max_chars: int = _i("WEB_MAX_CHARS", 6000)       # texte extrait par page
    max_calls_per_cycle: int = _i("WEB_MAX_CALLS", 12)
    max_parallel: int = _i("WEB_MAX_PARALLEL", 3)     # appels web parallèles simultanés
    cache_min: int = _i("WEB_CACHE_MIN", 20)
    # UA de navigateur : beaucoup de sites refusent les UA inconnus. Certains sites
    # interdisent l'acces automatise dans leurs CGU — privilegier les API officielles.
    user_agent: str = field(default_factory=lambda: os.environ.get(
        "WEB_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"))
    # Vide = tout le web autorise (hors adresses internes et domaines bloques).
    allow_domains: tuple[str, ...] = field(default_factory=lambda: tuple(
        d.strip().lower() for d in os.environ.get("WEB_ALLOW_DOMAINS", "").split(",") if d.strip()))
    deny_domains: tuple[str, ...] = field(default_factory=lambda: tuple(
        d.strip().lower() for d in os.environ.get("WEB_DENY_DOMAINS", "").split(",") if d.strip()))
    # Moteur de recherche : Google Custom Search si les deux cles sont presentes.
    google_key: str = field(default_factory=lambda: _s("GOOGLE_API_KEY"))
    google_cse: str = field(default_factory=lambda: _s("GOOGLE_CSE_ID"))
    # Sentiment retail myfxbook : la page publique est protegee (403). L'API officielle
    # demande un compte -> renseignez VOS identifiants dans .env pour l'activer.
    myfxbook_email: str = field(default_factory=lambda: _s("MYFXBOOK_EMAIL"))
    myfxbook_password: str = field(default_factory=lambda: _s("MYFXBOOK_PASSWORD"))


@dataclass(frozen=True)
class SourcesConfig:
    """SOURCES DE DONNEES PLUGGABLES (cf. data/sources.py) — Marche / Social / News /
    Fondamentaux, a l'image de TradingAgents. Tout est OPT-IN, FAIL-CLOSED et ASSAINI.

    Rien n'est actif par defaut : une source ne s'allume qu'avec sa cle (.env) ou son
    drapeau. Les sources textuelles (Reddit, RSS, news) sont traitees comme des donnees
    hostiles (data/sanitize.py) : agregats chiffres + extraits neutralises seulement.
    """
    # Reddit : JSON public, SANS cle. Drapeau explicite car c'est une source non fiable.
    reddit_enabled: bool = field(default_factory=lambda:
                                 os.environ.get("SOURCES_REDDIT", "0") == "1")
    reddit_subs: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in os.environ.get("SOURCES_REDDIT_SUBS", "Forex,wallstreetbets").split(",")
        if s.strip()))
    # RSS : flux libres (Reuters, banques centrales, medias). URLs separees par des virgules.
    rss_feeds: tuple[str, ...] = field(default_factory=lambda: tuple(
        u.strip() for u in os.environ.get("SOURCES_RSS", "").split(",") if u.strip()))
    # Cles API (paliers gratuits) : activent news/social/fondamentaux quand renseignees.
    finnhub_key: str = field(default_factory=lambda: _s("FINNHUB_API_KEY"))
    eodhd_key: str = field(default_factory=lambda: _s("EODHD_API_KEY"))
    # FXSSI : sentiment retail long/short SANS compte (alternative a myfxbook). Best-effort
    # (page publique) et fail-closed. Sert de repli quand myfxbook n'est pas configure.
    fxssi_enabled: bool = field(default_factory=lambda:
                                os.environ.get("SOURCES_FXSSI", "0") == "1")
    # Fusion des sources dans les dossiers des analystes (Fondamental/Actualite).
    inject_fundamentals: bool = field(default_factory=lambda:
                                      os.environ.get("SOURCES_FUNDAMENTALS", "1") == "1")
    inject_news: bool = field(default_factory=lambda:
                              os.environ.get("SOURCES_NEWS", "1") == "1")


@dataclass(frozen=True)
class MailConfig:
    """Notifications par email : ouverture/cloture de trade, urgences, arret du script.

    L'envoi ne doit JAMAIS bloquer ni casser la boucle de trading : il part dans un
    thread, avec timeout, et toute erreur est simplement journalisee.
    """
    enabled: bool = field(default_factory=lambda: os.environ.get("MAIL_ENABLED", "1") == "1")
    host: str = field(default_factory=lambda: _s("MAIL_HOST"))
    port: int = _i("MAIL_PORT", 465)
    secure: str = field(default_factory=lambda:
                        os.environ.get("MAIL_SECURE", "").strip().lower())
    user: str = field(default_factory=lambda: _s("MAIL_USER"))
    password: str = field(default_factory=lambda: _s("MAIL_PASSWORD"))
    sender: str = field(default_factory=lambda: _s("MAIL_FROM"))
    to: tuple[str, ...] = field(default_factory=lambda: tuple(
        a.strip() for a in _s("MAIL_TO").split(",") if a.strip()))
    on_trade: bool = field(default_factory=lambda: os.environ.get("MAIL_ON_TRADE", "1") == "1")
    on_alert: bool = field(default_factory=lambda: os.environ.get("MAIL_ON_ALERT", "1") == "1")
    timeout: int = _i("MAIL_TIMEOUT", 20)

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.host and self.to)

    @property
    def mode(self) -> str:
        """ssl | tls | none — deduit du port si MAIL_SECURE n'est pas explicite."""
        if self.secure in ("ssl", "smtps"):
            return "ssl"
        if self.secure in ("tls", "starttls"):
            return "tls"
        if self.secure in ("none", "false", "0", "no"):
            return "none"
        if self.secure in ("true", "1", "yes"):
            return "ssl" if self.port == 465 else "tls"
        return "ssl" if self.port == 465 else ("tls" if self.port == 587 else "none")


@dataclass(frozen=True)
class BedrockConfig:
    """Modeles servis par AWS Bedrock (API Converse via langchain_aws).

    ATTENTION : sur Bedrock, l'ID modele est un *inference profile* qui depend de
    la region/compte et de l'acces active dans la console Bedrock. Le format usuel
    est `us.<provider>.<modele>-v1:0` (prefixe region `us.`/`eu.`/`apac.`). Lister :
        aws bedrock list-inference-profiles --region <region>
        aws bedrock list-foundation-models --region <region>

    AUTHENTIFICATION : soit la chaine boto3 classique (AWS_PROFILE / cles / role IAM),
    soit une **cle API Bedrock** (`BEDROCK_API_KEY`) — un jeton porteur que boto3 lit
    dans `AWS_BEARER_TOKEN_BEDROCK` ; on le positionne automatiquement au demarrage.

    OUTILS : tous les modeles Bedrock ne savent PAS appeler des outils (tool calling).
    DeepSeek-R1 notamment expose Converse mais pas le tool use. `tool_mode` :
      "auto"  -> essaie les outils, bascule en mode JSON si le modele ne suit pas,
      "tools" -> force le tool calling (Claude, Nova, Mistral Large...),
      "json"  -> force le mode JSON (contexte injecte, reponse = plan d'actions JSON).
    """
    model_id: str = field(default_factory=lambda: os.environ.get(
        "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"))
    region: str = field(default_factory=lambda: os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")))
    # Credentials : laisses vides -> chaine boto3 par defaut (env, ~/.aws, role IAM).
    aws_profile: str = field(default_factory=lambda: os.environ.get("AWS_PROFILE", ""))
    # Cle API Bedrock (jeton porteur) : alternative aux credentials IAM.
    api_key: str = field(default_factory=lambda:
                         _s("BEDROCK_API_KEY") or _s("AWS_BEARER_TOKEN_BEDROCK"))
    tool_mode: str = field(default_factory=lambda:
                           os.environ.get("BEDROCK_TOOL_MODE", "auto").strip().lower())

    def __post_init__(self):
        # boto3/botocore lisent le jeton porteur dans cette variable d'environnement.
        if self.api_key and not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = self.api_key

    @property
    def supports_tools(self) -> bool:
        """Heuristique : familles connues pour NE PAS supporter le tool calling."""
        mid = self.model_id.lower()
        return not any(p in mid for p in ("deepseek", "titan-text", "llama3-8b", "jamba-instruct"))


@dataclass(frozen=True)
class DeskConfig:
    """DESK MULTI-AGENTS — l'agent unique devient une *entreprise* de rôles specialises
    qui collaborent, se challengent et apprennent (cf. ai-company/IMPLEMENTATION.md).

    L'orchestrateur choisit le cerveau via `mode` :
      "desk" (defaut) -> Gerant + analystes + debat Bull/Bear + Trader + Risk Manager +
                         Trade Manager, chacun un rol distinct ;
      "solo"          -> agent unique historique (brain/agent.py), garde comme REPLI.

    IMPORTANT : quel que soit le mode, le moteur de risque FTMO deterministe (risk/ftmo.py)
    reste le plancher non negociable. Le Risk Manager LLM ne fait que DURCIR.
    """
    mode: str = field(default_factory=lambda:
                      (os.environ.get("AGENT_MODE", "desk").strip().lower() or "desk"))
    # C'est le GERANT qui convoque le desk complet (analystes + debat) a la demande ;
    # au repos, seul le book est gere. Ces bascules permettent de couper une brique.
    use_analysts: bool = field(default_factory=lambda: os.environ.get("DESK_USE_ANALYSTS", "1") == "1")
    use_debate: bool = field(default_factory=lambda: os.environ.get("DESK_USE_DEBATE", "1") == "1")
    # Risque en DEBAT (agressif/neutre/prudent + arbitrage du DG) plutot qu'un officier
    # unique dont l'humeur devient la politique de la maison. 0 = Risk Manager mono-voix.
    use_risk_debate: bool = field(default_factory=lambda:
                                  os.environ.get("DESK_RISK_DEBATE", "1") == "1")
    # DEBAT ADAPTATIF : `debate_rounds` est un MAXIMUM. Le 2e tour n'a lieu que si le debat
    # est serre (ecart de conviction <= `debate_gap`) — payer une relance quand un camp
    # ecrase l'autre n'apprend rien.
    debate_rounds: int = _i("DESK_DEBATE_ROUNDS", 2)
    debate_gap: float = _f("DESK_DEBATE_GAP", 0.2)
    # ANTI-SPECULATION (cf. desk/preuves.py) : une affirmation qui ne cite aucune donnee du
    # dossier est ecartee, un camp sans preuve perd sa conviction, un verdict non fonde
    # devient une abstention et une ouverture dont la these n'est pas sourcee est supprimee.
    # Sens unique assume : le doute n'a pas besoin de preuve, la prise de risque, si.
    # 0 = filtre desactive (les prompts continuent d'exiger des chiffres).
    exiger_preuves: bool = field(default_factory=lambda:
                                 os.environ.get("DESK_EXIGER_PREUVES", "1") == "1")
    # nb max de symboles envoyes au desk complet par cycle (borne le cout LLM)
    max_candidates: int = _i("DESK_MAX_CANDIDATS", 2)
    # tokens/temperature propres aux agents du desk (reponses courtes et structurees)
    max_tokens: int = _i("DESK_MAX_TOKENS", 1500)
    temperature: float = _f("DESK_TEMPERATURE", 0.2)

    # VIGIE DES POSITIONS : surveille les trades ouverts EN CONTINU (au-dela du watchdog
    # deterministe), alerte le DG quand un trade se degrade, et peut declencher une
    # SESSION EXTRAORDINAIRE (arreter/modifier le trade). Conditionnee + rate-limitee.
    vigie_enabled: bool = field(default_factory=lambda: os.environ.get("DESK_VIGIE_ENABLED", "1") == "1")
    vigie_alert_r: float = _f("DESK_VIGIE_ALERT_R", -0.6)      # perte flottante (R) declencheuse
    vigie_giveback_r: float = _f("DESK_VIGIE_GIVEBACK_R", 1.0) # MFE atteint -> "gains rendus"
    vigie_min_minutes: int = _i("DESK_VIGIE_MIN_MINUTES", 30)  # anti-spam par ticket

    # TRACING PAS-A-PAS (cf. desk/trace.py) : chaque appel LLM du desk est journalise
    # (rol, modele, latence, taille, ok/erreur) dans state/traces/<cycle>.jsonl. C'est la
    # debuggabilite qu'un graphe LangGraph offrirait, sans en adopter le framework.
    trace_enabled: bool = field(default_factory=lambda: os.environ.get("DESK_TRACE", "1") == "1")
    # CHECKPOINT DE CYCLE : les artefacts d'un cycle (mandat, briefs, debat) sont persistes ;
    # un redemarrage DANS le meme cycle reutilise les etapes deja faites au lieu de tout
    # refaire (et de repayer les appels LLM). 0 = pas de reprise.
    checkpoint_enabled: bool = field(default_factory=lambda: os.environ.get("DESK_CHECKPOINT", "1") == "1")
    # ANALYSTES TOOL-CAPABLES (cf. desk/analysts.py) : quand le modele sait appeler des outils
    # (Claude, Nova...), chaque analyste peut aller CHERCHER une donnee non pre-chargee via un
    # sous-ensemble d'outils LECTURE SEULE propre a son metier, avant de rendre son brief.
    # Repli automatique sur le dossier pre-charge si le modele ne sait pas (DeepSeek) ou echoue.
    analystes_outils: bool = field(default_factory=lambda: os.environ.get("DESK_ANALYSTES_OUTILS", "0") == "1")
    analystes_outils_max_iter: int = _i("DESK_ANALYSTES_OUTILS_ITER", 3)
    # REFLEXION HYBRIDE (cf. desk/reflexion.py) : a la cloture, une note QUALITATIVE bornee et
    # SOURCEE (contrainte de citer les faits du trade) complete les stats calculees ; les
    # reflexions des cas passes les plus SIMILAIRES sont ensuite injectees au Trader.
    reflexion_enabled: bool = field(default_factory=lambda: os.environ.get("DESK_REFLEXION", "1") == "1")
    reflexion_k: int = _i("DESK_REFLEXION_K", 3)
    # SENTIMENT SOCIAL (cf. desk/social.py) : source optionnelle (StockTwits/Reddit...) pour
    # l'analyste Sentiment. DESACTIVE par defaut : c'est du texte NON FIABLE (surface
    # d'injection de prompt). Quand active, il est ASSAINI et reduit a des agregats chiffres.
    sentiment_social: bool = field(default_factory=lambda: os.environ.get("DESK_SENTIMENT_SOCIAL", "0") == "1")

    # MODELES A DEUX VITESSES (optionnel). Trois niveaux, du plus general au plus precis :
    #   1. `BEDROCK_MODEL_ID`  — le modele partage, defaut de tout le monde ;
    #   2. `DESK_MODEL_RAPIDE` / `DESK_MODEL_FORT` — par CLASSE de role : rapide pour ceux
    #      qu'on appelle a chaque cycle (Gerant, Trade Manager, Vigie), fort pour ceux qui
    #      raisonnent rarement mais lourdement (Trader, analystes, debat, risque) ;
    #   3. `DESK_MODEL_<ROLE>` — override individuel, gagne sur tout le reste.
    # C'est ce qui finance les tours de debat : payer un gros modele pour dire « rien a
    # signaler » 24 fois par jour n'a aucun sens.
    model_rapide: str = field(default_factory=lambda: _s("DESK_MODEL_RAPIDE"))
    model_fort: str = field(default_factory=lambda: _s("DESK_MODEL_FORT"))
    model_gerant: str = field(default_factory=lambda: _s("DESK_MODEL_GERANT"))
    model_trader: str = field(default_factory=lambda: _s("DESK_MODEL_TRADER"))
    model_risk: str = field(default_factory=lambda: _s("DESK_MODEL_RISK"))
    model_analyste: str = field(default_factory=lambda: _s("DESK_MODEL_ANALYSTE"))
    model_debat: str = field(default_factory=lambda: _s("DESK_MODEL_DEBAT"))
    model_suivi: str = field(default_factory=lambda: _s("DESK_MODEL_SUIVI"))
    model_vigie: str = field(default_factory=lambda: _s("DESK_MODEL_VIGIE"))

    #: roles appeles a CHAQUE cycle (ou plus souvent) -> classe "rapide" ; les autres
    #: raisonnent rarement et profitent d'un modele fort.
    ROLES_RAPIDES = ("gerant", "suivi", "vigie")

    def model_for(self, role: str, fallback: str) -> str:
        """Modele d'un rol : override individuel > classe rapide/fort > modele partage."""
        individuel = {
            "gerant": self.model_gerant, "trader": self.model_trader,
            "risk": self.model_risk, "analyste": self.model_analyste,
            "debat": self.model_debat, "suivi": self.model_suivi,
            "vigie": self.model_vigie,
        }.get(role, "")
        classe = self.model_rapide if role in self.ROLES_RAPIDES else self.model_fort
        return individuel or classe or fallback


@dataclass(frozen=True)
class EvalConfig:
    """MESURE — on ne croit pas un cerveau sur parole, on l'observe.

    Empiler des agents LLM sans instrument de mesure augmente la variance, pas la
    qualite. Trois leviers, tous inoffensifs pour le trading reel :

    - `journal_cycles` : chaque cycle enregistre son DOSSIER D'ENTREE complet (compte,
      scan, charts, news, bilan) + le plan rendu par le cerveau. C'est la
      matiere premiere du rejeu hors-ligne (`desk/replay.py`) : sans ca, impossible de
      comparer solo vs desk sur les MEMES donnees.
    - `shadow` : MODE OMBRE. Le cerveau decide et son plan est journalise, mais AUCUNE
      de ses actions n'est executee. Les protections deterministes (trailing, stop
      d'urgence, panique perte-jour, garde week-end) continuent de tourner normalement.
      Sert a observer un nouveau cerveau en conditions reelles sans lui confier l'argent.
    - `journal_keep` : rotation (nb de cycles conserves), pour borner la taille de la base.
    """
    shadow: bool = field(default_factory=lambda: os.environ.get("EVAL_SHADOW", "0") == "1")
    journal_cycles: bool = field(default_factory=lambda:
                                 os.environ.get("EVAL_JOURNAL_CYCLES", "1") == "1")
    journal_keep: int = _i("EVAL_JOURNAL_KEEP", 500)
    # bougies max parcourues en aval d'une decision pour simuler son issue (rejeu)
    replay_max_bars: int = _i("EVAL_REPLAY_MAX_BARS", 30)
    # Retention du journal OPERATIONNEL (plans, modifications, blocages...). Les
    # evenements qui font la memoire du compte (trades clotures, ordres, vetos,
    # urgences) ne sont JAMAIS purges — cf. Store.KINDS_PERMANENTS. 0 = ne rien purger.
    event_retention_days: int = _i("AGENT_EVENT_RETENTION_DAYS", 90)


@dataclass(frozen=True)
class AgentConfig:
    # LLM (AWS Bedrock)
    bedrock: BedrockConfig = field(default_factory=BedrockConfig)
    temperature: float = _f("AGENT_TEMPERATURE", 0.2)
    max_tokens: int = _i("AGENT_MAX_TOKENS", 2000)
    # nb max d'appels d'outils par cycle (il explore : symboles, charts, indicateurs,
    # news, series FRED, pages web) — le budget web a sa propre limite (WEB_MAX_CALLS)
    max_steps: int = _i("AGENT_MAX_STEPS", 24)

    # Boucle de trading — profil SWING (tenue de position sur plusieurs jours) :
    # structure journaliere D1, cycle horaire (pas de bruit intraday).
    # `symbols` = WATCHLIST pre-scannee a chaque cycle (point de depart du raisonnement).
    # Ce n'est PAS une limite : l'agent peut lister l'univers du broker (list_symbols) et
    # analyser/trader n'importe quel symbole negociable, charge a la demande.
    symbols: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip().upper() for s in os.environ.get(
            "AGENT_SYMBOLS", "EURUSD,GBPUSD,USDJPY,XAUUSD").split(",") if s.strip()))
    timeframe: str = field(default_factory=lambda: os.environ.get("AGENT_TIMEFRAME", "D1"))
    # timeframes du chart pre-charge (du plus grand au plus petit) ; l'agent peut en
    # demander d'autres a la volee via get_chart(timeframes=...).
    chart_timeframes: tuple[str, ...] = field(default_factory=lambda: tuple(
        t.strip().upper() for t in os.environ.get(
            "AGENT_CHART_TFS", "W1,D1,H4").split(",") if t.strip()))
    loop_seconds: int = _i("AGENT_LOOP_SECONDS", 3600)          # 1 h (swing)

    # EXECUTION DIRECTE : l'agent passe TOUJOURS des ordres reels via MT5.
    # (Plus de mode dry-run.) La seule protection est le moteur de risque FTMO.

    ftmo: FTMOConfig = field(default_factory=FTMOConfig)
    mt5: MT5Config = field(default_factory=MT5Config)
    news: NewsConfig = field(default_factory=NewsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    safe: SafeModeConfig = field(default_factory=SafeModeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    desk: DeskConfig = field(default_factory=DeskConfig)
    eval: EvalConfig = field(default_factory=EvalConfig)


CFG = AgentConfig()
