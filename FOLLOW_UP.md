# BOT05 — plan de développement et suivi

**Dernière mise à jour :** 27 août 2026

**Spécification :** [`PLAN.md`](PLAN.md)

**État global :** D0/T0, D1A et D1B livrés ; D1C est le prochain lot. Aucune
capacité d'ordre et aucun appel réseau dans les lots livrés.

## 1. Rôle de ce document

`PLAN.md` fixe la thèse, les définitions causales, les modèles d'exécution et les
gates de promotion. Ce document ordonne les livraisons, sépare les lots de
développement des lots de test et consigne uniquement ce qui est réellement
implémenté et vérifié.

Statuts utilisés : `À faire`, `En cours`, `Terminé`, `Bloqué`.

## 2. Principes non négociables

- aucun client de signature, secret ou gateway d'ordre avant autorisation ;
- `live_enabled = false`, `shadow_only = true`, `public_data_only = true` ;
- aucun import runtime depuis `/workspaces/hyperbot` ou `/workspaces/trident` ;
- les deux dépôts partagés restent strictement en lecture seule ;
- inventaire local, qualification, calcul des gaps, puis fetch public limité aux
  seuls gaps prouvés ;
- les datasets HyperBot partagés sont H1 dans BOT05, jamais H2 rétroactif ;
- les datasets TRIDENT sont L par défaut et ne valident jamais seuls une
  exécution ou une promotion ;
- données brutes append-only, dérivés reproductibles et checksummés ;
- fail-closed sur config inconnue, provenance invalide, gap critique, stale data
  ou ambiguïté temporelle.

## 3. Lots de développement

| Lot | Contenu | Statut | Critère de sortie |
|---|---|---|---|
| D0 | dépôt, règles, packaging, config sûre, suivi | Terminé | package installable, aucun chemin live, commandes qualité vertes |
| D1A | inventaire local-first HyperBot/TRIDENT et plan de gaps | Terminé | rapport déterministe, aucune lecture massive ni requête réseau |
| D1B | contrats marché/candle/trade, calendrier et provenance | Terminé | modèles immuables, DST/holidays et checksums couverts |
| D1C | store append-only et adaptateurs H0/H1/L | À faire | import bit-identique, rejets séparés, sources inchangées |
| D2 | bougies 1m/5m et features causales | À faire | aucune fuite, gaps explicites, parité officielle |
| D3 | stratégie v0 et superviseur de risque | À faire | machine d'état exact-once et fail-closed |
| D4 | replay déterministe et coûts | À faire | modèles conservateur/central/stress reproductibles |
| D5 | qualification des datasets et études par marché | À faire | rapports séparés U/H0/H1/H2/L, limites publiées |
| D6 | ablations, walk-forward et OOS verrouillé | À faire | toutes variantes publiées, holdout non réoptimisé |
| D7 | collector et runner shadow publics | À faire | zéro signature, observabilité et procédures incident |
| D8 | canary éventuel | Bloqué | décision utilisateur et gates de `PLAN.md` requises |

## 4. Lots de test

| Lot | Portée | Dépend de | Statut | Gate |
|---|---|---|---|---|
| T0 | packaging, config stricte, absence de live/secrets | D0 | Terminé | pytest + ruff + mypy verts |
| T1 | provenance, lecture seule, checksums, gaps et déterminisme | D1A–D1C | En cours | sources inchangées, report bit-identique |
| T2 | DST, jours fériés, bougies, lookahead et pivots | D1B–D2 | En cours | calendrier couvert ; features/pivots attendent D2 |
| T3 | transitions stratégie, refus risque, exact-once | D3 | À faire | invariants long/short et fail-closed |
| T4 | intrabar, gaps, fees, slippage, funding, latence | D4 | À faire | central et stress couverts |
| T5 | intégration dataset → signal → replay → rapport | D2–D5 | À faire | répétition bit-identique et audit des coûts |
| T6 | walk-forward, bootstrap, ablations et holdout | D6 | À faire | contrôles faux positifs et concentration |
| T7 | shadow, stale, reprise, divergence et kill path | D7 | À faire | 60 sessions/30 signaux avant toute proposition |

## 5. Lot livré — D0 / T0

### Livré dans le worktree

