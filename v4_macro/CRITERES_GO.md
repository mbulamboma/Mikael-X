# Critères GO/NO-GO — écrits le 14 juillet 2026, AVANT le forward

**Règle d'or : ces critères ne seront JAMAIS modifiés après le début du test.**
Les modifier après coup = se mentir (cf. 6 falsifications V1→V3).

## Le test

- **Forward démo ≥ 6 semaines**, débuté à la date du premier run complet du
  service (`v4_macro/history/` fait foi).
- Deux candidats en parallèle, compte démo FTMO, mêmes conditions :
  - **EA-B** : MIKAEL_DONCHIAN v1.10+ (Donchian + veto sentiment), 8 paires,
    `InpRiskCashFixed=2`, magic 20260713.
  - **EA-A** : châssis MIKAEL + modèle tabulaire macro (n'existe que si sa
    propre validation offline donne GO), magic 20260714.
- `macro_service.py --loop 60` doit tourner pendant toute la période
  (VPS ou PC allumé). Les trous de service sont notés, pas maquillés.

## Critères de GO (TOUS requis, par EA)

1. **N ≥ 40 trades** clôturés sur la période (sinon : test prolongé, pas conclu).
2. **R_net moyen > 0** après spread + commission (2.50 $/côté/lot).
3. **Profit factor ≥ 1.15** (gains bruts / pertes brutes).
4. **Max drawdown de la période ≤ 5 %** de l'équity initiale du test.
5. **Pas de dépendance à un trade unique** : retirer le meilleur trade
   laisse le R_net moyen > 0.

## Décisions (pré-engagées)

- **Les deux échouent** → PAS de challenge. Retour à la case information
  (autres sources), jamais aux mêmes features.
- **Un seul passe** → il devient LE candidat. Le forward continue 4 semaines
  de plus en risque réel minuscule avant toute décision de challenge.
- **Les deux passent** → celui au meilleur profit factor est candidat,
  l'autre reste en observation.
- Un challenge FTMO ne peut être acheté qu'après : GO forward + 4 semaines
  de confirmation. Aucune exception, aucun « je le sens bien ».

## Journal des runs de service (trous, incidents)

| Date | Événement |
|---|---|
| 2026-07-14 | Création du service, premiers runs de test. Calendrier MT5 périmé (mai 2025) — à ré-exporter. |
