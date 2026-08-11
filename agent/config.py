# -*- coding: utf-8 -*-
"""Configuration centrale de l'agent trader FTMO.

Toutes les valeurs sensibles viennent de .env (jamais commite). Les constantes
FTMO sont les GARDE-FOUS DURS : le moteur de risque les fait respecter, le LLM
ne peut pas les contourner.
"""
from __future__ import annotations
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
TRADING = ROOT.parent
load_dotenv(TRADING / ".env")          # reutilise le .env deja present a la racine

STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(exist_ok=True)


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
    password: str = field(default_factory=lambda: os.environ.get("MT5_PASSWORD", ""))
    server: str = field(default_factory=lambda: os.environ.get("MT5_SERVER", ""))
    path: str = field(default_factory=lambda: os.environ.get("MT5_PATH", ""))  # optionnel


@dataclass(frozen=True)
class NewsConfig:
    """Flux d'actualite pour un trader swing : calendrier economique MT5, donnees
    Reserve federale (FRED) et titres d'actualite (GDELT), plus le brain macro
    par devise (`macro_features.csv` produit par v4_macro/macro_service.py)."""
    enabled: bool = field(default_factory=lambda: os.environ.get("NEWS_ENABLED", "1") == "1")
    # Dossier MQL5\Files du terminal MT5 (calendar_history.csv, macro_features.csv).
    mt5_files: str = field(default_factory=lambda: os.environ.get("MT5_FILES", ""))
    fred_key: str = field(default_factory=lambda: os.environ.get("FRED_API", ""))
    use_gdelt: bool = field(default_factory=lambda: os.environ.get("NEWS_GDELT", "1") == "1")
    # Fenetre "black-out" : pas de NOUVELLE entree si un event a fort impact touche
    # une devise du symbole dans +/- ces minutes (regle FTMO : 60 min avant news).
    blackout_min: int = _i("NEWS_BLACKOUT_MIN", 60)
    recent_hours: int = _i("NEWS_RECENT_HOURS", 72)      # surprises recentes prises en compte
    upcoming_hours: int = _i("NEWS_UPCOMING_HOURS", 72)  # horizon des events a venir
    cache_min: int = _i("NEWS_CACHE_MIN", 30)            # TTL du cache news (swing = lent)
    min_importance: int = _i("NEWS_MIN_IMPORTANCE", 2)   # 2=moyen, 3=fort


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
    google_key: str = field(default_factory=lambda: os.environ.get("GOOGLE_API_KEY", ""))
    google_cse: str = field(default_factory=lambda: os.environ.get("GOOGLE_CSE_ID", ""))
    # Sentiment retail myfxbook : la page publique est protegee (403). L'API officielle
    # demande un compte -> renseignez VOS identifiants dans .env pour l'activer.
    myfxbook_email: str = field(default_factory=lambda: os.environ.get("MYFXBOOK_EMAIL", ""))
    myfxbook_password: str = field(default_factory=lambda: os.environ.get("MYFXBOOK_PASSWORD", ""))


@dataclass(frozen=True)
class BedrockConfig:
    """Modeles servis par AWS Bedrock (API Converse via langchain_aws).

    ATTENTION : sur Bedrock, l'ID modele est un *inference profile* qui depend de
    la region/compte et de l'acces active dans la console Bedrock. Le format usuel
    est `us.anthropic.claude-...-v1:0` (prefixe region `us.`/`eu.`/`apac.` pour
    l'inference cross-region). Lister les IDs disponibles :
        aws bedrock list-inference-profiles --region <region>
        aws bedrock list-foundation-models --by-provider anthropic --region <region>
    """
    model_id: str = field(default_factory=lambda: os.environ.get(
        "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"))
    region: str = field(default_factory=lambda: os.environ.get(
        "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")))
    # Credentials : laisses vides -> chaine boto3 par defaut (env, ~/.aws, role IAM).
    aws_profile: str = field(default_factory=lambda: os.environ.get("AWS_PROFILE", ""))


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
    safe: SafeModeConfig = field(default_factory=SafeModeConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)


CFG = AgentConfig()
