# BOT05 Agent Instructions

Ces instructions constituent le contexte persistant du projet BOT05.

## Sources de vérité

- La spécification stratégique et causale est `PLAN.md`.
- Le plan d'exécution et l'état courant sont dans `FOLLOW_UP.md`.
- En cas de contradiction, `PLAN.md` gagne sur la thèse, la causalité, le
  risque et les gates ; `FOLLOW_UP.md` gagne sur l'ordre et le statut des lots.
- `/workspaces/hyperbot` et `/workspaces/trident` sont des sources de données et
  des références historiques en lecture seule. Ne jamais les modifier depuis
  BOT05 sans demande explicite.

## Mission et sécurité

- BOT05 est autonome et n'a aucune dépendance runtime vers HyperBot ou TRIDENT.
- Aucun client de signature, gateway d'ordre ou chemin live ne doit être ajouté
  avant une autorisation explicite séparée.
- Toute configuration garde `live_enabled = false`, `shadow_only = true` et
  `public_data_only = true` pendant les lots de recherche.
- Les données locales sont inventoriées et qualifiées avant tout fetch. Une
  requête Hyperliquid ne peut viser que les marchés, canaux et intervalles
  encore manquants après cette qualification.
- Une donnée HyperBot partagée est classée H1 dans BOT05 tant que son schéma et
  sa couverture sont qualifiés. Elle ne devient jamais H2 rétroactivement.
- Les données TRIDENT restent legacy L sauf preuve explicite d'une archive
  publique H1 ; elles ne peuvent pas valider seules une exécution ou une
  promotion.

## Conventions de développement

- Python 3.11 minimum, package source dans `src/bot05/`.
- Code, identifiants et commentaires techniques en anglais ; documentation et
  rapports en français.
- Utiliser des types explicites et des modèles immuables pour les faits de
  marché, décisions, positions et expériences.
- Les données brutes sont append-only. Tout dérivé porte les checksums des
  sources, versions d'adaptateur, code et configuration.
- Toute logique de signal doit être causale et testée contre le lookahead.
- Préserver les changements non liés déjà présents dans le worktree.

## Commandes usuelles

- Installer/synchroniser : `uv sync`
- Tests : `uv run pytest`
- Lint : `uv run ruff check .`
- Format : `uv run ruff format .`
- Types : `uv run mypy src`
- Plan local-first : `uv run python scripts/plan_data_acquisition.py`

## Critères avant livraison

- Mettre à jour `FOLLOW_UP.md` avec toute livraison ou décision matérielle.
- Ajouter des tests proportionnés et vérifier sécurité, reproductibilité et
  fail-closed.
- Signaler tout impact sur collecte, stockage ou format de données.
- Ne jamais présenter un backtest comme une garantie ou comme une preuve live.

