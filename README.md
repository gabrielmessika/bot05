# BOT05

BOT05 est un projet de recherche autonome sur une stratégie directionnelle de
session Hyperliquid : impulsion d'ouverture, pullback causal au midpoint,
confirmation puis continuation. La thèse complète et ses critères de
falsification sont dans [`PLAN.md`](PLAN.md). L'état réel des lots est suivi
dans [`FOLLOW_UP.md`](FOLLOW_UP.md).

Le dépôt démarre volontairement sans capacité d'ordre. La configuration de
recherche impose `live_enabled = false`, `shadow_only = true` et
`public_data_only = true`. Aucun SDK de trading, secret ou gateway live n'est
présent.

## Premier jalon

Le lot initial fournit :

- un package Python 3.11 avec pytest, ruff et mypy strict ;
- une configuration TOML stricte et fail-closed ;
- des contrats immuables de couverture de données ;
- un inventaire en lecture seule des datasets utiles d'HyperBot et TRIDENT ;
- un plan d'acquisition qui distingue données réutilisables, données locales à
  qualifier et intervalles réellement absents ;
- un rapport versionnable, sans appel réseau.

Les données HyperBot partagées sont classées H1 dans BOT05, jamais H2. Les
données TRIDENT sont classées L par défaut. Un chevauchement local non encore
qualifié bloque le fetch correspondant : BOT05 doit d'abord auditer ce fichier,
puis demander à Hyperliquid uniquement le reliquat prouvé manquant.

## Démarrage

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
uv run python scripts/plan_data_acquisition.py
```

Le dernier script ne contacte pas Hyperliquid. Il lit les métadonnées et petits
manifests locaux, puis écrit le rapport configuré sous `reports/data_quality/`.
La fenêtre initiale est un smoke de qualification, pas un échantillon destiné à
conclure sur la rentabilité.

## Données

La priorité d'acquisition est :

1. dataset BOT05 déjà qualifié ;
2. dataset HyperBot partagé, checksumé et qualifié ;
3. archive publique H1 déjà présente ;
4. donnée TRIDENT legacy pour pré-recherche seulement ;
5. API publique Hyperliquid, limitée aux gaps restants.

Les racines partagées restent en lecture seule. BOT05 écrit uniquement ses
manifests, données normalisées, dérivés et rapports sous son propre dépôt.

