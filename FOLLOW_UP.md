# BOT05 — plan de développement et suivi

**Dernière mise à jour :** 29 août 2026

**Spécification :** [`PLAN.md`](PLAN.md)

**État global :** D0/T0, D1A, D1B, D1C, T1, D3/T3 et D4/T4 livrés. Les chaînes
D2/T2 et D5/T5 sont couvertes sur fixtures, mais leurs preuves externes sont
bloquées par les données locales : zéro asset H0 et seulement cinq dates
historiques candidates avant la cible, contre vingt requises. Aucune capacité
d'ordre et aucun appel réseau dans les lots livrés.

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
| D1C | store append-only et adaptateurs H0/H1/L | Terminé | import bit-identique, rejets séparés, sources inchangées |
| D2 | bougies 1m/5m et features causales | Bloqué | aucune fuite, gaps explicites, parité officielle |
| D3 | stratégie v0 et superviseur de risque | Terminé | machine d'état exact-once et fail-closed |
| D4 | replay déterministe et coûts | Terminé | modèles conservateur/central/stress reproductibles |
| D5 | qualification des datasets et études par marché | Bloqué | code livré ; historique multi-marché qualifié absent |
| D6 | ablations, walk-forward et OOS verrouillé | À faire | toutes variantes publiées, holdout non réoptimisé |
| D7 | collector et runner shadow publics | À faire | zéro signature, observabilité et procédures incident |
| D8 | canary éventuel | Bloqué | décision utilisateur et gates de `PLAN.md` requises |

## 4. Lots de test

| Lot | Portée | Dépend de | Statut | Gate |
|---|---|---|---|---|
| T0 | packaging, config stricte, absence de live/secrets | D0 | Terminé | pytest + ruff + mypy verts |
| T1 | provenance, lecture seule, checksums, gaps et déterminisme | D1A–D1C | Terminé | sources inchangées, report bit-identique |
| T2 | DST, jours fériés, bougies, lookahead et pivots | D1B–D2 | Bloqué | code couvert ; parité H0 externe indisponible |
| T3 | transitions stratégie, refus risque, exact-once | D3 | Terminé | invariants long/short et fail-closed |
| T4 | intrabar, gaps, fees, slippage, funding, latence | D4 | Terminé | central et stress couverts |
| T5 | intégration dataset → signal → replay → rapport | D2–D5 | Bloqué | fixtures vertes ; preuve multi-marché absente |
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

1. Qualifier les sources U/H0/H1/H2 par marché nécessaires aux rapports D5,
   sans agréger les étages de preuve.
2. Si une acquisition publique est autorisée, limiter H0 et l'historique BTC au
   déficit exact publié par l'audit D2 ; `remote_fetch_enabled` reste `false`.
3. Qualifier éventuellement les cinq fenêtres H1 candidates locales comme
   smoke supplémentaire, sans prétendre qu'elles satisfont le minimum de vingt.
4. Étendre ensuite la qualification aux marchés/session prioritaires avant tout
   calcul de PnL ou sélection portefeuille.

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

## 9. Lot livré — D1C / T1 partiel

### Normalisation checksum-first

- `src/bot05/data/normalizer.py` vérifie le SHA-256 complet d'une source JSONL
  ou JSONL gzip avant de la lire, conserve l'index et le checksum exact de
  chaque ligne et sépare les rejets sémantiques des erreurs d'intégrité ;
- les doublons bit-à-bit du domaine sont refusés avec un motif stable au lieu
  d'être insérés silencieusement ;
- une erreur de checksum, de chaîne ou de séquence H1 interrompt tout le batch,
  alors qu'un record intact mais incompatible est conservé dans le flux de
  rejets auditables.

### Adaptateurs bornés

- H0 adapte les réponses publiques `candleSnapshot` 1m/5m, contrôle les bornes,
  la clôture causale, le marché, l'intervalle et les décimales sous forme de
  chaînes ; aucun transport HTTP n'est présent ;
- H1 valide indépendamment les enveloppes HyperBot v2, les hashes payload et
  record, la chaîne, la séquence, le contexte d'horloge exchange et les
  timestamps avant d'adapter trades, BBO et L2 ; aucun import runtime HyperBot
  n'a été ajouté ;
- L adapte les trades JSONL TRIDENT en documentant l'hypothèse
  `received_at = exchange_time`. Les lignes L2 legacy ne contenant pas les
  tailles au meilleur bid/ask sont rejetées explicitement : BOT05 ne fabrique
  pas un `BookSnapshot` à partir des seules profondeurs à 10 bps.

