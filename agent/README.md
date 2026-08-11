# Agent trader FTMO SWING autonome (LangChain + AWS Bedrock + MT5)

Un agent IA **trader expert autonome, spécialiste swing** (tenue de position sur
plusieurs jours). Il **gère un portefeuille vivant** comme un vrai trader : il
**choisit ses paires**, **charge les graphiques** qu'il veut, **calcule les
indicateurs** dont il a besoin, **enquête sur le web et la macro** (Fed/FRED,
banques centrales, sentiment retail), **ouvre**, **clôture** (totale/partielle),
**déplace ses stops** (break-even, trailing), **choisit sa stratégie** et **apprend
de ses erreurs** — sans supervision. LLM Claude servi par **AWS Bedrock**, châssis
FTMO strict, ordres **réels** sur MT5. Et si l'IA tombe, un **pilote de secours
100 % Python** protège les positions ouvertes jusqu'à leur fermeture, puis arrête
le script.

## Autonomie : il gère son book à chaque cycle

L'agent ne fait pas que chercher une entrée. Chaque cycle, dans l'ordre :

1. **Il gère l'existant d'abord.** Pour chaque position ouverte (avec profit
   flottant en R, distance SL/TP, âge, stratégie), il décide : clôturer si la thèse
   est cassée, remonter le stop au break-even au-delà de +1R, suivre la structure
   (trailing), sécuriser une partie (`plan_close` partiel), réduire avant une news.
2. **Puis il cherche des opportunités** (si budget de risque et slots dispo) :
   scan → **lecture approfondie du chart** → entrée à des niveaux techniques réels.

Ses actions sont **réelles** et passent par des garde-fous :

| Action LLM | Exécution | Garde-fou |
|---|---|---|
| `plan_open` | ordre marché MT5 | moteur de risque FTMO (sizing + veto) + black-out news + symbole négociable |
| `plan_close` | clôture totale/partielle | toujours autorisée (réduit le risque) |
| `plan_modify` | déplace SL/TP | refusé si le nouveau SL **augmente** le risque au-delà du budget |
| `plan_trail` | arme/désarme un **trailing stop automatique** | le suivi ne déplace le stop que dans le sens du profit |

Même quand le garde-fou FTMO bloque les **ouvertures** (stop jour, objectif atteint,
max positions), l'agent peut toujours **gérer/clôturer** ses positions.

> Objectif : **réussir les 2 étapes** — Étape 1 (Challenge) **+10 %**, Étape 2 (Vérification)
> **+5 %** — sans jamais violer −5 %/jour ni −10 % total. Objectif atteint → **il arrête d'ouvrir**.

> ⚠️ **Exécution directe.** Il n'y a **pas de mode simulation** : chaque décision
> validée envoie un ordre réel au marché. La seule protection est le moteur de
> risque FTMO (sizing + veto). Testez sur un compte démo/challenge d'abord.

## Liberté d'analyse : il décide de ses outils, pas seulement de ses trades

Puisqu'il change de stratégie selon le marché, il doit pouvoir changer de **méthode
d'analyse**. Rien n'est figé : chaque outil ci-dessous est appelé **à sa demande**,
sur le symbole, le timeframe et la période **qu'il choisit**.