- `AGENTS.md` formalise les sources de vérité, la lecture seule et le local-first ;
- `pyproject.toml` configure Python 3.11, pytest, ruff et mypy strict ;
- `config/research.toml` verrouille les modes sûrs et une fenêtre smoke ;
- `src/bot05/config.py` refuse les clés inconnues, les timestamps sans `Z`, le
  live, le non-shadow, les données privées et une politique remote-first ;
- aucune dépendance runtime et aucun module d'exécution n'existent ;
- `.gitignore` garde les données volumineuses et secrets hors Git.

Le lot est fermé : environnement synchronisé, package installé et trois gates
de qualité vertes. Les tests de sécurité vérifient l'absence de module
d'exécution, de dépendance runtime exchange et de lecture de secrets.

## 6. Lot livré — D1A / T1 partiel

### Contrat local-first

Le plan d'acquisition traite chaque besoin comme un triplet
`marché × canal × intervalle UTC` :

1. réutiliser les assets BOT05 déjà qualifiés ;
2. qualifier les candidats HyperBot H1 et TRIDENT L qui chevauchent le besoin ;
3. soustraire leur couverture qualifiée ;
4. produire uniquement les intervalles encore absents pour un futur fetch
   Hyperliquid public.

La configuration initiale garde `remote_fetch_enabled = false`. Le lot D1A
calcule donc les gaps sans les télécharger.

### Implémentation

- contrats immuables `DataAsset`, `TimeRange`, `DataRequirement` et
  `RequirementPlan` ;
- découverte par sidecars SHA-256 et métriques de fin des datasets replay
  HyperBot, sans charger leurs fichiers multi-Go en mémoire ;
- découverte des segments bruts physiquement présents dans les exports
  HyperBot ; leur continuité n'est fusionnée que lorsque les séquences de
  records sont contiguës ;
- réutilisation du manifest legacy HyperBot pour référencer les sources TRIDENT
  sans rescanner ni modifier les archives ;
- détection des éventuelles bougies 1m/5m déjà manifestées par TRIDENT ;
- calcul exact d'union/soustraction des intervalles half-open ;
- dérivation candidate de bougies 1m/5m depuis les trades, sous réserve d'un
  audit de complétude ;
- rapport JSON déterministe, sidecar SHA-256 et synthèse Markdown immuables.

### Limites connues

- un sidecar valide prouve l'identité du fichier, pas la complétude de chaque
  session ; la qualification événementielle reste D1C/D2 ;
- les replays HyperBot partagés ne couvrent actuellement en profondeur que BTC,
  ETH, HYPE et SOL sur certaines journées ;
- les marchés `xyz:*` présents dans le collector HyperBot n'ont pas encore de
  replay BOT05 qualifié ; ils doivent être recherchés dans les segments locaux
  avant tout fetch ;
- les candles TRIDENT 15m/30m/1h/2h ne remplacent pas les 1m/5m requises ;
- aucun dataset local n'est encore marqué `qualified` par BOT05.

### Résultat initial

Le rapport du 27 août recense 59 assets sans erreur d'inventaire : 35 candidats
H1 HyperBot et 24 candidats L TRIDENT. Les 42 besoins de la fenêtre smoke ont
un plan explicite. Les captures raw HyperBot sont détectées pour SILVER, SP500,
GOLD, HYPE, BTC, ETH et SOL ; elles doivent être qualifiées avant de calculer le
reliquat. Le canal `market_context` reste absent sur toute la fenêtre dans les
datasets actuellement indexés.

## 7. Ordre de travail immédiat

1. Implémenter D1C : store append-only et adaptateurs checksumés des candidats
   utiles uniquement.
2. Auditer la couverture événementielle des marchés et sessions cibles.
3. Générer un manifest de gaps ; seulement alors autoriser un fetch H0/H1 public.
4. Livrer D2 puis D3 sur fixtures synthétiques avant tout calcul de PnL.

## 8. Lot livré — D1B / T2 calendrier

### Contrats de domaine

`src/bot05/models.py` fournit des modèles gelés et typés pour :

- `DatasetProvenance`, avec tier, chemins/URL, période UTC, timezone source,
  versions code/config/calendrier/adaptateur et trois SHA-256 obligatoires ;
- `MarketDefinition`, y compris DEX, asset ID, decimals, tick, size step,
  leverage, marge, growth mode, deployer fee et statut ;
- `Candle`, qui refuse une observation antérieure à sa clôture et contrôle ses
  invariants OHLC ;
