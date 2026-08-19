# Desk multi-agents FTMO — journal d'implémentation

> Fichier de suivi vivant. **Objectif** : transformer l'agent trader unique en une
> *entreprise* d'agents spécialisés qui collaborent, se challengent et apprennent —
> pour gérer un compte FTMO (jusqu'à 1 M$) avec plus de rigueur qu'un seul cerveau.
>
> Ce document est la source de vérité de l'avancement. À reprendre ici après chaque pause.

---

## 0. Le dossier est le déployable

Depuis le 2026-08-12, `ai-company/` **est** l'application : on copie ce dossier sur une
machine, on remplit `.env`, il tourne. Rien au-dessus n'est requis.

```
ai-company/
  .env / .env.example   ← configuration : l'ORGANISATION (DESK_*, EVAL_*) et la MACHINE
                          (MT5, Bedrock/AWS, SMTP, FTMO) — sections séparées dans l'exemple
  run.py                ← point d'entrée (boucle de vie, watchdog, exécution)
  config.py store.py notify.py process.py
  brain/ broker/ data/ risk/ strategy/   ← l'infrastructure
  desk/                 ← les employés (le cerveau)
  state/                ← SQLite + verrou d'instance (jamais versionné)
  tests/                ← une seule suite : `python -m pytest tests -q`
```

Une variable d'environnement du shell l'emporte toujours sur `.env`, ce qui permet de
tout piloter par variables dans un conteneur, **sans fichier `.env` du tout**.
`AGENT_STATE_DIR` déplace l'état hors du dossier si le code est monté en lecture seule.

Ce que le déploiement garantit (cf. §10) : une seule instance à la fois, un arrêt propre
sur SIGTERM, des alertes email qui ne se perdent pas, et un journal qui ne grossit pas
indéfiniment.

---

## 1. Principe fondateur (NON NÉGOCIABLE)

Le code hérité repose sur **« le LLM propose, le risque dispose »**. On le garde intact :

- **`risk/ftmo.py`** — moteur déterministe : dimensionne le lot et oppose un **veto dur**
  (perte jour/total, corrélation par devise, marge, R:R net, spread). C'est le **plancher**.
  Le *Risk Manager LLM* vient PAR-DESSUS et ne peut que **durcir** (refuser / réduire),
  jamais desserrer.
- **`brain/autopilot.py` + watchdog** — si le cerveau de gestion tombe, on rebascule sur
  le pilote 100 % Python (aucune nouvelle entrée, protection du book).
- **Journée FTMO sur horloge serveur, black-out news, garde week-end** — inchangés.

Règle d'or : *aucune décision LLM ne devient un ordre sans repasser par le moteur FTMO.*

---

## 2. L'organigramme (les « employés »)

```
        ┌─────────────── GÉRANT / DG (fixe le mandat du cycle) ───────────────┐
        │  objectif atteint ? jours restants ? posture ? convoquer le desk ?  │
        └───────────────────────────────┬─────────────────────────────────────┘
   (si entrées permises + candidat)      │            (toujours)
   ┌─────────────┬─────────────┬─────────┴────────┬────────────────┐    ┌──────────────┐
   │ Analyste    │ Analyste    │ Analyste         │ Analyste        │    │ TRADE        │
   │ TECHNIQUE   │ FONDAMENTAL │ SENTIMENT        │ ACTU. MONDIALE  │    │ MANAGER      │
   └──────┬──────┴──────┬──────┴────────┬─────────┴────────┬────────┘    │ (gère book   │
          └─────────────┴──── 4 briefs ─┴──────────────────┘             │  ouvert)     │
                                 │                                        └──────────────┘
                     ┌───────────┴───────────┐
                     │ DÉBAT Bull vs Bear    │  se challengent → élimine le biais
                     └───────────┬───────────┘
                     ┌───────────┴───────────┐
                     │ TRADER (tranche)      │  expert FTMO, apprend de SES erreurs
                     └───────────┬───────────┘  ET de celles de chaque agent
              ┌──────────────────┴──────────────────┐
              │ RISK MANAGER LLM (durcit seulement)  │
              └──────────────────┬──────────────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              │ MOTEUR FTMO DÉTERMINISTE (plancher)  │ → exécution MT5
              └─────────────────────────────────────┘
```

Rôles → modules :

| Rôle demandé | Module | Sortie |
|---|---|---|
| Gérant / Directeur Général | `desk/gerant.py` | mandat du cycle (posture, convoquer, candidats, consignes) |
| Analyste technique | `desk/analysts.py` | brief {biais, confiance, niveaux, invalidation} |
| Analyste fondamental | `desk/analysts.py` | brief macro (taux, banques centrales) |
| Analyste sentiment | `desk/analysts.py` | brief positionnement (contrarien) |
| Analyste actualité mondiale | `desk/analysts.py` | brief événementiel / géopolitique |
| Débatteurs Bull & Bear | `desk/debat.py` | thèses opposées + risques non résolus |
| Trader | `desk/trader.py` | actions `open` (+ `close` si thèse cassée) |
| Risk Manager | `desk/risk_manager.py` | verdicts approve/reject/reduce (durcit) |
| Agent de suivi des trades | `desk/trade_manager.py` | actions `close/modify/trail` |
| **Vigie des positions** | `desk/vigie.py` | alerte le DG sur un trade qui se dégrade |
| Orchestration entreprise | `desk/desk.py` | `TradingDesk` (drop-in de `TraderAgent`) |
| Apprentissage multi-rôles | `desk/learning.py` | attribution + leçons par rôle |

### 2b. Boucle d'escalade — Vigie → DG → session extraordinaire

Distincte du cycle horaire normal ET du watchdog déterministe. Objectif : ne pas
attendre le prochain cycle horaire quand un trade se dégrade **entre deux cycles**.

```
Watchdog déterministe (toutes les AGENT_WATCH_SECONDS)
  1. protège TOUJOURS (SL urgence, break-even, trailing, panique perte-jour)   ← inchangé
  2. calcule des signaux d'alerte bon marché par position (floating R, gains
     rendus, news imminente, âge, distance au SL)
        └─ si un seuil est franchi (et pas déjà consultée récemment) :
             VIGIE (LLM) évalue CE trade → {gravité: ok|surveiller|escalader}
                └─ si « escalader » → alerte le DG
                     └─ DG décide : SESSION EXTRAORDINAIRE ciblée sur ce ticket ?
                          └─ le desk re-tourne en « revue de position » (Trader +
                             Trade Manager, analystes si besoin) → close / modify / trail
                             exécuté immédiatement, sans attendre le cycle horaire.
```

Garde-fous : la Vigie est **conditionnée** par un déclencheur déterministe (jamais un
appel LLM à vide toutes les 60 s) et **rate-limitée** par ticket (`DESK_VIGIE_MIN_MINUTES`).
Si la Vigie ou le DG est injoignable, **rien ne casse** : le watchdog déterministe protège
déjà. La session extraordinaire ne peut que **réduire le risque** (arrêter/resserrer), pas
ouvrir de nouvelle position.

---

## 3. Contrat d'intégration (pourquoi ça ne casse rien)

`TradingDesk` est un **remplaçant direct** de `brain.agent.TraderAgent` :

- `decide(summary: dict) -> list[dict]` — mêmes actions `{type: open|close|modify|trail, ...}`.
- `reflect(trade: dict, postmortem: str) -> str` — leçon(s) à la clôture.
- attributs `degraded: bool`, `last_error: str`.

L'orchestrateur (`run.Orchestrator`) choisit solo vs desk dans `_lazy_agent()` selon
`AGENT_MODE`. Tout l'aval (moteur FTMO, exécution MT5, notifications, journal, scoreboard,
post-mortem) est **inchangé**. Le desk lit le même contexte de cycle déjà préparé
(`brain.tools._CTX` via `bind_context`) et le même accès marché (`bind_live`).

