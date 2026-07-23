# Presets MIKAEL_DONCHIAN v2.13 — protocole "une config gelée, les variantes au tester"

## Règle du jeu (décidée le 23/07/2026)

**Le live tourne sur `BASELINE_v213_H1.set` et on n'y touche plus** jusqu'au
checkpoint des **~30 lignes** dans `MIKAEL_DONCHIAN_mfe_<magic>.csv`.
Toute envie de modifier un paramètre passe par le **Strategy Tester**, jamais
par le compte. Un changement à la fois. (Les 3 changements live du 21-23/07 —
trail 1.5→1.0, RR 1.5→1.0→1.5, TF H1→H4 — ont montré pourquoi : l'échantillon
forward devient illisible.)

## Fichiers

| Fichier | Rôle | Différence vs baseline |
|---|---|---|
| `BASELINE_v213_H1.set` | **Config live gelée** (= défauts du code, commit 70f6339) | — |
| `TEST_TRAIL10.set` | Variante tester | `InpTrailStartATR` 1.5 → **1.0** (trailing dès le BE ; motivé par le round-trip EURJPY +1.11 ATR → perte) |
| `TEST_H4.set` | Variante tester | `InpSignalTF` H1 → **H4** (16388) (moins de bruit/scratches ; SL plus larges, moins de trades) |
| `INDICES_DEMO.set` | **2ᵉ instance** indices (v2.14+) | `InpSymbols=US500,US100,US30,GER40`, magic **20260730**, martingale **OFF**, risque **0.25 %**, `InpMaxPerCcy=1` (1 seul indice à la fois : famille corrélée) |

## Remettre le live sur la baseline (VPS)

1. Graphe → clic droit sur l'EA → *Expert List / Properties* → onglet **Inputs** → **Load** → `BASELINE_v213_H1.set` → OK.
2. Vérifier dans le journal la ligne d'init : `TF=PERIOD_H1 ... RR=1.50 ... trail@1.5/1.2ATR`.
3. Les positions déjà ouvertes restent gérées (BE/trailing tournent en continu).

## Lancer les backtests (Strategy Tester)

1. Ctrl+R → Expert : `MIKAEL_DONCHIAN`, Symbole : `EURUSD`, Période : **H1**
   (l'EA est multi-paires via timer : le symbole du graphe importe peu, les 8
   paires de `InpSymbols` sont téléchargées automatiquement).
2. Modèle : **« Every tick based on real ticks »** (sinon les stops BE/zero-lock
   sont mal simulés). Dépôt : **100 000 USD**, levier 1:100.
3. Période : 12 mois minimum (inclure des régimes calmes ET volatils).
4. Onglet *Inputs* → **Load** → le `.set` à tester. Lancer. Noter PF, DD max,
   nb trades, profit net.
5. Comparer **chaque variante contre la baseline sur la même période exacte**.
   Une variante ne passe en live que si elle bat la baseline sur PF **et** DD,
   et pas seulement de quelques %.

## Limites connues du tester (honnêteté)

- **Pas de `macro_features.csv` dans le tester** → filtre sentiment
  **fail-open (inactif)**. Les backtests sur-tradent donc vs le live (le live
  refuse des signaux `sent_contre`). Comparer variante vs baseline reste
  valide (même biais des deux côtés) ; comparer tester vs forward ne l'est pas.
- Slippage de stops (type EURJPY minuit) non simulé → les scratches BE du
  tester sont plus « propres » que la réalité.
- Multi-devises + timer 30 s : exige « real ticks » et un historique complet
  des 8 paires (premier run long : téléchargement).

## Instance INDICES (v2.14) — règles

- **Jamais dans l'instance FX baseline.** Ouvrir un 2ᵉ graphe (n'importe quel
  symbole), attacher l'EA, **Load** → `INDICES_DEMO.set`. Magic 20260730
  (jamais adjacent aux magics existants — règle day-ticket).
- **Vérifier les noms de symboles** dans le Market Watch FTMO (US500 / US100 /
  US30 / GER40 — adapter `InpSymbols` si le broker suffixe autrement).
- v2.14 route automatiquement les non-FX : spread contrôlé en **fraction
  d'ATR** (`InpMaxSpreadATR`, 5 %), corrélation = **une seule famille** (tous
  les indices comptent ensemble, cap `InpMaxPerCcy`), sentiment **skippé**.
- Défauts volontairement prudents : martingale OFF, risque 0.25 %, 1 indice à
  la fois. Marché **non validé** → backtest puis démo avant tout compte réel.
- ⚠️ Risque structurel assumé : **gaps de session/week-end** — un SL peut être
  sauté avec slippage bien pire que l'EURJPY du 21/07. L'EA ne ferme pas avant
  les clôtures de session.

## Analyse du log MFE (checkpoint ~30 trades)

```
python ..\analyse_mfe.py <chemin>\MIKAEL_DONCHIAN_mfe_20260713.csv
```

Le script sort la séparation gagnants/perdants par seuil de MFE (calibration
du zero-lock), le taux de scratch et son coût net, et les round-trips
(calibration du trail-start).
