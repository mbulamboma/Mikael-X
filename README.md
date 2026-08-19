# Trading — entreprise d'agents FTMO

Ce dépôt ne contient plus qu'un projet : **[`ai-company/`](ai-company/)**, une entreprise
d'agents LLM qui gère un compte FTMO (LangChain + AWS Bedrock + MetaTrader 5), avec un
moteur de risque déterministe comme plancher non négociable.

Tout est là-bas : [`ai-company/README.md`](ai-company/README.md) pour l'usage,
[`ai-company/IMPLEMENTATION.md`](ai-company/IMPLEMENTATION.md) pour l'architecture et
l'organigramme des rôles.

```bash
cd ai-company
pip install -r requirements.txt
cp .env.example .env      # puis renseigner MT5 / Bedrock / FTMO
python run.py
```

`ai-company/` est **autonome** : on copie ce dossier sur une machine, on remplit `.env`,
il tourne. Rien au-dessus de lui n'est requis.

## Ce qui a été supprimé, et pourquoi

Le dépôt hébergeait aussi une famille d'Expert Advisors MQL5 et un projet de recherche ML
(`MIKAEL_IA`, `MIKAEL_DONCHIAN`, `MIKAEL_MACRO`, `v3_outcome`, `v4_macro`, notebooks et
modèles ONNX). Tout a été retiré au profit d'un seul projet.

Les deux briques dont l'agent **dépend réellement** ont été rapatriées dans
[`ai-company/tools/`](ai-company/tools/) plutôt que supprimées :

| Outil | Produit | Consommé par |
|---|---|---|
| `tools/ExportCalendar.mq5` | `calendar_history.csv` | black-out news (sans lui, il ne protège plus rien) |
| `tools/macro_service.py` | `macro_features.csv` | biais macro par devise (dégradation douce) |

Voir [`ai-company/tools/README.md`](ai-company/tools/README.md).

## Récupérer un fichier supprimé

`MIKAEL_DONCHIAN`, `MIKAEL_MACRO`, `v3_outcome` et `v4_macro` restent dans l'historique
git — leur suppression est un commit, pas une perte :

```bash
git log --oneline --all -- MIKAEL_DONCHIAN
```

En revanche `MIKAEL_IA/`, `dataset.parquet`, `v3_outcome/raw_dataset/` et les caches de
`v4_macro/` n'étaient **pas suivis** (gitignorés) : ils sont définitivement perdus.

## Licence

Voir [LICENSE](LICENSE).