### Dégradation (préserve le filet actuel)
- Gérant OU Trade Manager injoignable → `degraded=True` → l'orchestrateur bascule sur
  `SafePilot` (déterministe, aucune entrée). **Identique au comportement solo.**
- Échec dans la chaîne de recherche (analystes/débat/Trader/Risk Manager) → **pas** de
  panique : on garde la gestion du book, on saute simplement les nouvelles entrées.

### Réduction d'exposition par le Risk Manager
- `verdict=reject` → l'ouverture est supprimée.
- `verdict=reduce` → l'action porte `risk_pct` (≤ budget cfg) ; `_exec_open` le passe au
  moteur comme **plafond** (`validate(..., risk_pct_override=...)`). Le moteur ne peut
  qu'aller en-dessous — jamais au-dessus.

---

## 4. Décisions (validées avec l'utilisateur)

- **Rythme** : c'est le **Gérant qui déclenche** le desk complet, à la demande (≈2 appels
  LLM/cycle au repos, ≈10-12 quand un candidat mérite la recherche). Le Trade Manager et le
  watchdog protègent le book à chaque cycle quoi qu'il arrive.
- **Modèles** : **modèle unique** partout (`BEDROCK_MODEL_ID`). Override par rôle possible
  via `DESK_MODEL_<ROLE>` mais désactivé par défaut.
- **Livraison** : **par phases**, on **écrase l'agent solo** (le desk devient le défaut).
  `AGENT_MODE=solo` reste dispo comme repli le temps de la validation.

---

## 5. Phases

- [x] **Phase 0 — Fondations** : package `desk/`, `DeskConfig`, `base.py` (client Bedrock
      partagé + JSON robuste + dégradation), câblage `AGENT_MODE` dans `run.py`, ce fichier.
