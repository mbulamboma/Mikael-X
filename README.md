# Mikael-X — laboratoire de forward-test FTMO (MT5)

⚠️ **FORWARD DEMO UNIQUEMENT.** Aucun des moteurs de signal n'a d'edge prouvé
(le modèle macro est NO-GO en backtest : R_net −0.038 après coûts). Ce dépôt
sert à exécuter le forward-test qui tranchera. **Ne jamais attacher à un
compte réel sans GO documenté** (voir `v4_macro/CRITERES_GO.md`).

## Contenu

| Dossier | Rôle |
|---|---|
| `MIKAEL_DONCHIAN/` | EA v1.14 — moteur Donchian (Turtle, sortie canal, S&R) + mode Scalp (pullback EMA8/21, sortie flip de biais) + veto sentiment FinBERT. Châssis FTMO complet. |
| `MIKAEL_MACRO/` | EA v2.11 — modèle ONNX macro (calendrier+FRED, 11 features) + veto FinBERT. Mode EXIT_SIGNAL : pas de TP, taille ∝ score, SL catastrophe 3×ATR (vol targeting). |
| `v4_macro/` | `macro_service.py` = cerveau data (calendrier MT5 + FRED + GDELT + FinBERT → `macro_features.csv` par devise, 1 run/h). `CRITERES_GO.md` = critères gelés du forward. |
| `v3_outcome/ExportCalendar.mq5` | Script MT5 : exporte `calendar_history.csv` (à re-glisser sur un graphique ~1×/semaine). |

## Installation VPS (Windows)

1. **MT5** (broker FTMO) installé et connecté au compte **DÉMO**.
2. **Python 3.12+** : `pip install -r requirements.txt`
   (1er run FinBERT = téléchargement ~440 Mo, ensuite en cache).
3. **Clés API** : copier `.env.example` → `.env` à la racine, renseigner
   `FRED_API` (gratuite : fred.stlouisfed.org) et `ALPHA_VANTAGE_API` (optionnelle).