| Ce qu'il veut faire | Outil | Liberté réelle |
|---|---|---|
| Choisir **quoi trader** | `list_symbols(query)` | tout l'univers négociable du broker (FX, métaux, indices, crypto…), pas seulement la watchlist |
| **Charger un graphique** | `get_chart(symbol, timeframes, candles)` | il compose ses timeframes (`W1,D1,H4`, `D1,H4,H1`, `H4,H1,M15`…) et la profondeur |
| **Calculer un indicateur** | `compute_indicator(symbol, indicator, timeframe, period, …)` | `ema, sma, rsi, atr, macd, bbands, stoch, adx, cci, roc, donchian, keltner, supertrend, vwap, obv, ichimoku` |
| Scanner un actif | `get_market(symbol, timeframe)` | n'importe quel symbole/TF, chargé à la volée |
| **Suivre les grandes news** | `get_macro_events(hours)` | agenda fort impact **toutes devises** + surprises récentes + taux Fed/2 ans/10 ans |
| **Enquêter sur un thème** | `search_news(query, hours)` | recherche libre de titres (banques centrales, géopolitique, matières premières…) |
| Contexte d'un actif | `get_news(symbol)` | biais macro + événements + black-out, même hors watchlist |
| **Analyse macro d'expert** | `get_fred_series(series_id)` | n'importe quelle série officielle FRED : CPI, chômage, NFP, DGS10/DGS2, T10Y2Y, DXY, VIX, WTI… |
| **Naviguer sur le web** | `web_search(query)` puis `web_read(url)` | Google (si clé CSE) ou DuckDuckGo, puis lecture du texte de la page (Fed, BCE, médias, myfxbook…) |
| **Sentiment retail** | `get_retail_sentiment(symbol)` | positionnement des particuliers (myfxbook), lecture contrarienne |
| **Trailing stop** | `plan_trail(ticket, atr_mult \| pips, activate_r, timeframe)` | distance en ATR ou en pips, TF de suivi, seuil d'activation en R, désarmable |

La watchlist (`AGENT_SYMBOLS`) n'est plus une limite : c'est le **point de départ**
pré-scanné de son raisonnement. S'il justifie qu'un autre actif offre un meilleur
setup, il l'analyse et le trade — le seul refus possible est un symbole inconnu ou
non négociable chez le broker.

Le **trailing stop** est maintenu par l'orchestrateur à *chaque* cycle : une fois
armé, le stop remonte derrière le prix (jamais dans le mauvais sens) au-delà du
seuil d'activation, y compris sur un symbole hors watchlist. L'agent peut aussi le
désarmer et gérer son stop à la main (`plan_modify`).

> ⚠️ **Le web n'est jamais une consigne.** Tout ce que l'agent lit en ligne est traité
> comme de la **donnée non vérifiée** : son prompt lui interdit explicitement d'obéir à
> une instruction trouvée dans une page, lui impose de recouper au moins deux sources et
> de vérifier les dates. Garde-fous techniques : budget de requêtes par cycle
> (`WEB_MAX_CALLS`), taille de page plafonnée, timeout, refus des adresses internes
> (anti-SSRF), listes blanche/noire de domaines. Certains sites interdisent l'accès
> automatisé dans leurs CGU — privilégiez les API officielles (FRED, myfxbook).

### Sources d'analyse fondamentale

| Source | Accès | Configuration |
|---|---|---|
| **FRED** (Fed St. Louis) | API officielle, toutes séries | `FRED_API` (déjà dans le `.env`) |
| **Recherche web** | Google Custom Search, sinon DuckDuckGo | `GOOGLE_API_KEY` + `GOOGLE_CSE_ID` (facultatif) |
| **myfxbook** (sentiment retail) | API officielle (la page publique renvoie 403) | `MYFXBOOK_EMAIL` + `MYFXBOOK_PASSWORD` — **vos** identifiants, envoyés uniquement à myfxbook |
| Calendrier éco + GDELT | déjà en place | `MT5_FILES`, `NEWS_GDELT` |

## Filet de sécurité déterministe (indépendant du LLM)

Correctifs issus d'une revue de risque sévère. **Aucune de ces protections ne dépend
de l'IA** : elles tournent à chaque cycle **et** dans un watchdog entre deux cycles.