- [x] **Phase 1 — Noyau décisionnel** : Gérant + Trader + Risk Manager + Trade Manager +
      `TradingDesk`. Desk fonctionnel de bout en bout (sans analystes ni débat). Override
      `risk_pct` dans le moteur + `_exec_open`. Leçons filtrées par rôle. Tests.
- [x] **Phase 1C — Traces & honnêteté de l'exécution** (cf. §8) : dossier de décision
      persisté par ticket, garde entrée/marché, isolation SMTP des tests.
- [x] **Phase 1D — Mesure** (cf. §8) : journal des cycles (`desk/journal.py`), mode ombre
      (`EVAL_SHADOW`), harnais de rejeu + métriques (`desk/replay.py`).
- [x] **Phase 1B — Vigie & session extraordinaire** : agent `desk/vigie.py` qui surveille
      les positions ouvertes (au-delà du watchdog déterministe), alerte le DG sur un trade
      en perte / qui rend ses gains / menacé par une news. Le DG peut convoquer une
      **session extraordinaire** (`TradingDesk.review_position(ticket, alerte)`) branchée dans
      le watchdog de l'orchestrateur, qui ne peut qu'**arrêter ou modifier** le trade visé.
      Déclencheur déterministe + rate-limit par ticket + repli sûr si LLM injoignable. Tests.
      **Livré** : `desk/vigie.py`, `Gerant.escalade()`, `TradingDesk.watch()` +
      `_session_extraordinaire()`, hook `run._vigie_watch()` / `_vigie_triggers()` dans le
      watchdog, `tests/test_vigie.py` (17). Le watchdog sort avant la vigie si une urgence
      déterministe a déjà été traitée. Double filtre anti-ouverture (desk **et**
      orchestrateur) : une session extraordinaire ne peut que réduire le risque.
- [x] **Phase 2 — Analystes** : Technique, Fondamental, Sentiment, Actualité mondiale.
      Briefs injectés au Trader (et au débat). **Indépendants** (aucun ne voit les briefs des
      autres) et alimentés par des **dossiers spécialisés pré-chargés** — pas de tool-calling,
      DeepSeek V3 sur Bedrock n'en a pas. Tests.
      **Livré** : `desk/analysts.py` (`Analyste` + 4 métiers + `Analystes`), rôles distincts
      pour les leçons mais modèle partagé (`DeskAgent.model_role`), toile de fond macro
      chargée **une fois par cycle**, brief mal formé → neutre/0.0, source en panne → trou
      dans le dossier, `tests/test_analystes.py` (15). Coupe-circuit `DESK_USE_ANALYSTS=0`.
- [x] **Phase 3 — Débat Bull/Bear + JUGE** : contradiction forcée, puis un **arbitre** rend un
      *plan écrit* (thèse retenue, invalidation, risques non résolus) que le Trader exécute au
      lieu d'arbitrer lui-même. Débat **adaptatif** : 2e tour seulement si les convictions
      sont proches. **Livré** : `desk/debat.py` (Bull, Bear, Juge, `Debat`), le Bear parle en
      second et doit réfuter, verdict illisible → **abstention**, filtre déterministe
      `TradingDesk._appliquer_verdicts` (une ouverture qui contredit le juge est supprimée —
      un prompt qui demande d'obéir se fait ignorer tôt ou tard), `tests/test_debat.py` (14).
- [x] **Phase 3B — Risque à 3 tempéraments** : Agressif / Neutre / Prudent donnent un avis,
      le Gérant arbitre en approve/reduce/reject, à la place du Risk Manager mono-voix.
      Contrainte inchangée : le verdict ne peut que **durcir**, le moteur FTMO reste le
      plancher. **Livré** : `desk/risque.py` (le Prudent parle en dernier et voit les autres),
      `Gerant.arbitrer_risque()`, **unanimité négative** (3 refus = suppression sans même
      arbitrer), `risk_manager.appliquer_verdicts()` partagé (un seul endroit où le
      durcissement devient une action), `DESK_RISK_DEBATE=0` pour revenir au mono-voix,
      `tests/test_risque_college.py` (11).
- [x] **Phase 4 — Apprentissage multi-agents** : attribution du résultat par rôle (à partir
      du `dossier` de décision livré en Phase 1C) et **mémoire situationnelle**.
      **Livré** : `desk/situation.py` — signature déterministe (régime, ATR %, RSI, position
      dans le range, momentum, session, news proche, direction du juge) + distance pondérée
      + bloc « cas passés les plus proches » injecté au Trader avec leur R réel ;
      `TradingDesk.reflect()` fait l'attribution (0 à 3 leçons **taguées par rôle**, rôle
      inconnu ignoré, « aucune leçon » est une réponse valable — on juge le processus, pas
      le résultat) ; repli sur l'ancien coach pour un trade sans dossier ;
      `desk/bilan_roles.py` — **revue de performance par employé** croisant le dossier de
      décision avec le R réalisé (analyste aligné vs opposé, juge par conviction, débat par
      nombre de tours, collège par verdict, Gérant par posture), injectée au Gérant *et* au
      coach, avec un **refus explicite de conclure sous 5 observations** ;
      `tests/test_apprentissage.py` (14) + `tests/test_bilan_roles.py` (11).
      *Choix assumé* : pas d'embeddings — sept variables de marché explicables, déterministes
      et rejouables, sans service externe ni coût par appel. Le prix est de choisir les
      variables à la main (`ECHELLES`/`POIDS` dans `desk/situation.py`), donc modifiables.