### Store append-only

- les records normalisés, rejets et manifests sont séparés sous un segment
  content-addressed ; chaque fichier est créé exclusivement et une répétition
  identique est idempotente ;
- le manifest v1 porte les checksums source, records et rejets, versions
  d'adaptateur/code/config, marchés, canaux, types et bornes observées ;
- toute corruption post-écriture est détectée à la lecture ;
- un segment normalisé reste `candidate`, même avec zéro rejet. Il ne devient
  `qualified` qu'avec un rapport JSON checksumé lié au dataset, au segment, au
  brut et aux records, déclarant zéro gap critique, doublon et rejet et une
  couverture contenue dans les bornes observées.

### Limites et prochaine preuve

- aucun gros fichier HyperBot/TRIDENT n'a encore été importé dans BOT05 ; cette
  livraison qualifie le moteur et ses fixtures, pas les datasets réels ;
- la couverture min/max d'un segment n'est jamais assimilée implicitement à une
  continuité ; le prochain travail est de produire les rapports événementiels
  checksummés sur des segments locaux bornés ;
- aucun nouveau rapport de gaps ni appel réseau n'a été produit dans D1C.

## 10. Décisions techniques

### 2026-08-28 — normalisé ne signifie pas qualifié

L'absence de rejet de schéma prouve seulement que les records ont été adaptés.
La réutilisation par le planner exige en plus un rapport de qualification
checksumé prouvant la couverture déclarée et l'absence de gap critique.

### 2026-08-28 — un bucket absent reste un gap

L'agrégateur D2 ne fabrique jamais une bougie plate. Une couverture partielle,
une minute sans trade ou un composant 1m absent produit un gap typé. Le temps
`closed_at` conserve en outre le retard maximal de réception des trades.

### 2026-08-28 — seuil roulant lié à son scope

Un percentile causal porte le marché, la session et son instant `as_of`. Il ne
peut pas être appliqué à un autre marché/session, et la valeur courante reste
strictement exclue des 60 observations historiques.

### 2026-08-29 — D2 bloqué par une preuve de déficit, pas par le code

L'audit metadata-only ne trouve aucun asset H0 officiel sur la fenêtre commune.
Avant le 21 août, seulement cinq dates H1 ont leurs fichiers physiques et un gap
de manifest inférieur au cap de 15 secondes ; cette borne haute inclut le
week-end et reste donc quinze sessions sous le minimum causal de vingt.

### 2026-08-29 — stratégie fonctionnelle et risque sans effet de bord

La stratégie est un réducteur de snapshots immuables : une candle identique est
idempotente, une révision ou un gap invalide le setup et un état terminal ne
peut pas réentrer. Le superviseur de risque est pur ; l'application de sa
décision et la clôture de position retournent un nouveau ledger explicite.

### 2026-08-29 — aucune limite risque implicite

Tous les caps quantitatifs du `RiskLimits` sont obligatoires et leur hash est
porté par chaque décision. Le code ne choisit donc pas silencieusement un cap de
spread, slippage, levier ou staleness non enregistré.

### 2026-08-28 — L2 legacy incomplet rejeté

Les snapshots TRIDENT fournissant meilleur bid/ask et profondeur à 10 bps sans
taille au top ne sont pas convertis en `BookSnapshot`. Ils restent des rejets L
auditables et ne peuvent pas valider une exécution.

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

## 11. Preuves de validation

- `uv sync` : réussi, lockfile créé, package `bot05==0.1.0` installé ;
- `uv run pytest` : **92 tests réussis** ;
- `uv run ruff check .` : **réussi** ;
- `uv run mypy src` : **réussi en mode strict**, 25 fichiers source ;
- D1C couvre H0 causal, chaîne/séquence/checksums H1, limites L, source gzip en
  lecture seule, doublons, rejets séparés, import bit-identique, idempotence,
  corruption du store et gate de qualification vers le planner ;
- rapport JSON : `reports/data_quality/initial_local_coverage.json` ;
- SHA-256 du rapport :
  `096af9241b895f70e581d5e12110a74326898a77e08f00314f7dc447d516ab27` ;
- SHA-256 du code inclus dans le rapport :
  `36cd9ffe4c4ae5c86734ad94330d683c4d80eb613134ed7cd1a4f838629000d9` ;
