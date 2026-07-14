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

   **Démarrage automatique au boot du VPS** (PowerShell administrateur,
   adapter le chemin) :
   ```
   schtasks /Create /TN "MikaelX_MacroService" /SC ONLOGON ^
     /TR "C:\chemin\vers\Mikael-X\v4_macro\start_service.bat" /RL LIMITED /F
   ```
   Et activer la reconnexion/logon automatique du VPS pour que la session
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

## Garde-fous intégrés

- Budget de perte journalier prospectif (flottant + nouveau trade inclus), Max
  Loss statique, halt persistant, règle vendredi 22h, file d'attente spread,
  anti-dérive — calibrés FTMO 2-step (compte SWING requis).
- `macro_features.csv` périmé (>12 h) : MIKAEL_MACRO **ne trade plus** (les
  features macro sont des entrées du modèle) ; le veto sentiment de
  MIKAEL_DONCHIAN passe en inactif (fail-open) avec log.