- [x] **Phase 5 — Bascule & docs** : desk par défaut, **modèles à deux vitesses**
      (`DESK_MODEL_RAPIDE` pour Gérant/Trade Manager/Vigie appelés à chaque cycle,
      `DESK_MODEL_FORT` pour Trader/analystes/débat/risque ; l'override `DESK_MODEL_<ROLE>`
      gagne sur les deux), section desk + mesure dans `README.md`, `.env.example` complété.

---

## 6. Config ajoutée (`.env`)

| Variable | Défaut | Rôle |
|---|---|---|
| `AGENT_MODE` | `desk` | `solo` = agent unique ; `desk` = entreprise multi-agents |
| `DESK_DEBATE_ROUNDS` | `1` | tours de contradiction Bull/Bear |
| `DESK_USE_ANALYSTS` | `1` | activer les 4 analystes |
| `DESK_USE_DEBATE` | `1` | activer le débat Bull/Bear |
| `DESK_MAX_CANDIDATS` | `2` | nb max de symboles envoyés au desk complet par cycle |
| `DESK_VIGIE_ENABLED` | `1` | activer la vigie + boucle d'escalade vers le DG |
| `DESK_VIGIE_ALERT_R` | `-0.6` | seuil de perte flottante (R) qui réveille la vigie |
| `DESK_VIGIE_GIVEBACK_R` | `1.0` | MFE atteint (R) au-delà duquel « gains rendus » alerte |
| `DESK_VIGIE_MIN_MINUTES` | `30` | anti-spam : délai min entre deux consultations d'un même ticket |
| `DESK_MODEL_GERANT/TRADER/RISK/ANALYSTE/DEBAT/SUIVI/VIGIE` | (vide) | override modèle par rôle |
| `DESK_DEBATE_GAP` | `0.2` | écart de conviction en dessous duquel le débat est « serré » |
| `DESK_RISK_DEBATE` | `1` | collège du risque (0 = Risk Manager mono-voix) |
| `DESK_MODEL_RAPIDE` / `DESK_MODEL_FORT` | (vide) | modèle par classe de rôle (deux vitesses) |
| `EVAL_JOURNAL_CYCLES` | `1` | archive le dossier d'entrée + le plan de chaque cycle (rejouable) |
| `EVAL_JOURNAL_KEEP` | `500` | rotation du journal des cycles |
| `EVAL_SHADOW` | `0` | **mode ombre** : le cerveau décide, rien n'est exécuté |
| `EVAL_REPLAY_MAX_BARS` | `30` | bougies parcourues en aval d'une décision (simulation) |
| `AGENT_ENTRY_REPRICE` | `1` | recaler l'entrée sur le prix exécutable avant le sizing |
| `AGENT_MAX_ENTRY_DRIFT_ATR` | `0.5` | au-delà de N × ATR d'écart, l'entrée est refusée |

---

## 7bis. Ce qu'on a repris de TradingAgents (Xiao et al., arXiv 2412.20138)