- seconde génération : **identique**, même SHA-256, aucune baseline écrasée.
- D2 couvre l'agrégation 1m/5m, les gaps explicites, la parité de séries,
  l'Opening Drive long/short, le quartile externe, les percentiles exclusifs,
  les pivots 2-left/2-right et les niveaux de session précédente complets ;
- SHA-256 courant du code dans les rapports D2 :
  `fbc38fa53b8bae67e99b5fa4a777aceff1c69f56d82cc5a6b25c7760b12369eb`.
- D3 couvre 18 tests dédiés de stratégie/risque ; SHA-256 du code D3 :
  `c333a4ee6e816904239045fb15a9a0564a4709a9999e7ccdbadd339c27d28ae2`.

## 12. Impact opérationnel

- **Trading :** impossible ; aucun gateway ni dépendance d'exchange.
- **Réseau :** aucun appel effectué ou implémenté dans D0/D1A/D1B/D1C.
- **Données partagées :** lecture seule ; aucun fichier HyperBot/TRIDENT modifié.
- **Stockage BOT05 :** seuls petits manifests/rapports sont versionnables ; raw,
  normalized et derived restent ignorés hors `.gitkeep`. Les trois segments BTC
  normalisés occupent environ 33 Mo sous `data/normalized/` et sont
  reproductibles depuis les sources H1 en lecture seule.

## 13. Première qualification réelle H1 — fermeture T1

### Segment et périmètre

Le segment HyperBot
`2026-08-21-000618.jsonl.gz` a été choisi car il recouvre une portion utile de
l'ouverture US BTC, du 21 août 2026 à 13:31:15.921 UTC jusqu'à
13:39:17.601 UTC. L'audit lit toute la source, mais le store BOT05 conserve
uniquement BTC `trades` et `bbo` dans cette fenêtre.

### Preuves observées

- SHA-256 brut vérifié :
  `2841b3f01cc5ebf232ca82b7318effad4cbcba4d9db5739e3cbf55a5a03fdb2c` ;
- manifest source vérifié, chaîne continue de la séquence `81319971` à
  `81450743`, soit **130 773 records source** ;
- **15 802 records BTC normalisés** : 10 185 trades et 5 617 BBO ;
- zéro rejet, zéro doublon et zéro gap critique ;
- gap BBO maximal : **288 ms**, sous le cap préenregistré de 15 secondes ;
- rapport de qualification SHA-256 :
  `f0fbca2f2a13c854f8c68f93932dbda74c4a3151e703ccb3ac43e156bdb258d9` ;
- deux exécutions produisent le même segment, le même rapport et le même hash.

Le rapport versionné est sous `reports/data_quality/qualifications/`. Les 19 Mo
de records normalisés restent locaux et ignorés par Git sous
`data/normalized/`. Les sources HyperBot n'ont pas été modifiées.

### Réintégration local-first et gaps

`src/bot05/data/inventory.py` redécouvre une qualification BOT05 seulement si
les checksums du rapport, du manifest normalisé, des records, des rejets, du
brut HyperBot et de son manifest sont encore valides. Le planner réutilise alors
l'asset H1 pour BTC trades/BBO ; il ne le reclasse jamais H2.

Le rapport post-qualification recense 64 assets, dont un asset BOT05 qualifié,
sans problème d'inventaire. Son SHA-256 est
`46dcf754640c2eb8ab1217bd56cb568198e66e4f333aefb243a1b69a810ca32e`.
Le nombre de plages remote reste inchangé car les candidats locaux non encore
qualifiés bloquaient déjà ces fetches ; la différence matérielle est que cette
fenêtre BTC apparaît désormais dans `reusable_dataset_ids`.

### Limite causale

Cette tranche ne couvrait pas à elle seule les trois bougies de l'opening drive
13:30–13:45 UTC. Elle validait le pipeline T1 et une portion de données, pas un
signal, un backtest ou une hypothèse de rendement. La section suivante consigne
la qualification des segments adjacents et l'audit D2.

## 14. D2/T2 en cours — opening drive BTC complet

### Qualification des segments adjacents

Les fenêtres bornées des segments HyperBot `000617` et `000619` complètent sans
trou la qualification centrale `000618` :

- `000617` couvre 13:30:00.000–13:31:15.921 UTC, valide **130 809** records
  source et conserve 1 740 trades plus 848 BBO ; gap BBO maximal **241 ms** ;
- `000619` couvre 13:39:17.601–13:45:00.000 UTC, valide **130 872** records
  source et conserve 6 692 trades plus 3 959 BBO ; gap BBO maximal **405 ms** ;
