# tools/ — les deux producteurs de données dont l'agent dépend

Ces fichiers vivaient auparavant dans `v4_macro/` et `v3_outcome/`, supprimés lors du
regroupement du dépôt autour de `ai-company/`. Ils sont **hors du cycle de décision** :
l'agent ne les appelle jamais. Ils écrivent des CSV dans le dossier `MQL5\Files` du
terminal MT5, que [`data/news.py`](../data/news.py) lit ensuite.

```
tools/ExportCalendar.mq5  ──(MT5)──▶  MQL5\Files\calendar_history.csv  ──▶  black-out news
tools/macro_service.py    ──(cron)──▶  MQL5\Files\macro_features.csv   ──▶  biais macro/devise
```

## 1. `ExportCalendar.mq5` — calendrier économique

Script MT5 à compiler dans MetaEditor et à exécuter sur un graphique. Il exporte
l'historique du calendrier économique dans `calendar_history.csv`.

**C'est la source du black-out news.** Sans ce fichier :

- `NEWS_FAIL_CLOSED=0` (défaut) → le black-out ne protège plus rien, et
  [`data/news.py`](../data/news.py) logue une **ERREUR** à chaque cycle ;
- `NEWS_FAIL_CLOSED=1` → **aucune entrée** n'est autorisée.

À ré-exécuter périodiquement (le calendrier avance).

## 2. `macro_service.py` — biais macro par devise

Service Python : calendrier + FRED + GDELT + FinBERT → un score de sentiment par devise
dans `macro_features.csv`. Sa disparition est une **dégradation douce** : le champ macro
du dossier news reste vide, l'agent continue sans ce signal.

```bash
python tools/macro_service.py            # un run
python tools/macro_service.py --loop 60  # boucle horaire
python tools/macro_service.py --no-news  # calendrier + FRED seuls (sans FinBERT)
```

Sur VPS, préférer `run_once.bat` via le Planificateur de tâches (un run horaire, aucun
processus permanent à surveiller) plutôt que `start_service.bat` (boucle + auto-restart).

### Dépendances

`torch` et `transformers` (FinBERT, CPU) ne sont **pas** dans le `requirements.txt` de
l'agent : ce service s'installe à part.

```bash
pip install -r tools/requirements.txt
```

### Configuration

Lit `ai-company/.env`, puis le `.env` du dossier parent en repli.

| Variable | Rôle |
|---|---|
| `FRED_API` | momentum des taux US (requis) |
| `ALPHA_VANTAGE_API` | complément de news, option `--av` (quota 25/jour) |
| `MT5_FILES` | force le dossier `MQL5\Files` ; sinon auto-détecté |

### État local

`cache/` (cache FinBERT — évite de re-scorer les titres déjà vus) et `history/` (chaque
run est appendé : dataset forward sans look-ahead). Les deux sont ignorés par git et
recréés automatiquement s'ils manquent.
