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
- des contrats immuables et versionnés pour définitions de marché, bougies,
  trades, carnets, contextes marché et provenance ;
- un moteur de sessions IANA fail-closed pour `europe_open`, `us_cash_open` et
  `video_us_1500`, avec calendriers checksumés, DST et demi-sessions ;
- un inventaire en lecture seule des datasets utiles d'HyperBot et TRIDENT ;
- un plan d'acquisition qui distingue données réutilisables, données locales à
  qualifier et intervalles réellement absents ;
- des adaptateurs checksum-first H0/H1/L sans transport réseau, avec validation
  des chaînes H1 et rejets legacy séparés ;
- un store normalisé append-only, content-addressed et reproductible, dont la
  promotion exige un rapport de qualification checksumé ;
- un moteur D2 de bougies 1m/5m, gaps explicites, Opening Drive causal,
  percentiles historiques exclusifs, pivots confirmés et parité OHLCV ;
- une stratégie v0 exacte-once sur snapshots immuables et un superviseur de
  risque pur, sans gateway d'exécution ;
- quatre modèles de replay déterministes OHLC/trades-BBO, avec impact, frais
  versionnés, funding, latence et sorties fail-closed ;
- des rapports de replay JSON/Markdown immuables et checksummés, sans appel
  réseau ;
- des études D5 mono-marché et mono-étage de preuve, avec entonnoirs, MFE/MAE,
  métriques économiques descriptives et parité sous-jacent/Hyperliquid.

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

Le moteur D1C est livré, mais aucune archive partagée volumineuse n'est importée
automatiquement. Un import reste `candidate` jusqu'à l'audit événementiel de sa
couverture et la publication d'un rapport de qualification checksumé.

## Qualification H1 bornée

La commande suivante relit et valide intégralement un segment partagé, tout en
ne normalisant que le marché et les canaux explicitement demandés :

```bash
uv run python scripts/qualify_hyperbot_segment.py \
  --source-manifest /workspaces/hyperbot/data/server-fetches/EXAMPLE/payload/data/raw/collector/public-market-data/manifest.json \
  --segment 2026-08-21-000618.jsonl.gz \
  --market BTC \
  --channels trades bbo
```

Il n'y a aucun appel réseau. Une chaîne, séquence ou checksum invalide bloque le
batch. La qualification exige aussi zéro rejet/doublon/gap critique et un BBO
continu sous le cap configuré. Les records normalisés restent ignorés par Git ;
seuls les petits rapports et leurs sidecars sont publiables.

Après qualification, le plan local-first est régénéré sans écraser la baseline :

```bash
uv run python scripts/plan_data_acquisition.py \
  --config config/research_post_qualification.toml
```

Les calendriers utilisés par une expérience sont des fichiers TOML stricts et
versionnés par leur SHA-256. Une date hors de leur plage déclarée retourne un
état inconnu ou un rejet explicite ; BOT05 ne prolonge jamais implicitement le
calendrier de l'année précédente.

## Audit causal BTC 13:30–13:45 UTC

Une fois les trois qualifications locales présentes, les commandes suivantes
vérifient leur couverture puis dérivent l'Opening Drive sans accès réseau :

```bash
uv run python -m bot05.data.report \
  --config config/research_btc_open_qualified.toml
uv run python scripts/audit_opening_drive_features.py
```

Le second script vérifie à nouveau les manifests, records et rapports de
qualification, construit 1m et 5m, compare les 5m directes au rollup des 1m et
publie un rapport immuable sous `reports/features/`. La parité officielle H0
reste explicitement en attente tant qu'une source checksummée commune n'est pas
disponible.

L'absence locale de H0 et d'un historique de vingt sessions est publiée sans
fetch réseau :

```bash
uv run python scripts/audit_d2_data_gates.py
```

Le rapport distingue strictement les fenêtres `manifest-only` des datasets
qualifiés. Cette limite de données bloque la fermeture D2, mais pas les tests
synthétiques de la stratégie et du risque D3.

## Données

La priorité d'acquisition est :

1. dataset BOT05 déjà qualifié ;
2. dataset HyperBot partagé, checksumé et qualifié ;
3. archive publique H1 déjà présente ;
4. donnée TRIDENT legacy pour pré-recherche seulement ;
5. API publique Hyperliquid, limitée aux gaps restants.

Les racines partagées restent en lecture seule. BOT05 écrit uniquement ses
manifests, données normalisées, dérivés et rapports sous son propre dépôt.