- les deux rapports déclarent zéro rejet, doublon et gap critique ; leurs
  SHA-256 sont respectivement
  `1f1132275a9b9ce1d66adb028fe1b979bf34fd4053648dc67b41fdb0aa580446`
  et `f1364a361098a09ea2640e8bcdefff5fac685bd4bba429e27543676ad3397e7c`.

Sur les trois segments, l'audit porte donc sur **392 454 records source** et
normalise **29 041 records BTC**, dont 18 617 trades et 10 424 BBO. Le rapport
local-first dédié découvre 66 assets, dont trois BOT05 qualifiés, sans problème
d'inventaire ; les quatre besoins BTC trades/BBO/candles 1m/5m sont
`reuse_local`, avec zéro plage de fetch. Son SHA-256 est
`621add2d5712b037a6196802f61697df3496fa6d7a2c1a03b1712ddd4ed44d3b`.

### Features causales livrées

- agrégation trades vers 1m/5m, et rollup 1m vers 5m, ordonnés par temps
  exchange avec tie-break de réception/source ;
- gaps typés pour couverture partielle, minute sans trade et composant absent,
  sans prix synthétique ;
- comparateur de parité OHLCV avec buckets manquants et tolérances explicites ;
- Opening Drive immuable, direction symétrique, quartile externe et midpoint ;
- percentiles q50/q75 type-7 sur les 60 sessions antérieures seulement, avec
  minimum fail-closed de 20 et scope marché/session obligatoire ;
- pivots stricts 2-left/2-right émis seulement après observation des deux
  bougies droites, et previous-session high/low refusés si la série est
  incomplète.

Les tests synthétiques couvrent long, short, invalidation par quartile,
lookahead des percentiles, scope erroné, bougie retardée, gaps, rollup, parité,
pivot non confirmé et session précédente incomplète.

### Audit réel 13:30–13:45 UTC

Le rapport `reports/features/btc_us_open_2026-08-21.json` relit les trois stores
et leurs qualifications checksummées. Les 18 617 trades produisent exactement
15 bougies 1m et trois bougies 5m, sans gap. Les trois 5m directes sont
bit-à-bit identiques au rollup des 1m. Le dernier `closed_at` causal est
13:45:00.280 UTC.

L'Opening Drive observé est long : open 77 116, high 77 364, low 76 256, close
77 330, body 27,7504 bps, range 143,6797 bps, close-location 0,9693 et midpoint
76 810. Ces valeurs vérifient uniquement la mécanique sur une session ; elles
ne constituent ni backtest ni preuve d'edge. Le SHA-256 du rapport est
`4b44bf9e36c110e280fde5de8d1bc796cbf916b28ab9d2f0fad944afb9eabc56`.

### Gate restant

D2 demeure `Bloqué` : aucune candle H0 officielle checksummée n'est présente
sur cette fenêtre, donc la parité externe n'est pas revendiquée. Les filtres
q50/q75 restent aussi indisponibles sur données réelles tant que 20 sessions
comparables antérieures ne sont pas qualifiées. Aucun résultat de rendement et
aucune capacité d'ordre n'ont été ajoutés.

## 15. Audit des gates D2 et livraison D3/T3

### Déficit local D2 prouvé

`scripts/audit_d2_data_gates.py` inspecte uniquement l'inventaire BOT05 et le
manifest HyperBot déjà checksumé, sans lire les gros segments ni contacter le
réseau. Le rapport immuable
`reports/data_quality/d2_local_data_gates_2026-08-21.json` établit :

- zéro asset H0 officiel chevauchant 13:30–13:45 UTC ;
- zéro session antérieure déjà qualifiée ;
- cinq dates H1 au maximum avec fichiers présents et gaps metadata sous 15 s,
  du 16 au 20 août inclus ;
- un déficit minimal de quinze par rapport aux vingt historiques requis, avant
  même de retirer le dimanche 16 ou de valider un calendrier de production.

Ces fenêtres restent `manifest-only` et ne sont pas promues. Le SHA-256 du
rapport est
`8f6e1fd89c955ccd349050971f3093c8172ee674000938b1990ac59cf1138193`.
D2/T2 passent donc à `Bloqué` jusqu'à disponibilité d'une source H0 commune et
d'un historique suffisant ; aucun fetch n'est inféré de ce constat.

### Machine d'état Strategy v0