4. **Chemin MT5** : définir la variable d'environnement `MT5_FILES` vers le
   dossier `...\MetaQuotes\Terminal\<ID>\MQL5\Files` du VPS
   (sinon le chemin par défaut du poste d'origine est utilisé).
5. **EA** : copier `MIKAEL_DONCHIAN/` et `MIKAEL_MACRO/` dans `MQL5\Experts\`,
   compiler les `.mq5` dans MetaEditor (0 erreur attendu, 1 warning bénin),
   ou utiliser les `.ex5` fournis.
6. **Calendrier** : glisser `ExportCalendar.mq5` sur un graphique →
   `MQL5\Files\calendar_history.csv`. À refaire ~1×/semaine.
7. **Service data** : lancer `v4_macro/start_service.bat` et le laisser ouvert
   (1 run/heure ; alimente `macro_features.csv` + l'historique forward).
   Le .bat se relance seul si Python crashe (`service_restarts.log`).

   **Mode VPS RECOMMANDE — le Planificateur fait tout** : au lieu du .bat en
   boucle (processus permanent = point de defaillance), programmer un run
   UNIQUE par heure via `run_once.bat`. Meme si un run plante, le suivant
   part quand meme : l'OS est le watchdog, les donnees restent fraiches en
   continu et la limite des 12 h ne peut plus etre atteinte que si le VPS
   lui-meme est mort. Commande TESTEE (invite de commandes ou PowerShell,
   droits utilisateur suffisants — adapter le chemin) :
   ```
   schtasks /create /tn "MikaelX_MacroService_Hourly" /sc hourly /mo 1 /f ^
     /tr "C:\chemin\vers\Mikael-X\v4_macro\run_once.bat"
   ```
   Verifier puis declencher un run test :
   ```
   schtasks /query /tn "MikaelX_MacroService_Hourly" /v /fo list
   schtasks /run   /tn "MikaelX_MacroService_Hourly"
   ```
   (Alternative : `start_service.bat` en boucle, a lancer au logon.)
   Activer la reconnexion/logon automatique du VPS pour que la session
   s'ouvre au redemarrage (MT5 en a besoin de toute facon).

   **Chaine de robustesse** : run horaire -> fraicheur max 12 h
   (`InpMacroMaxAgeH`) = 11 runs de marge avant que MIKAEL_MACRO ne se mette
   en securite ; GDELT en panne -> reprise des derniers scores (<24 h) ;
   FRED/calendrier en panne -> valeurs precedentes conservees dans le CSV.
   Si le VPS peut rester eteint plus de 12 h, augmenter `InpMacroMaxAgeH`
   (au prix de features plus rassises pour le modele).

## Lancement du forward-test (3 instances, magics distincts)

| Graphique | EA | Réglages non-défaut | Magic |
|---|---|---|---|
| 1 | MIKAEL_DONCHIAN | aucun (défauts = Donchian H1, 8 paires) | 20260713 |
| 2 | MIKAEL_DONCHIAN | `InpStrategy=STRAT_SCALP`, `InpSignalTF=M15`, `InpMaxHoldHours=2`, `InpMagic=20260715` | 20260715 |
| 3 | MIKAEL_MACRO | aucun (défauts = H4, EXIT_SIGNAL) | 20260714 |

Vérifier dans l'onglet **Experts** la ligne `init OK` de chaque instance
(paires (8), sent_filter/veto ON, risk %). Puis **ne plus rien toucher** :
règles et durée dans `v4_macro/CRITERES_GO.md`.

## Vérification post-déploiement (5 min, dans l'ordre)

1. **Service data** : `MQL5\Files\macro_features.csv` existe, en-tête =
   `ccy;sent24;sent72;sentmom;cnt24;surprise24;surprise72;fredmom;curvemom;updated_utc`
   (**10 colonnes**) et `updated_utc` < 1 h. 8 devises attendues (EUR USD JPY GBP AUD NZD CAD CHF).
2. **Tâche planifiée** : `schtasks /query /tn "MikaelX_MacroService_Hourly"`
   → Statut `Prêt`, prochaine exécution dans l'heure. Un `service_runs.log`
   apparaît dans `v4_macro/` après le premier run planifié.
3. **EA** : onglet Experts → ligne `init OK` de chaque instance, puis AUCUNE
   ligne `[MACRO] macro_features.csv absent/perime/ancien format`.
4. **Journaux** : `MQL5\Files\MIKAEL_MACRO_journal.csv` s'alimente à chaque
   bougie H4 (signal ou raison de skip).

## Dépannage (pannes réellement rencontrées)

| Symptôme | Cause | Remède |
|---|---|---|
| `[MACRO] ... ancien format — AUCUN trade` alors que le CSV est frais | Le service tournait avec un **code périmé chargé en mémoire** (Python ne recharge pas un script modifié) → CSV 8 colonnes | Tuer/relancer le service après CHAQUE mise à jour de `macro_service.py` (le mode Planificateur est immunisé : chaque run recharge le code) |
| `CUDA failure 801 ... GPU=-1` en boucle dans les logs | Le runtime ONNX de MT5 sonde un GPU NVIDIA absent | **Bénin** — la ligne suivante `ONNX: CPU selected` confirme le repli CPU. Ignorer |
| EA muet, log `== OBJECTIF DE PROFIT ATTEINT ==` inattendu | `InpInitialBalance` ≠ taille réelle du compte (ex. 10000 sur un compte 100k → cible +10 % « déjà atteinte ») | Mettre `InpInitialBalance` = solde initial réel du compte attaché |
| `ExportCalendar` : CSV vide, `err 5401` | Base calendrier MT5 pas encore synchronisée (timeout) | Ouvrir l'onglet Boîte à outils → Calendrier, puis relancer le script (v2 : tranches annuelles + retries) |
| `copy_rates_range` renvoie vide sur une grande plage | Téléchargement asynchrone de l'historique au 1er appel | Réessayer (les scripts font des tranches annuelles + retry) |

## Ressources

- **Clé FRED** (gratuite, requise) : https://fred.stlouisfed.org/docs/api/api_key.html
- **Python 3.12+ Windows** : https://www.python.org/downloads/windows/
- **MT5 FTMO** : espace client FTMO → Téléchargements (installer sur le VPS, compte DÉMO)
- **Package `MetaTrader5`** (inclus dans `requirements.txt`) : nécessite le terminal MT5 **installé et lancé** sur la même machine
- **FinBERT** (téléchargé automatiquement au 1er run, ~440 Mo) : cache dans `%USERPROFILE%\.cache\huggingface`
- **Règles FTMO** (daily loss / max loss / Swing) : https://ftmo.com/en/trading-objectives/

## Garde-fous intégrés

- Budget de perte journalier prospectif (flottant + nouveau trade inclus), Max
  Loss statique, halt persistant, règle vendredi 22h, file d'attente spread,
  anti-dérive — calibrés FTMO 2-step (compte SWING requis).
- `macro_features.csv` périmé (>12 h) : MIKAEL_MACRO **ne trade plus** (les
  features macro sont des entrées du modèle) ; le veto sentiment de
  MIKAEL_DONCHIAN passe en inactif (fail-open) avec log.