Comparaison faite le 2026-08-11. Leur pipeline : 4 analystes (avec outils) → débat
Bull/Bear arbitré par un **juge** → Trader → **débat de risque à 3 tempéraments**
(Risky/Neutral/Safe) arbitré par un *fund manager* → décision. Communication par
**documents structurés** dans un état partagé (et non par chaîne de messages, qui perd
l'information — leur « telephone effect »), plus des **modèles à deux vitesses**.

**Retenu** : le juge du débat (Phase 3), le risque comme débat contradictoire (Phase 3B),
la mémoire retrouvée par *similarité de situation* (Phase 4), l'indépendance des analystes
(Phase 2), les modèles à deux vitesses (Phase 5).

**Écarté / adapté** : leur couche de risque est 100 % LLM, sans plancher dur — inacceptable
en prop-firm : `risk/ftmo.py` reste l'autorité et le débat de risque ne peut que durcir.
Leurs analystes appellent des outils (ReAct) : impossible avec DeepSeek V3 sur Bedrock (pas
de tool calling) → dossiers spécialisés pré-chargés. Leur évaluation (3 titres, un trimestre
haussier, Sharpe 8,21) ne prouve rien de généralisable : **on copie l'architecture, pas la
confiance** — d'où les phases 1C/1D avant tout ajout d'agents.

---

## 8. Phases 1C / 1D — traces & mesure (livrées)

Motivation : ajouter des employés LLM sans instrument de mesure augmente le coût et la
variance, pas forcément la qualité. Trois manques ont été comblés **avant** d'aller plus loin.

**1C-B — Dossier de décision persisté.** Chaque ouverture du desk emporte désormais un
`dossier` compact (mandat du Gérant, thèse du Trader, verdict du Risk Manager ; briefs et
débat s'y ajouteront en Phase 2/3). `run._exec_open` le range dans le meta du ticket, et
`_record_close` le rend avec le trade fermé. **C'est le prérequis de l'attribution par rôle
(Phase 4)** : sans lui, on sait qu'un trade a perdu, pas qui l'a produit.

**1C-C — Garde entrée/marché** (`run.Orchestrator._entry_guard`). Les ordres partent au
marché (`TRADE_ACTION_DEAL`) : le niveau `entry` annoncé par le LLM n'est jamais le prix
d'exécution, il ne sert qu'à calculer la distance au stop, donc **le lot et le R:R**. Une
entrée à 40 pips du marché dimensionnait le trade sur une distance fictive. Désormais :
au-delà de `AGENT_MAX_ENTRY_DRIFT_ATR × ATR` → refus (niveau périmé ou inventé) ; sinon
l'entrée est **recalée sur le prix exécutable** (ask/bid) et le R:R recalculé sur la réalité.
Le meta conserve les deux (`entry_planifiee` = niveau annoncé, `entry_sizing` = niveau réel,
`derive_entree_pips`).

**1C — Isolation SMTP des tests** (`tests/_isolation.py`). Un `Orchestrator` de test
construisait un `Mailer` à partir du vrai `.env` : le moindre trade simulé déclenchait un
envoi réel depuis le compte de l'utilisateur, dans un thread daemon dont l'échec passait
inaperçu. La socket SMTP est coupée pendant les tests (la logique de formatage et le cas
« serveur injoignable » restent testés). Effet de bord : suite passée de ~34 s à ~3 s.

**1D — Journal des cycles** (`desk/journal.py`). Chaque cycle archive son **dossier
d'entrée complet** (compte, scan, charts, news, stratégies, bilan, leçons) et le **plan
rendu**, en rotation (`EVAL_JOURNAL_KEEP`). C'est ce qui rend un cycle rejouable et deux
cerveaux comparables sur les mêmes données. Ne lève jamais : une panne de journalisation
n'empêche pas de trader.

**1D — Mode ombre** (`EVAL_SHADOW=1`). Le cerveau décide, son plan est journalisé,
**aucune action n'est exécutée**. Les protections déterministes (trailing, stop d'urgence,
panique perte-jour, garde week-end) continuent de tourner. Sert à observer un nouveau
cerveau en conditions réelles sans lui confier l'argent.

**1D — Harnais de rejeu** (`desk/replay.py`). Pour chaque ouverture journalisée, déroule
les bougies postérieures à la décision → stop ou cible touché en premier → **R réalisé**,
puis expectancy / winrate / profit factor / drawdown en R, ventilés par stratégie et par
symbole, et **propositions du cerveau vs trades réellement exécutés** (ce que le veto FTMO
a évité… ou coûté). `replay_cycles()` rejoue un cerveau sur des dossiers archivés pour
comparer solo vs desk sur exactement les mêmes données.

```bash
python desk/replay.py                # toutes les ouvertures journalisées
python desk/replay.py --shadow       # uniquement les cycles observés en mode ombre
python desk/replay.py --roles        # revue par employé (ni MT5 ni LLM)
python desk/replay.py --rejouer solo # re-décider sur les mêmes dossiers (LLM facturé)
```

⚠️ **Ce que cette mesure n'est pas** : hors frais (option `--cout-r`), chemin intra-bougie
inconnu (stop ET cible dans la même bougie → tranché pour le **stop**, hypothèse
pessimiste), sans sizing ni règles FTMO. Elle mesure la **qualité du signal**, pas la courbe
d'equity — et sur quelques dizaines de trades elle ne prouve rien. C'est un garde-fou contre
l'illusion, pas un certificat de performance.

---

## 9. État courant / reprise

**Toutes les phases sont livrées et testées** (0, 1, 1B, 1C, 1D, 2, 3, 3B, 4, 5).
- Modules livrés : `desk/base.py`, `desk/context.py`, `desk/gerant.py`,
  `desk/trade_manager.py`, `desk/trader.py`, `desk/risk_manager.py`, `desk/desk.py`,
  `desk/journal.py`, `desk/replay.py`, `desk/vigie.py`, `desk/analysts.py`,
  `desk/debat.py`, `desk/risque.py`, `desk/situation.py`.
- Câblage : `config.DeskConfig` + `AGENT_MODE`, `config.EvalConfig` (`EVAL_*`),
  `run._lazy_agent` (desk/solo), gate FTMO exposé au Gérant
  (`summary["ouvertures_bloquees"]`), plafond de risque du Risk Manager
  (`FTMOEngine.validate(risk_pct_override=...)` + `_exec_open`), leçons par rôle
  (`Memory.relevant_lessons_text(roles=...)`), accès contexte (`tools.cycle_context()`),
  garde d'entrée + dossier de décision dans `_exec_open`, journal/ombre dans `cycle()`,
  rotation `Store.trim_events`.
- Tests : `tests/test_desk.py` (11) — flux d'ouverture, durcissement Risk Manager, gestion
  du book, et **toute la matrice de dégradation** ; `tests/test_mesure.py` (22) — garde
  d'entrée, dossier de décision, journal, simulateur, mode ombre bout en bout (+ la
  contre-preuve : le même plan s'exécute quand l'ombre est coupée) ; `tests/test_vigie.py`
  (17) — escalade, session ciblée, anti-spam, pannes ; `tests/test_analystes.py` (15) —
  dossiers spécialisés, indépendance, normalisation ; `tests/test_debat.py` (14) — ordre du
  débat, adaptativité, verdict contraignant ; `tests/test_risque_college.py` (11) —
  unanimité négative, arbitrage borné ; `tests/test_apprentissage.py` (14) — signature,
  distance, attribution par rôle ; `tests/test_bilan_roles.py` (11) — revue par employé et
  refus de conclure sur un petit échantillon. Suite complète : **197 passed**.