`src/bot05/strategy/` fournit des contrats gelés et un réducteur couvrant
`waiting_open → drive_complete → waiting_pullback → waiting_confirmation →
intent|expired|invalidated` :

- le filtre de drive, la confirmation et la target sont verrouillés dans un
  `StrategySpec` checksumé ;
- les confirmations breakout, engulf et midpoint reclaim sont distinctes et
  symétriques long/short ;
- touch et confirmation peuvent partager la même candle clôturée ;
- franchissement de l'origine, gap, révision, changement d'intervalle,
  lookahead, prix stale ou entrée au-delà du stop invalident fail-closed ;
- les targets fixes 1R/2R sont structurelles ; la target de liquidité est le
  niveau antérieur intact le plus proche, connu strictement avant `t0` ;
- le premier prix exécutable causal produit un seul `TradeIntent`. Ses retries
  sont idempotents et aucun état terminal ne peut réentrer ;
- l'intent porte les hashes de données/configuration, les versions calendrier
  et code, les timestamps, prix, stop, target et motif de sélection.

### Superviseur de risque pur

`src/bot05/risk/` refuse dans un ordre stable : scope/ledger incohérent, intent
dupliqué, gap ou stale, horloge/session ambiguë, marché ou définition invalide,
oracle interne requis, spread/slippage/divergence hors cap, position existante,
limites journalières/cooldown, ordre orphelin, fill inconnu, divergence de
position, perte de flux, risque de taille, levier et reward/risk net insuffisant.

Le risque de position inclut les coûts de perte attendus. Les limites sont
obligatoires et content-addressed. `apply_risk_decision` et
`close_risk_position` mettent à jour un ledger immuable, avec traitement
exact-once des intents et perte journalière en R. Aucun gateway ou module
d'exécution n'a été introduit.

### Tests de sortie

Les 18 tests D3/T3 couvrent long/short, trois confirmations, trois targets,
trois filtres, touch/confirmation simultanés, expiry, gap, révision, niveaux
futurs, absence de target, lookahead/stale/gap d'entrée, exact-once, tous les
codes de refus risque, ledger, cooldown et perte quotidienne. D3/T3 sont
`Terminé`; aucun résultat de rendement n'est calculé.

## 16. Lot livré — D4 / T4

### Contrats et coûts explicites

`src/bot05/replay/` ajoute une simulation de recherche pure, sans créer le
package `bot05.execution` interdit par le garde-fou de sécurité. Chaque run
consomme un `TradeIntent` et une `RiskDecision` acceptée et correspondante. Sa
configuration immuable fixe quantité, tick, pas de taille, latences, staleness,
horizon, slippage, multiplicateur de frais et version du code ; elle est
content-addressed.

Les frais sont des snapshots effectifs datés par marché. Ils enregistrent tier,
maker/taker de base, growth mode, `deployerFeeScale`, remises, builder fee,
taux effectifs et checksum source. Le moteur ne devine pas une formule HIP-3 à
partir d'informations partielles : les taux effectifs doivent provenir d'une
source qualifiée. Un changement de snapshot entre entrée et sortie est appliqué
à chaque fill. Le funding est traversé uniquement aux frontières strictement
postérieures à l'entrée et antérieures ou égales à la sortie.

### Quatre modèles de replay

- `ohlc_conservative` entre au next-open vérifié, arrondit contre la position,
  paie taker à l'entrée et à la sortie, applique le slippage fixe, traite les
  gaps au prix d'ouverture défavorable et donne priorité au stop quand stop et
  target apparaissent dans la même bougie ;
- `ohlc_optimistic` conserve une collision favorable et zéro slippage comme
  plafond informatif seulement ;
- `trade_bbo_central` attend la latence après la décision risque, balaie tous
  les niveaux nécessaires pour la taille, exécute les stops en taker et ne
  crédite un target maker qu'après ACK, repos et trade-through strict dans le
  bon sens d'agression ; un simple touch ne remplit pas ;
- `trade_bbo_stress` verrouille les frais à 1,5×, les latences à 2× et ajoute le
  slippage p95 défavorable aux exécutions marketables. L'impact et les gaps
  restent ceux du carnet réellement fourni.

Une profondeur insuffisante n'est jamais extrapolée. Une donnée stale, un trou
de flux pendant la position, une séquence ambiguë, l'absence de frais effectifs
ou de liquidité de sortie retourne un résultat fail-closed sans fabriquer de
PnL.

### Résultats et rapports