| Garde-fou | Comportement |
|---|---|
| **Urgence perte-jour** | perte du jour ≥ 75 % du stop agent (ou perte totale ≥ seuil doux) → **toutes nos positions sont fermées**, LLM vivant ou non |
| **Watchdog** | `AGENT_WATCH_SECONDS` (60 s) : perte-jour, SL d'urgence, break-even, trailing — sans appeler le modèle |
| **Positions étrangères** | l'agent n'agit **que** sur ses ordres (`magic`) ; celles d'un autre EA sont comptées dans le risque et l'equity, jamais fermées — une alerte est loguée |
| **Journée FTMO** | ancrée sur l'**horloge serveur** (EET/EEST), plus sur UTC : la perte journalière est mesurée sur la même journée que FTMO |
| **Corrélation** | plafond de risque **par devise** (`AGENT_MAX_RISK_PER_CURRENCY_PCT`, 2 %) : EURUSD + GBPUSD + XAUUSD longs ne peuvent plus faire 3 % sur le seul pari dollar |
| **Calendrier absent** | log **ERREUR** à chaque cycle (le black-out ne protégeait plus rien en silence) + `NEWS_FAIL_CLOSED=1` pour interdire toute entrée |
| **Heure d'été** | conversion EET→UTC calculée (UTC+2 / UTC+3) au lieu d'un -2 h fixe : la fenêtre de black-out tombe au bon moment |
| **Ticket de position** | résolu via le *deal* (`position_id`) : plus de trailing/MFE/attribution rattachés à un ticket fantôme |
| **Stop d'urgence** | arrondi aux **digits réels** du symbole (l'or à 2 décimales était rejeté par le broker) |
| **Gap week-end** | `AGENT_WEEKEND_FLATTEN=1` ferme tout au cutoff du vendredi (défaut 0 : blocage des entrées seulement) |
| **Objectif vs jours minimum** | l'arrêt « objectif atteint » ne bloque plus les ouvertures tant que le minimum de jours de trading n'est pas atteint (sinon l'étape devenait invalidable) |

## Modèle : Claude, DeepSeek… avec ou sans outils

Deux modes, choisis automatiquement (`BEDROCK_TOOL_MODE=auto`) :

- **Tool calling** (Claude, Nova, Mistral Large…) : le modèle appelle lui-même
  `get_chart`, `compute_indicator`, `web_search`, `plan_open`…
- **Mode JSON** (DeepSeek et tout modèle sans *tool use*) : on lui livre un **dossier
  complet** du cycle (compte, positions, marchés, chart, stratégies, post-mortem, news)
  et il répond par un **plan d'actions JSON**, qui repasse par **exactement les mêmes
  validations** (`plan_open/close/modify/trail`) puis par le moteur de risque FTMO.

La bascule est automatique si le tool calling échoue. En mode JSON, l'agent ne peut pas
demander une analyse supplémentaire : il décide avec le dossier, ou s'abstient.

**Authentification** : `BEDROCK_API_KEY` (clé API Bedrock, exportée pour boto3 en
`AWS_BEARER_TOKEN_BEDROCK`) ou la chaîne de credentials AWS classique.

## État persistant : SQLite, pas des fichiers

Tout l'état vit dans **`state/agent.db`** ([store.py](agent/store.py)) — journal,
leçons, positions suivies, session FTMO, marqueur de mode secours, cache news.

Pourquoi : `write_text()` n'est **pas atomique**. Un process tué (VPS qui redémarre,
Ctrl-C, crash MT5) pendant l'écriture d'`open_meta.json` faisait perdre la
correspondance ticket → stratégie/risque : plus de trailing suivi, plus d'attribution
du R, post-mortem faussé. Avec SQLite : chaque écriture est une **transaction**, le
mode **WAL** survit à une coupure, et au redémarrage l'agent retrouve exactement ses
positions suivies, son ancrage de perte journalière et ses leçons.

| Table | Contenu |
|---|---|
| `events` | journal horodaté (ordres, vetos, clôtures, black-out, mode secours…) |
| `lessons` | leçons apprises |
| `open_meta` | ticket → stratégie, risque, trailing armé, MFE/MAE (écrit **en une transaction**) |
| `kv` | session FTMO, marqueur mode secours, cache news |