- `Trade`, `BookLevel`, `BookSnapshot` et `MarketContext` ;
- enveloppe JSON canonique v1, décimales sérialisées comme chaînes, rejet des
  champs inconnus et round-trip bit-à-bit pour les cinq types de records.

### Calendrier et sessions

`src/bot05/calendars/` fournit :

- les définitions distinctes `europe_open` à 09:00 `Europe/Paris`,
  `us_cash_open` à 09:30 `America/New_York` et l'ablation
  `video_us_1500` à 15:00 `Europe/Paris` ;
- un loader TOML strict dont le SHA-256 entre dans `calendar_version` ;
- une plage de validité obligatoire et fail-closed hors couverture ;
- week-ends, jours fermés et demi-sessions avec fermeture anticipée ;
- détection explicite des heures locales inexistantes ou ambiguës ;
- résolution UTC de `t0`, fin du drive, expiration du pullback et dernière
  sortie possible ;
- rejet si l'ouverture ou l'horizon sort de la session externe ;
- états XYZ `external_open`, `internal_oracle`, `closed` et `unknown`.

Les calendriers 2026 présents sous `tests/fixtures/calendars/` sont des fixtures
de validation, pas des sources de production. Tout calendrier de recherche réel
devra être acquis avec une source opérateur, checksumé et limité à sa période ;
aucune liste de jours fériés n'est extrapolée implicitement.

### Tests de sortie

La suite couvre les semaines où les DST US et Europe diffèrent, week-end,
holiday, demi-session, ouverture hors plage externe, horizon dépassant une
fermeture anticipée, bascule XYZ externe/interne, date hors version, temps local
inexistant/ambigu et construction causale des trois bougies 5 minutes clôturées.

## 9. Décisions techniques

### 2026-08-27 — séparation spécification / exécution

`PLAN.md` reste inchangé et sert de fondation. `FOLLOW_UP.md` porte les statuts,
preuves de tests, limites et décisions quotidiennes.

### 2026-08-27 — classification des données HyperBot

Le niveau A interne à HyperBot n'est pas recopié comme H2. Dans BOT05, ces
captures deviennent H1 partagées avec les flags
`source_declared_tier_A`, `shared_hyperbot_not_bot05_h2` et
`requires_bot05_schema_and_gap_qualification`.

### 2026-08-27 — fetch désactivé au bootstrap

Le planner ne contient aucun transport HTTP/WebSocket. Il rend visibles les
gaps et bloque l'acquisition distante tant que la configuration ne l'autorise
pas dans un lot ultérieur couvert par tests.

### 2026-08-27 — temps de domaine en UTC, règles en IANA

Les records de marché acceptent uniquement des timestamps UTC aware. Les
heures de session restent exprimées dans leurs timezones IANA et sont résolues
strictement à la date concernée ; une heure locale ambiguë ou inexistante est
rejetée au lieu de choisir silencieusement un `fold`.

### 2026-08-27 — calendrier réel jamais extrapolé

Le moteur est générique, mais une expérience doit fournir un fichier calendrier
checksumé avec plage de validité. Les fixtures de tests ne constituent pas une
source opérateur et ne seront pas utilisées pour une conclusion de recherche.

## 10. Preuves de validation

- `uv sync` : réussi, lockfile créé, package `bot05==0.1.0` installé ;
- `uv run pytest` : **45 tests réussis** ;
- `uv run ruff check .` : **réussi** ;
- `uv run mypy src` : **réussi en mode strict**, 11 fichiers source ;
- rapport JSON : `reports/data_quality/initial_local_coverage.json` ;
- SHA-256 du rapport :
  `096af9241b895f70e581d5e12110a74326898a77e08f00314f7dc447d516ab27` ;
- SHA-256 du code inclus dans le rapport :
  `36cd9ffe4c4ae5c86734ad94330d683c4d80eb613134ed7cd1a4f838629000d9` ;
- seconde génération : **identique**, même SHA-256, aucune baseline écrasée.

## 11. Impact opérationnel

- **Trading :** impossible ; aucun gateway ni dépendance d'exchange.
- **Réseau :** aucun appel effectué ou implémenté dans D0/D1A/D1B.
- **Données partagées :** lecture seule ; aucun fichier HyperBot/TRIDENT modifié.
- **Stockage BOT05 :** seuls petits manifests/rapports sont versionnables ; raw,
  normalized et derived restent ignorés hors `.gitkeep`.