- Modèle actif = `us.deepseek.v3-v1:0` (mode JSON) → le desk est nativement compatible.

**Ce qui tourne** (`AGENT_MODE=desk`, défaut) : Gérant → Trade Manager → (si convoqué &
ouvertures permises) 4 analystes → débat Bull/Bear + juge → Trader (avec mémoire
situationnelle) → collège du risque + arbitrage DG → moteur FTMO déterministe → exécution ;
entre deux cycles, Vigie → DG → session extraordinaire ; à la clôture, attribution par rôle.
Repli pilote déterministe préservé à l'identique.

**Coût par cycle** (ordre de grandeur, `DESK_MAX_CANDIDATS=2`) : ~2 appels au repos
(Gérant + Trade Manager) ; ~20 quand le desk est convoqué (8 analystes + 6-10 débat/juge +
1 Trader + 4 risque/arbitrage). Leviers : `DESK_MAX_CANDIDATS`, `DESK_USE_ANALYSTS`,
`DESK_USE_DEBATE`, `DESK_RISK_DEBATE`, `DESK_DEBATE_ROUNDS`, et les modèles à deux vitesses.

**Prochaine étape** : plus de code — de la **mesure**. Faire tourner en ombre, puis rejouer.

**À faire tourner dès que possible** : `EVAL_SHADOW=1` sur quelques jours, puis
`python desk/replay.py --shadow` — c'est le seul moyen de savoir si le desk vaut
mieux que le solo avant de lui confier le compte.

Lancer les tests : `python -m pytest tests -q`  (Python système ; le `.venv` est vide)
Basculer temporairement en solo : `AGENT_MODE=solo python run.py`


---

## 10. Déploiement — ce qui ne se voit qu'en production

Le dossier part seul sur une machine. Quatre défauts réels ont été corrigés pour ça
(`process.py`, `notify.py`, `store.py`, `run.py`), chacun couvert par
`tests/test_deploiement.py` (19 tests).

**Instance unique.** SQLite en WAL laisse deux process ouvrir la même base sans broncher :
deux agents traderaient le même compte, chacun ignorant les ordres de l'autre — risque
doublé et perte journalière FTMO faussée. `run()` prend maintenant un **verrou système**
(`state/agent.lock`, `flock` / `msvcrt.locking`) et **refuse de démarrer** (code de sortie
`3`) si un autre process le tient. Un verrou OS, pas un fichier PID : il tombe avec son
propriétaire, même après un crash, donc il ne bloque jamais un redémarrage légitime.

**Arrêt propre.** `SIGTERM` (systemd, docker) et `SIGINT` ne tuent plus le process : ils
**demandent** l'arrêt. La boucle termine ce qu'elle fait, le `finally` ferme le broker,
**vide la file d'emails**, replie le WAL et libère le verrou. Un second signal tue tout de
suite — l'opérateur pressé garde le dernier mot. L'attente entre deux cycles utilise
`Event.wait` et non `sleep` : un arrêt rend la main immédiatement au lieu d'attendre la
fin du pas de watchdog. Les positions ouvertes ne sont **pas** fermées à l'arrêt : elles
restent protégées par leur SL/TP chez le broker.