**Migration automatique** : au premier démarrage, `trades.jsonl`, `lessons.json`,
`session.json`, `open_meta.json`, `news_cache.json` et `safe_mode.json` sont importés,
puis renommés `*.migrated` (rien n'est supprimé). Chemin de base surchargeable avec
`AGENT_DB`.

Inspection sans SQL — session, mode secours, positions suivies, derniers événements et
bilan chiffré :

```bash
python run.py --status
```

## Si l'IA tombe : pilote de secours 100 % Python

Dès que le LLM devient indisponible (Bedrock injoignable, credentials expirées, quota,
dépendance manquante, erreur d'exécution), l'agent **bascule automatiquement** sur
[brain/autopilot.py](agent/brain/autopilot.py) — **zéro IA, que des règles fixes** :

1. **Aucune nouvelle position.** Jamais, quelles que soient les conditions.
2. **Protection du compte d'abord** : si la perte du jour atteint 75 % du stop agent
   (ou la perte totale le seuil doux), **toutes** les positions sont fermées.
3. **Aucune position sans stop** : SL d'urgence à 1,5 × ATR si le stop manque.
4. **Break-even** dès +1R, puis **trailing ATR** armé automatiquement.
5. **Time-stop** : une position qui traîne 10 jours sans rien donner est fermée.
6. **Dès que le book est vide → le script s'arrête** (`SafeExit`, code 0) pour que vous
   inspectiez et relanciez vous-même.

Traces laissées pour l'inspection (`python run.py --status`) : l'entrée `safe_mode` de la
base (mode actif, raison, cycles, positions restantes) et les événements `safe_mode` /
`safe_exit` du journal.
Si l'IA redevient disponible **avant** la fermeture des positions, l'agent reprend
normalement et le marqueur est effacé.

Réglages (`.env`) : `SAFE_BE_AT_R`, `SAFE_TRAIL_ATR`, `SAFE_TRAIL_ACTIVATE_R`,
`SAFE_MISSING_SL_ATR`, `SAFE_TIME_STOP_DAYS`, `SAFE_TIME_STOP_MIN_R`, `SAFE_PANIC_RATIO`,
`SAFE_EXIT_WHEN_FLAT` (mettre à `0` pour continuer à tourner au lieu de sortir).

## Coûts réels : spread, commission, slippage, swap, gap, marge

Le risque d'un trade n'est **pas** `entrée − stop`. Le moteur de risque dimensionne sur
le **coût réel** ([risk/ftmo.py](agent/risk/ftmo.py), `TradeCosts`) :

```
perte si stop touché = (distance + spread + slippage) × valeur_du_pip × lot
                     + commission aller-retour × lot
```

Il refuse en plus le trade si :

| Veto | Seuil par défaut | Env |
|---|---|---|
| **R:R net** (après frais) trop faible | < 1.0 | — |
| **Spread anormal** | > 3 pips **ou** > 12 % de l'ATR | `AGENT_MAX_SPREAD_PIPS`, `AGENT_MAX_SPREAD_ATR` |
| **Stop/TP sous le minimum broker** (`stops_level`) | rejet MT5 garanti | — |
| **Marge** engagée par la position | > 20 % de la marge libre | `AGENT_MAX_MARGIN_PCT` |
| **Gap week-end** : plus d'entrée le vendredi soir | après 20 h UTC | `AGENT_FRIDAY_CUTOFF_UTC`, `AGENT_WEEKEND_GUARD` |

Côté exécution : mode de remplissage détecté par symbole (fin des « Unsupported filling
mode »), déviation configurable, **retry sur requote**, et **mesure du slippage réel** à
chaque ordre (`AGENT_DEVIATION_POINTS`, `AGENT_ORDER_RETRIES`).

Le LLM voit tout ça via `get_trading_costs(symbol)` : spread en pips/$/% de l'ATR,
commission, **swap par nuit** (portage swing, ×3 le mercredi), stop minimum, marge requise,
provision de gap. Réglez `AGENT_COMMISSION_PER_LOT` (défaut **7 $/lot aller-retour**) sur
la vraie grille de votre broker — c'est l'hypothèse la plus structurante du sizing.

## Il apprend de ses erreurs (avec des chiffres)

Trois étages, du plus factuel au plus interprétatif :

1. **Traces** — à l'ouverture on enregistre le plan (R:R brut et net, régime, confiance,
   coûts estimés, slippage subi) ; à **chaque cycle** on met à jour **MFE/MAE** (le meilleur
   et le pire point atteint, en R) ; à la clôture on écrit plan **vs** réalité.
2. **Post-mortem chiffré** ([brain/postmortem.py](agent/brain/postmortem.py)) — winrate,
   espérance R, écart R:R planifié / R encaissé, coût d'exécution en % du PnL, performance
   par stratégie / symbole / régime, et surtout des **défauts récurrents** nommés :
   *gains rendus*, *stop trop serré*, *asymétrie défavorable*, *frais excessifs*,
   *slippage élevé*, *stratégie ou symbole à éviter*, *sur-confiance*.
3. **Leçons** — la réflexion post-clôture reçoit désormais ce bilan (interdiction de
   répéter une leçon déjà présente ou de sortir une banalité), et les leçons injectées sont
   **triées par pertinence** (symbole/stratégie du moment, erreurs d'abord) et dédupliquées.

Le prompt impose la règle : **tant qu'un défaut est listé, le corriger prime sur toute
nouvelle idée**. Outil : `get_postmortem()`.

## Ce qui est nouveau : l'agent choisit / change de stratégie

L'agent n'est plus enfermé dans une méthode. À chaque cycle :

1. **Régime de marché** (`strategy/regime.py`) — chaque symbole est classé :
   `trend_up`, `trend_down`, `range`, `high_vol`.
2. **Scoreboard** (`strategy/scoreboard.py`) — chaque stratégie a un score de
   sélection = **adéquation au régime** + **edge historique réel** (expectancy en R)
   + **bonus d'exploration UCB** (bandit). Une stratégie devenue mauvaise est
   reléguée ; une jamais testée reçoit sa chance.
3. **Le LLM choisit** la stratégie la mieux classée (ou justifie un autre choix)
   parmi le catalogue (`strategy/playbooks.py`) : `trend_follow`,
   `donchian_breakout`, `mean_reversion`, `momentum`, `flat`.
4. **Attribution** — à la clôture, le résultat (R) est imputé à la stratégie
   utilisée → le scoreboard se met à jour → l'agent apprend *quelle stratégie
   marche pour ce marché, ce compte, cette période*.

```
   régime marché ─┐
                  ├─▶ scoreboard (edge réel + exploration) ─▶ classement ─▶ LLM choisit
   track-record ──┘                                                          │
                                                                   applique la stratégie
                                                                             │
                                                          moteur de risque FTMO (veto + lot)
                                                                             │
                                                              exécution MT5 (ordres réels)
                                                                             │
                                                       clôture ─▶ R imputé ─▶ apprend
```

## Architecture — le LLM propose, le risque dispose

Le LLM **ne calcule jamais le lot** et **ne peut pas contourner** les limites :
le moteur `risk/ftmo.py` dimensionne par la distance au stop et oppose un veto
déterministe (perte jour/total, nb positions, trades/jour, cooldown, concentration).

| Fichier | Rôle |
|---|---|
| `config.py` | Config Bedrock + FTMO + news (via `.env`) ; profil swing D1 |
| `risk/ftmo.py` | Moteur de risque déterministe (sizing + veto) |
| `broker/mt5_broker.py` | MT5, data, **exécution réelle** (plus de simulation) |
| `data/market.py` | Indicateurs → snapshot chiffré (scan) |
| `data/chart.py` | **Lecture chart** : multi-TF **au choix de l'agent** + bougies + swings + niveaux |
| `data/indicators.py` | **Boîte à outils d'indicateurs** calculés à la demande (17 indicateurs) |
| `data/news.py` | **News** : calendrier MT5 + FRED (Fed, série au choix) + GDELT + macro/devise |
| `data/web.py` | **Recherche/lecture web** (Google/DuckDuckGo, pages publiques, sentiment retail) + garde-fous |
| `brain/autopilot.py` | **Pilote de secours déterministe** quand l'IA est indisponible |
| `brain/postmortem.py` | **Bilan chiffré + défauts récurrents** (apprentissage factuel) |
| `strategy/regime.py` | Classification du régime de marché |
| `strategy/playbooks.py` | Catalogue des stratégies (modes d'emploi) |
| `strategy/scoreboard.py` | Apprentissage : quelle stratégie marche (bandit UCB) |
| `brain/tools.py` | Outils LangChain : observer (`list_symbols`, `get_chart`, `compute_indicator`, `get_news`, `get_macro_events`, `search_news`…) + agir (`plan_open/close/modify/trail`) |
| `brain/agent.py` | Le trader swing autonome (Bedrock) + le coach (réflexion) |
| `store.py` | **Persistance SQLite** (journal, états, migration depuis les anciens JSON) |
| `brain/memory.py` | Journal + leçons + attribution stratégie (au-dessus de `store.py`) |
| `run.py` | Boucle de vie |

## News & profil swing

L'agent est **news-aware** ([data/news.py](agent/data/news.py)) — à chaque cycle il
reçoit, par devise du symbole :

- **Calendrier économique MT5** (`calendar_history.csv`, exporté par votre
  `ExportCalendar.mq5`) : surprises récentes + événements **à venir** à fort impact.
- **Réserve fédérale / taux** via **FRED** (`FRED_API`) : Fed funds, 2 ans, 10 ans.
- **Actualités** via **GDELT** (gratuit) : titres 48 h/devise, sentiment jugé par le LLM.
- **Brain macro par devise** (`macro_features.csv` de `v4_macro/macro_service.py`), s'il existe.

Deux protections swing en découlent :
1. **Black-out news** — l'agent n'ouvre **aucune** nouvelle position si un événement à
   fort impact touche une devise du symbole dans ±`NEWS_BLACKOUT_MIN` minutes (60 par défaut).
2. **Biais macro** — le LLM aligne la direction sur le fondamental (taux, banques centrales).

Profil swing par défaut : timeframe **D1**, cycle **horaire**, stops larges, R:R ≥ 2.

Réglages utiles (`.env`) : `AGENT_SYMBOLS` (watchlist de départ), `AGENT_TIMEFRAME`
(TF du scan), `AGENT_CHART_TFS` (TF pré-chargés du chart, défaut `W1,D1,H4`),
`AGENT_MAX_STEPS` (nombre max d'appels d'outils par cycle, défaut 18 — l'agent
explore : univers, charts, indicateurs, news).

## Challenge FTMO en 2 étapes

L'agent connaît l'**étape en cours** (`FTMO_PHASE=1` ou `2`) et adapte son objectif :

| Étape | Objectif | Perte jour max | Perte totale max | Durée | Min. jours tradés |
|---|---|---|---|---|---|
| 1 — Challenge | **+10 %** | −5 % | −10 % | 30 j | 4 |
| 2 — Vérification | **+5 %** | −5 % | −10 % | 30 j | 4 |

- **Objectif atteint → il arrête d'ouvrir** (le moteur de risque bloque les entrées ;
  il peut encore gérer/sécuriser l'existant).
- **Agressivité modulée** : chaque cycle, l'agent voit `jours_restants`,
  `reste_avant_objectif_pct`, `jours_trades` — il vise des setups A+ s'il est court en
  temps, reste sélectif sinon, sans jamais dépasser **1 %** de risque/trade.
- **Minimum 4 jours de trading** suivi (`state/session.json`) pour valider le critère.
- **Perte jour → −4 %** : l'agent arrête les nouvelles ouvertures (marge avant le −5 % fatal).
- Passage à l'étape 2 : mettre `FTMO_PHASE=2` → l'agent ré-ancre le solde initial et la fenêtre.

## Notifications email

Vous recevez un message à chaque moment qui compte, avec **l'état du portefeuille dans
chaque mail** (equity, PnL, perte du jour, objectif, positions ouvertes avec leur R
flottant) :

| Événement | Contenu |
|---|---|
| **Position ouverte** | sens, symbole, stratégie, lot, entrée réelle **vs** prévue et slippage, SL/TP, risque en $ et en %, R:R brut **et** net, coûts estimés, et le raisonnement de l'agent |
| **Position fermée** | GAIN/PERTE, résultat en R et en $, MFE/MAE, R:R planifié, durée, et la **leçon retenue** |
| **Urgence FTMO** | perte du jour proche de la limite → fermeture totale, avec l'action requise |
| **IA indisponible** | passage en pilote de secours (une seule alerte par panne) |
| **Arrêt du script** | plus aucune position ouverte : que faire pour relancer |
| **IA rétablie** | reprise du pilotage normal |

Configuration dans `agent/.env` (`MAIL_HOST`, `MAIL_PORT`, `MAIL_SECURE`, `MAIL_USER`,
`MAIL_PASSWORD`, `MAIL_FROM`, `MAIL_TO`). Le mode SSL/STARTTLS est déduit du port si
`MAIL_SECURE` n'est pas renseigné. Vérifier la configuration :

```bash
python run.py --test-mail
```

L'envoi part dans un thread avec timeout : **un serveur SMTP injoignable ne bloque ni
n'interrompt jamais le trading** (l'erreur est simplement journalisée).

## Configuration : un seul fichier

`agent/.env` est **autonome** — MT5, Bedrock, FTMO, coûts, news, web, mail, état. Le
`.env` du projet parent n'est lu qu'en repli, si `agent/.env` n'existe pas. Une variable
définie dans l'environnement l'emporte sur le fichier (`AGENT_WEEKEND_FLATTEN=1 python
run.py`). Les placeholders non remplacés (`<votre_cle>`) sont traités comme vides, pour
ne jamais s'authentifier avec une fausse clé.

```bash
cp .env.example .env   # puis remplir
```

## Installation

```bash
cd agent
pip install -r requirements.txt
cp .env.example .env
```

Renseigner dans `.env` :
- **`BEDROCK_MODEL_ID`** et **`AWS_REGION`**. L'ID est un *inference profile* qui
  dépend de votre région/compte — listez les vôtres :
  ```bash
  aws bedrock list-inference-profiles --region us-east-1
  ```
  et vérifiez que l'accès au modèle Claude est activé dans la console Bedrock.
- **Credentials AWS** : chaîne boto3 par défaut (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`,
  ou `AWS_PROFILE`, ou rôle IAM).
- MT5 : déjà présent dans le `.env` racine, réutilisé automatiquement.

## Utilisation

```bash
python run.py          # un cycle (ordre réel si un setup passe le risque)
python run.py --loop   # boucle continue (cycle horaire, swing)
```

Le script se termine tout seul (code 0) dans un seul cas : **IA indisponible + plus
aucune position ouverte** — c'est le pilote de secours qui a fini son travail, à vous
d'inspecter (`python run.py --status`) puis de relancer.

**Exécution directe.** Dès qu'un setup passe le moteur de risque FTMO, l'ordre est
**envoyé réellement** à MT5. Si MT5 n'est pas connecté, l'agent ne trade pas (log
explicite). La discipline (perte jour/total, cooldown, black-out news, R:R) reste
intégralement appliquée. **Lancez-le sur un compte démo ou challenge FTMO** le
temps de valider son comportement, jamais directement sur un compte financé non testé.

## Notes

- **Windows uniquement** pour le live (package `MetaTrader5`). Hors MT5 (non
  connecté), l'agent ne trade pas — il log l'erreur et saute le cycle.
- `MetaTrader5` peut ne pas avoir de wheel pour Python 3.14 ; si `pip install`
  échoue, utilisez un venv 3.11/3.12 pour ce module.
- Ceci est un **outil que vous exécutez vous-même**. Ce n'est pas un conseil en
  investissement ; testez longuement en dry-run avant tout capital réel.