Les résultats immuables séparent fills, rôles maker/taker, niveaux consommés,
latence, slippage, frais, funding, PnL brut/net et PnL en R. Le `run_id` couvre
intent, limites risque, configuration, schedule de frais et hash des événements
de replay. Les rapports JSON canoniques et Markdown calculent taux de réussite,
frais, funding, PnL et drawdown, écrivent un sidecar SHA-256 et refusent
d'écraser une publication différente.

### Tests de sortie et limites

Les 15 nouveaux scénarios couvrent long/short, collision stop/TP, gap au stop ou
à l'entrée, trou de bougie, stale transport, arrondis tick/taille, impact
multi-niveaux, profondeur insuffisante, funding à la frontière, changement de
frais, target touché mais non rempli, trade-through post-ACK, stop retardé,
latence/slippage/frais stress, perte de flux et répétition bit-identique. La
suite complète comptait 107 tests au jalon D4 ; pytest, ruff et mypy strict
passaient.

D4/T4 sont `Terminé` sur fixtures synthétiques. Aucun replay historique par
marché, résultat de rendement ou preuve d'exécution live n'est revendiqué : ces
travaux relèvent de D5 et restent contraints par les gates de données D2.

## 17. Code livré — D5 / T5, preuves externes bloquées

### Préenregistrement et séparation des preuves

`src/bot05/studies/` ajoute un `ExperimentSpec` immuable et checksumé qui fixe
avant les résultats l'univers, les sessions, StrategySpecs, quatre modèles de
replay, horizons MFE/MAE 15/30/60/120, calendriers, configuration et version du
code. Le sélecteur portefeuille reste refusé pendant D5.

Chaque `StudyDataset` manifeste explicitement marché canonique, instrument
source, étage U/H0/H1/H2/L, qualification, canaux, période, nombre de records,
gaps critiques, transformations et trois checksums. Un rapport économique est
mono-marché, mono-session, mono-modèle et mono-étage : mélanger H1 et H2, un
dataset candidat ou un dataset avec gap critique est refusé.

La politique codée autorise U pour la structure alpha, H0 pour le smoke et la
parité de signal, H1 pour le replay historique, H2 pour la preuve causale et L
pour la pré-recherche seulement. H0, L et U ne peuvent pas publier de PnL
d'exécution. Aucun rapport D5 ne permet une promotion ; GOLD, SILVER, SP500,
HYPE et BTC sont primaires, ETH/SOL restent marqués comme contrôles.

### Études par marché

- entonnoir monotone session complète → éligible → drive → pullback →
  confirmation → gate économique → trade, avec motif de rejet obligatoire ;
- MFE/MAE directionnels en bps et R aux quatre horizons préenregistrés, avec
  fenêtre incomplète explicite dès qu'une bougie manque ;
- win rate, expectancy nette R/bps, profit factor, PnL brut/net, frais connus,
  funding, slippage configuré, spread et impact BBO observés, drawdowns R/USD et
  durée, durée de position, moyenne, médiane, dispersion, percentile 5, CVaR 5,
  sorties stop/target/time et concentration des cinq meilleurs résultats ;
- rapport JSON canonique, Markdown français et sidecar SHA-256 immuables ;
- parité U/Hyperliquid OHLC par timestamp, avec écarts bps par champ et buckets
  manquants. Ce rapport de parité ne fusionne aucun PnL.

Le contrat `ReplayResult` D4 porte désormais explicitement marché, session et
direction en plus de l'intent, afin d'interdire les agrégations de portée
ambiguë. Les fills BBO conservent aussi benchmark top-of-book, spread et impact
de profondeur séparément du slippage configuré. Aucun rapport D4 publié
n'existait à migrer.

### Intégration et gate restant

Une intégration synthétique complète transforme des trades checksummés en
bougies 5m, construit l'Opening Drive et le signal, obtient une décision risque,
rejoue l'exécution avec frais, calcule MFE/MAE puis publie deux rapports
bit-identiques. Les tests couvrent aussi parité complète/incomplète, H0/L sans
preuve d'exécution, mélange d'étages, gaps critiques, contrôles de
préenregistrement et métriques de drawdown.

La suite compte 116 tests et pytest, ruff, format et mypy strict passent. D5/T5
restent néanmoins `Bloqué` : aucun historique U/H0/H1/H2 qualifié suffisant ne
permet encore les replays séparés GOLD, SILVER, SP500, HYPE, BTC et les contrôles
ETH/SOL. Les fixtures valident le logiciel, pas l'edge ni la rentabilité.