**Emails.** Un thread par message, tous daemon : à l'arrêt du process ils mouraient avec
lui et **l'alerte ne partait jamais** — précisément celle qu'on veut recevoir. Désormais
une **file bornée** (100) et **un seul worker**, **3 tentatives** avec attente croissante,
et `flush()` appelé dans le `finally`. File pleine : un message urgent chasse le plus
ancien message ordinaire. `send()` ne bloque ni ne lève jamais — le trading passe avant
la notification.

**Journal SQLite.** Le WAL est replié (`wal_checkpoint(TRUNCATE)`) à la fermeture, sans
quoi `agent.db-wal` grossit indéfiniment et une sauvegarde du seul `.db` serait incomplète.
`purge_events(jours)` supprime les événements **opérationnels** de plus de
`AGENT_EVENT_RETENTION_DAYS` (90) — jamais les types permanents (`trade_closed`,
`order_sent`, `risk_veto`, urgences) : perdre un trade clôturé reviendrait à effacer
l'expérience de l'agent. Création du store partagé protégée par verrou (le worker email,
le watchdog et la boucle peuvent le demander en même temps).

### Mettre en service

```bash
python run.py --status      # inspecte l'état persistant, ne trade pas
python run.py --test-mail   # vérifie SMTP par le vrai chemin (file + réessai)
python run.py --loop        # production
```

Codes de sortie : `0` arrêt normal, `1` erreur, `3` **une autre instance tourne déjà**.
`AGENT_STATE_DIR` déplace SQLite et le verrou hors du dossier si le code est en lecture
seule. Un `systemctl stop` / `docker stop` suffit à arrêter proprement : ne pas utiliser
`kill -9`, qui ne laisse ni le temps d'envoyer les alertes ni de replier le WAL.

---

## 11. Sortie des dépendances MT5 hors exécution (2026-08-19)

Deux CSV nourrissaient la couche news : `calendar_history.csv` (écrit par un indicateur
`ExportCalendar.mq5` chargé sur un graphique) et `macro_features.csv` (écrit par un service
horaire `tools/macro_service.py`, qui traînait `torch` + `transformers` pour FinBERT).

**Pourquoi c'était à jeter.** L'audit du 19/08 a trouvé les deux fichiers périmés — 36 et
34 jours. Or `_calendar()` préférait le CSV **dès qu'il existait**, sans regarder sa date :
`calendar_ok` restait à `True`, aucun événement à venir n'était trouvé, et le black-out news
ne bloquait plus rien **en silence**. Le même jour, le calendrier web voyait 16 événements,
dont un CPI britannique à fort impact dans les deux heures. Un garde-fou FTMO qui ment sur
son propre état est pire que pas de garde-fou.

**Ce qui remplace.**

| Avant | Après |
|---|---|
| `calendar_history.csv` (indicateur MT5) | `data/calendar_web.py` — flux faireconomy, miroir ForexFactory : **la source de la règle news FTMO** |
| `macro_features.csv` (service + FinBERT) | `data/macro_web.py` — momentum des taux FRED par devise + surprises, calculé dans le cycle |

`tools/` est supprimé en entier. Plus aucun fichier local dans le chemin de décision : ce
qui n'existe pas ne peut pas périmer.

**Le biais macro, honnêtement.** Le socle est le **momentum des taux** sur ~90 jours, une
série FRED par devise (`NewsFeed.FRED_TAUX`) : quotidiennes et fraîches pour USD (DGS2),
EUR (ECBDFR) et GBP (IUDSOIA) ; mensuelles OCDE pour les cinq autres, avec ~2 mois de
retard — d'où le champ `age_jours` exposé au LLM et la `fiabilite` qui tombe à « faible »
au-delà de 45 jours. Le calcul de **surprise** (actual vs forecast, pondéré par
l'importance, signe inversé pour le chômage) est implémenté et testé mais vaut 0
aujourd'hui : le flux faireconomy ne publie que `forecast` et `previous`, et les
calendriers qui donnent l'`actual` sont payants (Finnhub et EODHD répondent 403 sur nos
clés ; le compte guest de Trading Economics est fermé). Il s'allumera seul le jour où une
source d'`actual` sera branchée. `data/macro_web.py` reste une **fonction pure** — tout
l'accès réseau vit dans `news.py` — ce qui rend les 13 tests hors-ligne.

**Sources retirées.** Google Custom Search (`data/web.py` : DuckDuckGo devient le seul
moteur, plus aucune clé) et Reddit (`data/sources.py` : le sentiment social par lexique sur
un JSON public était le maillon le moins fiable de la chaîne).

**Modèle.** Bascule sur **Nova 2 Lite** (`us.amazon.nova-2-lite-v1:0`). Piège vérifié :
`amazon.nova-2-lite-v1:0` sans le préfixe `us.` est refusé par Bedrock
(`ValidationException`) — c'est un profil d'inférence, pas un modèle. Nova 2 Lite **sait
appeler des outils** (vérifié : `stopReason=tool_use`), donc `BEDROCK_TOOL_MODE=auto` prend
la voie tool calling et `DESK_ANALYSTES_OUTILS=1` devient possible — il reste à `0` tant
que la mesure en mode ombre n'a pas tourné. `us.amazon.nova-pro-v1:0` est disponible sur le
compte si l'on veut un modèle plus profond pour les rôles lourds (`DESK_MODEL_FORT`) ;
Nova 2 Pro et Nova Premier ne le sont pas.

**LangChain 1.x (2026-08-19).** Les dependances n'etaient installees sur aucun Python de la
machine : `ChatBedrockConverse` manquait, donc le desk tombait en degrade -> SafePilot a
chaque cycle, sans jamais decider. Installation faite — et `pip` sert desormais **LangChain
1.x**, ou `AgentExecutor` et `create_tool_calling_agent` **n'existent plus**. Comme les deux
imports etaient sous garde (`_TOOLS_OK`), la voie tool calling se serait desactivee en
silence : precisement ce que le passage a Nova venait de debloquer. `desk/base.py` et
`brain/agent.py` sont migres vers `create_agent` (graphe langgraph), le budget d'outils
passe par `recursion_limit` (2 x max_iter + 2) et `GraphRecursionError` est rattrape pour
garder ce qui a deja ete lu, comme le faisait `max_iterations`. Autre rupture 1.x : un
`@tool` est un `StructuredTool` qui ne s'appelle plus directement (`.invoke({...})`) — sans
effet en production, qui passe par les fonctions privees, mais les tests sont adaptes.
Verifie en reel sur Nova 2 Lite : appel simple OK, et boucle d'outils complete (l'agent
appelle l'outil, l'observation revient dans `intermediate_steps`).

**Sources retirees apres mesure (2026-08-19).** Trois API annoncees comme actives ne
rendaient rien, et le disaient mal :

| Source | Mesure | Verdict |
|---|---|---|
| GDELT | HTTP 429 « one request every 5 seconds », ~43 s par cycle, titres en arabe malgre `sourcelang=english`, requetes tirees d'affilee sans cadence ni cache | retiree |
| Finnhub | 403 sur `/stock/social-sentiment?symbol=XAUUSD` ; `fundamentals()` = `{}` | retiree |
| EODHD | 403 (palier gratuit = EOD seulement) | retiree |

Le point commun avec le bug du calendrier : un garde `startswith("{")` avalait la reponse
d'erreur en silence. L'analyste Actualite tournait sur les seuls flux RSS **sans que rien ne
le signale**, et rendait des briefs neutres qu'on prenait pour de la prudence. Regle qui en
sort : une source qui echoue doit LE DIRE avec le code HTTP et le debut du corps ; une cle
qui ne rend rien est pire qu'une source absente, parce qu'elle fait croire la colonne
couverte. L'outil `search_news` disparait avec GDELT — `web_search` (DuckDuckGo) couvre
deja l'enquete libre. Reste actif : calendrier web, FRED, RSS, FXSSI, myfxbook.
Effet mesure : le snapshot news passe de ~45 s a ~25 s (le reste est FRED, 11 series en
sequence, cache 30 min).

**Biais de l'or (2026-08-19).** L'analyste Fondamental rendait `neutre/0.0` a chaque cycle
sur XAUUSD, et ce n'etait pas une panne : XAU n'a ni serie de taux directeur ni evenement
au calendrier, donc son biais macro se reduisait au dollar. Trois series FRED — deja
payees par la cle existante — portent l'essentiel du cours de l'or :

| Serie | Role | Poids |
|---|---|---|
| `DFII10` | taux reel 10 ans (TIPS) | **-0.5** — des taux reels qui montent pesent sur l'or |
| `DTWEXBGS` | dollar index large | **-0.3** |
| `T10YIE` | point mort d'inflation 10 ans | **+0.2** — soutient l'or |

Chaque variation sur 90 jours est normalisee (tanh) avec une echelle calee sur l'amplitude
typique du mouvement, puis ponderee et bornee. Le `detail` chiffre (variation 90 j, derniere
valeur, effet sur l'or) remonte tel quel dans le dossier : le filtre de preuves du desk
n'accepte que des valeurs CITABLES, et « momentum -0.295 » n'en est pas une — « taux reel
10 ans +0.26 pt sur 90 jours » en est une. Quatre tests verrouillent les signes : se tromper
la produirait un biais rigoureusement contraire au marche.

Au passage, les series FRED sont desormais tirees EN PARALLELE (`_fred_lot`) : 14 series en
sequence coutaient ~25 s a chaque reconstruction du snapshot, sur le chemin critique d'un
cycle.
