# BOT05 — plan d'implémentation et de validation

Statut : fondation initiale, à transformer en lots suivis lors du démarrage du
développement.

Date de référence : 25 août 2026.

## 1. Décision et objectif

BOT05 est un projet autonome de recherche et, seulement si les preuves le
justifient, d'exécution d'une stratégie directionnelle de session inspirée du
pattern présenté comme « stratégie 0,5 » : impulsion à l'ouverture, retour à
50 % de l'impulsion, confirmation, puis continuation vers une cible causale.

L'objectif n'est pas de reproduire les tracés discrétionnaires de la vidéo. Il
est de répondre à une question falsifiable :

> Une définition entièrement mécanique du premier pullback après une impulsion
> de session produit-elle, sur certains marchés Hyperliquid, une expectancy
> nette positive et stable après frais, spread, slippage, latence et sélection
> ex ante du marché ?

Le projet vise peu de décisions : au plus une entrée par session et, dans la
configuration portefeuille initiale, au plus deux sessions par jour. La
surveillance d'une position ouverte et les contrôles de risque restent actifs
jusqu'à la clôture ; « peu de temps de décision » ne signifie pas « absence de
supervision ».

BOT05 ne dépendra pas à l'exécution de `/workspaces/trident` ou de
`/workspaces/hyperbot`. Les archives TRIDENT pourront être lues comme données
legacy de recherche, avec provenance et limites explicites, sans jamais être
modifiées.

## 2. Principes non négociables

1. Le trading live est absent des premiers lots et désactivé par défaut dans
   toute configuration future : `live_enabled = false`, `shadow_only = true`.
2. Aucun signal ne peut utiliser une bougie non clôturée, un pivot non encore
   confirmé, une statistique calculée avec le futur ou une sélection de marché
   faite après observation du résultat.
3. Les règles, variantes et splits sont enregistrés avant l'ouverture du holdout.
4. Les résultats sont toujours nets des coûts propres au marché et au compte.
5. Les données brutes sont append-only ; chaque dérivé est reproductible et
   porte les versions du code, de la configuration et de ses sources.
6. Un trou de données, une donnée stale, une timezone inconnue, un marché hors
   session externe, un ordre orphelin ou une divergence de position provoque un
   blocage fail-closed.
7. Aucun martingale, rattrapage de perte, augmentation automatique du levier ou
   sélection opportuniste de la meilleure variante n'est autorisé.
8. Un backtest positif n'autorise jamais le live. La séquence est : replay
   exploratoire, OOS verrouillé, shadow, canary explicitement autorisé, puis
   décision séparée.

## 3. Périmètre de marché initial

### 3.1 Univers principal

| Marché | Rôle initial | Session testée | Motif |
|---|---|---|---|
| `xyz:SILVER` | candidat principal | Europe et US | commodity volatile, frais HIP-3 growth faibles, premier filtre encourageant mais non validant |
| `HYPE` | candidat principal crypto | US | volatilité et premier signal exploratoire ; absence d'ouverture native à traiter explicitement |
| `xyz:SP500` | candidat principal | US cash | justification structurelle forte de l'opening drive et frais growth faibles |
| `xyz:GOLD` | marché de thèse et de falsification | Europe et US | actif de la vidéo, liquidité suffisante, mais frais HIP-3 ordinaires élevés |
| `BTC` | contrôle liquide | Europe et US | excellente exécution, mais pas d'ouverture native et premier filtre défavorable |

### 3.2 Univers secondaire

`ETH` et `SOL` servent d'abord de contrôles hors optimisation. Ils ne rejoignent
le portefeuille que si une règle verrouillée, développée sans leurs résultats
OOS, passe les mêmes gates que les marchés principaux.

Il est interdit de scanner des dizaines d'actifs puis de ne conserver que le
meilleur trade a posteriori. Tout élargissement de l'univers constitue une
nouvelle expérience versionnée, avec correction du risque de tests multiples.

### 3.3 Particularités HIP-3

Pour chaque marché HIP-3, un snapshot causal doit enregistrer au minimum : DEX,
asset ID, statut, `szDecimals`, tick, levier maximal, mode de marge, growth mode,
`deployerFeeScale`, oracle, external price si disponible, mark, open interest,
volume, funding et état du carnet.

Les sessions internes de l'oracle XYZ, les week-ends, les jours fériés et les
fenêtres de maintenance sont exclus de la baseline. Ils pourront faire l'objet
d'une expérience séparée, jamais mélangée à la baseline de session externe.

## 4. Spécification causale de la stratégie v0

### 4.1 Sessions

Deux sessions indépendantes sont définies avec des timezones IANA, jamais avec
un offset UTC codé en dur :

- `europe_open` : 09:00 `Europe/Paris`, conformément à la règle littérale de la
  vidéo ;
- `us_cash_open` : 09:30 `America/New_York`, correspondant à l'ouverture cash
  américaine réelle.

La variante « 15:00 Europe/Paris » évoquée dans la vidéo est autorisée comme
ablation distincte, sous le nom `video_us_1500`, mais ne doit pas être confondue
avec `us_cash_open`.

Le calendrier doit connaître DST, week-ends, jours fériés et sessions externes
propres à l'instrument. Une session n'est éligible que si les trois bougies
d'ouverture et tout l'horizon maximal attendu sont couverts sans gap critique.

### 4.2 Opening drive

La fenêtre d'ouverture est `[t0, t0 + 15 minutes)` et contient exactement trois
bougies 5 minutes clôturées : `b0`, `b1`, `b2`.

Pour chaque session :

```text
drive_open       = b0.open
drive_close      = b2.close
drive_high       = max(b0.high, b1.high, b2.high)
drive_low        = min(b0.low, b1.low, b2.low)
drive_body_bps   = 10_000 * (drive_close - drive_open) / drive_open
drive_range_bps  = 10_000 * (drive_high - drive_low) / drive_open
close_location   = (drive_close - drive_low) / (drive_high - drive_low)
```

La direction est longue si `drive_body_bps > 0`, courte s'il est négatif. Une
valeur nulle ou un range nul invalide la session.

Le close doit se situer dans le quart externe du range :

- long : `close_location >= 0.75` ;
- short : `close_location <= 0.25`.

La « grosse bougie » est testée avec une famille limitée et préenregistrée :

- `drive_none` : aucun seuil, contrôle seulement ;
- `drive_q50` : amplitude absolue au moins égale à la médiane des 60 dernières
  sessions comparables du même marché et de la même session ;
- `drive_q75` : amplitude absolue au moins égale au 75e percentile causal de
  cette même distribution.

Il faut au moins 20 sessions antérieures pour calculer le filtre. Aucune valeur
de la session courante n'entre dans son historique de référence.

### 4.3 Niveau 0,5 et invalidation avant entrée

Le niveau 0,5 ne dépend d'aucun swing choisi visuellement :

```text
midpoint = (drive_high + drive_low) / 2
```

La recherche du pullback commence après la clôture de `b2` et expire 60 minutes
après `t0`. Il y a touch :

- long si le low d'une bougie clôturée est inférieur ou égal au midpoint ;
- short si son high est supérieur ou égal au midpoint.

Si le prix franchit l'origine de l'impulsion avant confirmation — `drive_low`
pour un long, `drive_high` pour un short — le setup est invalidé. Aucun retracé
de Fibonacci n'est recalculé pendant la session et aucune seconde impulsion
n'est choisie a posteriori.

### 4.4 Confirmations autorisées

Une seule confirmation est primaire dans un run. Les autres sont des ablations
nommées ; le moteur n'a pas le droit de prendre celle qui déclenche le meilleur
résultat pour chaque trade.

- `breakout_confirm` — baseline recommandée et plus proche du trade live de la
  vidéo : après touch du midpoint, clôture haussière au-dessus du high de la
  bougie précédente pour un long, ou clôture baissière sous son low pour un
  short ;
- `engulf_confirm` — corps directionnel englobant entièrement le corps précédent ;
- `midpoint_reclaim` — clôture directionnelle revenue du bon côté du midpoint.

Le touch et la confirmation peuvent se produire dans la même bougie clôturée,
car l'entrée ne se fait qu'ensuite. Cette situation doit être identifiée dans le
rapport pour vérifier qu'elle ne concentre pas l'edge.

### 4.5 Entrée

La baseline entre au premier prix exécutable après la clôture de confirmation :

- screening OHLC : open de la bougie 1 minute suivante, sinon open 5 minutes
  suivante avec label `coarse_execution` ;
- replay trades/BBO : ordre IOC marketable après la latence configurée, rempli
  contre le carnet ou les trades disponibles ;
- shadow : prix réellement exécutable observé après l'horodatage de décision.

Le trade est refusé si l'entrée gappe au-delà du stop, si le marché devient
stale, si le spread dépasse le cap ou si le target causal n'offre plus le ratio
net minimal.

### 4.6 Stop

Le stop structurel est fixé une seule fois :

- long : `drive_low` ;
- short : `drive_high`.

La baseline n'ajoute pas de buffer. Les stress tests ajoutent un tick puis un
spread complet. Le risque est calculé sur le prix d'entrée réellement simulé,
pas sur le midpoint théorique.

Le stop n'est jamais éloigné après l'entrée. Le passage à break-even est absent
de v0 afin de ne pas multiplier les degrés de liberté.

### 4.7 Targets et sortie temporelle

Trois politiques sont autorisées :

- `fixed_1r` : contrôle simple ;
- `fixed_2r` : contrôle proche du trade live montré dans la vidéo ;
- `causal_liquidity` : cible principale correspondant à la narration.

Pour `causal_liquidity`, les niveaux candidats sont calculés uniquement avant
la session : previous-day high/low et pivots fractals confirmés `2-left/2-right`
sur les cinq dernières sessions. Un pivot n'est confirmé que si ses deux
bougies de droite sont déjà clôturées avant `t0`. À l'instant de l'entrée, les
niveaux déjà traversés depuis `t0` sont retirés causalement. La cible est alors
le niveau intact le plus proche, strictement au-dessus de l'entrée pour un long
ou strictement en dessous pour un short.

Un trade `causal_liquidity` n'est autorisé que si :

```text
net_reward = target_distance_bps - expected_win_cost_bps
net_loss   = stop_distance_bps + expected_loss_cost_bps
net_reward / net_loss >= 1.5
```

Si aucune cible valide n'existe, il n'y a pas de trade. Le TP ne peut pas être
déplacé pendant le replay.

La position expire 120 minutes après l'entrée ou avant une coupure de session
externe, selon le premier événement. Il n'y a ni overnight ni carry volontaire
dans v0.

### 4.8 Fréquence et sélection portefeuille

La baseline autorise :

- au plus une entrée par marché et par session ;
- aucune réentrée après stop ou target ;
- au plus une position ouverte dans le portefeuille ;
- au plus deux trades par jour.

Les backtests par marché sont exécutés avant le sélecteur portefeuille. Une fois
les marchés individuellement qualifiés, le sélecteur choisit ex ante le setup
au meilleur `net_reward / net_loss`, puis le percentile d'impulsion le plus
élevé, puis un ordre de marché statique enregistré dans la configuration. Il ne
peut utiliser ni excursion future ni résultat journalier.

## 5. Coûts et modèles d'exécution

### 5.1 Frais

Les frais ne sont jamais une constante globale. Chaque run enregistre :

- tier de compte ou tier 0 explicitement assumé ;
- maker/taker de base ;
- growth mode ;
- `deployerFeeScale` HIP-3 ;
- remises staking/referral si réellement applicables ;
- builder fee éventuel, qui vaut zéro dans la baseline ;
- funding réellement traversé par la position.

Le screening initial utilise le tier 0 sans remise. Tant que l'exécution maker
n'est pas démontrée, l'entrée, le stop et le TP paient tous le coût taker.

### 5.2 Modèles

Chaque expérience publiable produit au minimum :

1. `ohlc_conservative` : entrée suivante, coûts taker, slippage fixe par marché,
   stop gagnant si stop et TP apparaissent dans la même bougie ;
2. `trade_bbo_central` : BBO/trades, latence mesurée, impact pour la taille,
   stop taker et TP maker seulement si l'ordre a reposé et qu'un trade-through
   après ACK est observé ;
3. `trade_bbo_stress` : frais multipliés par 1,5, slippage au percentile 95,
   latence multipliée par deux et gap défavorable ;
4. `ohlc_optimistic` : plafond informatif seulement, jamais utilisé pour une
   promotion.

La stratégie étant directionnelle et majoritairement taker, la reconstruction
de file n'est pas son problème principal. En revanche, l'ordre intrabar,
l'impact, les gaps et la latence de déclenchement du stop sont obligatoires.

### 5.3 Gates économiques avant signal

Le moteur rejette un setup si l'un des critères suivants est vrai :

- `target_distance_bps <= 3 * expected_roundtrip_cost_bps` ;
- ratio reward/risk net inférieur à 1,5 ;
- spread supérieur à 10 % de la distance du stop ou au cap absolu du marché ;
- taille souhaitée supérieure à 1 % de la profondeur observée dans la bande de
  slippage autorisée ;
- mark/oracle ou external/oracle divergent au-delà du seuil versionné ;
- funding prévu défavorable et non négligeable par rapport au reward net.

Ces gates sont calculées avant l'entrée et leurs refus sont journalisés.

## 6. Données

### 6.1 Étages de preuve

| Niveau | Source | Usage autorisé | Limite |
|---|---|---|---|
| U | historique long du sous-jacent : XAU/USD ou GC, XAG/USD ou SI, SPX/ES, crypto spot/perp | recherche de l'edge de session et robustesse multi-régime | ne prouve pas l'exécution du perp Hyperliquid |
| H0 | API officielle `candleSnapshot`, environ 5 000 bougies récentes | smoke, parité de signal, premières observations | historique 5 min trop court pour conclure |
| H1 | archives publiques Hyperliquid trades/BBO/L2 reconstruites | replay d'exécution historique selon couverture | qualité et disponibilité à qualifier par marché HIP-3 |
| H2 | collector BOT05 append-only | preuve causale Hyperliquid, slippage, shadow | nécessite d'accumuler suffisamment de sessions |
| L | snapshots TRIDENT en lecture seule | pré-recherche et comparaison | legacy, snapshots ponctuels, jamais suffisants seuls pour promotion |

Pour les sous-jacents TradFi, les contrats à terme, rolls, sessions et jours
fériés doivent être normalisés. GOLD/SILVER XYZ référençant des spots, un
historique futures ne doit pas être présenté comme identique : il sert à tester
la structure temporelle, puis la parité est mesurée sur les fenêtres communes.

### 6.2 Collecte BOT05

Le collector public enregistre, sans capacité de signature :

- chandeliers WebSocket 1m et 5m ;
- trades ;
- BBO et L2 ;
- mark, oracle, external price disponible, funding, OI et volume ;
- définitions de marché et changements de frais/growth mode ;
- calendrier de session attendu et état external/internal inféré ou fourni ;
- métriques de connexion, gaps, reconnexions, drops et retard d'horloge.

L'univers de collecte initial reste limité aux sept marchés nommés dans les
sections 3.1 et 3.2. Il est permis de réduire les canaux hors des fenêtres de
session, mais les 30 minutes avant `t0`, l'ouverture, les deux heures suivant un
signal et toute position ouverte doivent avoir la couverture complète.

### 6.3 Provenance et stockage

Chaque dataset porte :

- `dataset_id`, source et chemin/URL ;
- SHA-256 du brut et du manifest ;
- période exacte et timezone source ;
- nombre de records, doublons, trous, corrections et valeurs rejetées ;
- schéma et version d'adaptateur ;
- version de calendrier ;
- versions code/config ;
- transformations et checksum de la sortie.

Disposition recommandée :

```text
data/
├── raw/              # append-only, ignoré par Git
├── normalized/       # événements versionnés, ignoré par Git
├── derived/          # candles/features reproductibles
├── manifests/        # petits manifestes versionnables
└── reports/          # JSON de run ; synthèses publiées copiées sous reports/
```

## 7. Protocole de backtest

### 7.1 Préenregistrement

Avant tout holdout, créer une `ExperimentSpec` immuable contenant : univers,
sessions, calendrier, direction, drive filter, confirmation, target, horizon,
coûts, exécution, sélecteur, taille, splits, métriques et gates. Son hash est
inclus dans chaque résultat.

La matrice initiale est volontairement petite :

```text
3 drive filters × 3 confirmations × 3 targets
```

Les 27 combinaisons ne sont pas 27 stratégies indépendantes à trier librement.
Elles forment des familles d'ablation. Une seule spécification est verrouillée
après development/validation et évaluée une fois sur le holdout final.

### 7.2 Splits

Lorsque l'historique le permet :

- 50 % chronologiques : development ;
- 20 % suivants : validation et choix définitif ;
- 30 % finaux : OOS verrouillé, consulté une seule fois.

En complément, produire un walk-forward ancré d'au moins trois folds. Les
sessions d'une même journée restent dans le même split. Une purge d'un jour est
appliquée autour des frontières pour les features utilisant des fenêtres
glissantes.

Le résultat OOS du sous-jacent et le résultat Hyperliquid sont séparés. Une
bonne alpha U suivie d'une mauvaise exécution H1/H2 échoue ; leurs PnL ne sont
pas fusionnés pour masquer le problème.

### 7.3 Ordre des études

1. **Parité des données** : timestamps, OHLC, sessions, DST, gaps et comparaison
   sous-jacent/Hyperliquid sur fenêtres communes.
2. **Fréquence du setup** : nombre de drives, pullbacks, confirmations, rejets
   économiques et trades par marché/session.
3. **Alpha brute** : MFE/MAE après confirmation sans target optimisé, horizons
   15/30/60/120 minutes.
4. **Targets fixes** : 1R et 2R, sans sélecteur portefeuille.
5. **Target causal** : previous-day/pivots, avec audit de disponibilité causale.
6. **Coûts** : fees/slippage/latence/funding, central puis stress.
7. **Ablations** : 0,382, 0,5, 0,618, zone 30–70 %, sans confirmation, opening
   drive sans pullback et heures décalées/aléatoires.
8. **Stabilité** : marché, session, long/short, mois, volatilité, tendance/range,
   jours macro et distances de stop.
9. **Sélecteur portefeuille** : seulement avec les candidats individuellement
   qualifiés.
10. **OOS verrouillé**, puis gel de la stratégie.

### 7.4 Contrôles contre les faux positifs

- Comparer le midpoint exact aux niveaux 0,382 et 0,618. Si la zone entière
  fonctionne pareil, documenter l'edge comme « opening pullback », pas comme
  « Fibonacci 0,5 ».
- Décaler l'heure d'ouverture de ±30/60/120 minutes et utiliser des pseudo-opens
  aléatoires stratifiés par heure. Le vrai open doit battre ces contrôles.
- Comparer confirmation, entrée immédiate après touch et absence de touch.
- Rapporter tous les membres de la famille, pas seulement le meilleur.
- Utiliser un bootstrap par blocs de journées pour préserver l'autocorrélation.
- Mesurer la concentration : marché, mois, cinq meilleurs trades, sens et régime.
- Rejouer le même run deux fois et exiger une sortie bit-à-bit identique.

### 7.5 Métriques obligatoires

Chaque rapport contient au minimum :

- sessions observées, éligibles, incomplètes et rejetées ;
- entonnoir drive → pullback → confirmation → gate économique → trade ;
- trades, win rate, expectancy nette en R et bps, profit factor ;
- PnL brut, frais, spread, slippage, funding et PnL net ;
- drawdown maximal en R et USD, durée de drawdown ;
- MAE/MFE, temps en position, stop/TP/time exits ;
- moyenne, médiane, dispersion, percentile 5 et CVaR ;
- intervalle de confiance bootstrap 95 % de l'expectancy ;
- résultats par marché/session/sens/mois/régime ;
- concentration des cinq meilleurs trades et meilleure période de 30 jours ;
- sensibilité aux coûts, latence et ordre intrabar ;
- hash des données, du code, de la configuration et de l'ExperimentSpec.

Sharpe et Sortino peuvent être affichés, mais ne remplacent ni l'expectancy, ni
le profit factor, ni le drawdown, ni l'intervalle de confiance.

## 8. Gates de promotion

### 8.1 Research → candidat OOS

- logique et causalité couvertes par les tests ;
- parité des données acceptable et gaps critiques nuls sur les trades ;
- au moins 100 trades OOS agrégés et 30 sur tout marché revendiqué comme edge
  autonome ;
- profit factor net central au moins 1,20 ;
- expectancy nette positive dans au moins trois folds et sur le holdout ;
- borne basse bootstrap 95 % de l'expectancy strictement positive ;
- aucune contribution de marché ou de sens supérieure à 40 % du PnL total ;
- les cinq meilleurs trades ne représentent pas plus de 35 % du PnL ;
- stress de coûts +50 % non négatif ;
- drawdown maximal inférieur à 10R ;
- le vrai open bat les pseudo-opens et le setup bat l'opening drive naïf.

Si le nombre de trades est insuffisant, la conclusion est « données
insuffisantes », jamais « edge validé avec peu de trades ».

### 8.2 Candidat OOS → shadow

- spécification gelée et versionnée ;
- même contrat Strategy/Risk en replay et shadow ;
- collector et horloge qualifiés ;
- zéro capacité d'envoi d'ordre dans le gateway shadow ;
- réconciliation théorique déterministe ;
- dashboards de signaux, refus, coûts et stale data ;
- procédure d'arrêt et reprise testée.

### 8.3 Shadow → proposition de canary

- au moins 60 sessions shadow et 30 signaux exécutables, sans modifier les
  règles ;
- aucune divergence de position théorique ou anomalie non expliquée ;
- prix shadow comparés au BBO réellement exécutable ;
- capture réelle située dans l'enveloppe du replay central/stress ;
- expectancy shadow nette positive, sans concentration nouvelle ;
- review humaine signée et autorisation explicite séparée.

Le canary et le live ne sont pas planifiés comme livraison automatique. Leur
implémentation nécessite une décision ultérieure et commence avec une limite de
risque très faible.

## 9. Architecture cible

Python 3.11 minimum, environnement `uv`, code et identifiants techniques en
anglais, documentation et rapports en français.

```text
bot05/
├── pyproject.toml
├── README.md
├── PLAN.md
├── configs/
│   ├── research.toml
│   ├── shadow.toml
│   └── live.example.toml       # désactivé, valeurs factices, créé bien plus tard
├── src/bot05/
│   ├── config.py
│   ├── models.py               # modèles immuables et versionnés
│   ├── clock.py
│   ├── calendars/
│   │   ├── sessions.py
│   │   └── holidays.py
│   ├── data/
│   │   ├── contracts.py
│   │   ├── hyperliquid.py
│   │   ├── legacy.py
│   │   ├── normalizer.py
│   │   └── store.py
│   ├── features/
│   │   ├── candles.py
│   │   ├── opening_drive.py
│   │   └── pivots.py
│   ├── strategy/
│   │   ├── contract.py
│   │   └── bot05.py
│   ├── risk/
│   │   └── supervisor.py
│   ├── execution/
│   │   ├── gateway.py
│   │   ├── replay.py
│   │   └── shadow.py
│   ├── replay/
│   │   ├── engine.py
│   │   ├── fill_models.py
│   │   ├── experiment.py
│   │   └── metrics.py
│   ├── portfolio/
│   │   └── selector.py
│   └── reporting/
│       └── report.py
├── scripts/
│   ├── collect_public_data.py
│   ├── import_legacy_data.py
│   ├── build_dataset.py
│   ├── run_replay.py
│   └── run_shadow.py
├── tests/
│   ├── fixtures/
│   ├── unit/
│   └── integration/
├── data/                       # ignoré sauf manifests/fixtures explicites
└── reports/                    # synthèses petites et versionnables
```

### 9.1 Contrats principaux

Modèles immuables recommandés :

- `MarketDefinition` ;
- `Candle` et `Trade` ;
- `BookSnapshot` et `MarketContext` ;
- `SessionDefinition` et `SessionState` ;
- `OpeningDrive` ;
- `PullbackTouch` et `Confirmation` ;
- `TradeIntent` ;
- `RiskDecision` ;
- `OrderLifecycle` et `Fill` ;
- `PositionState` ;
- `ExperimentSpec`, `ReplayRun` et `ReplayResult`.

Une `TradeIntent` contient tous les faits connus au moment de la décision :
timestamps source/décision, session, drive, midpoint, confirmation, entry
reference, stop, target, distances, coûts attendus, raison de sélection et hashes.

### 9.2 Contrat Strategy/Risk/Execution

Le même `Strategy` et le même `RiskSupervisor` tournent en replay et shadow.
Seuls l'horloge et l'`ExecutionGateway` changent. Le gateway shadow ne possède
aucune méthode d'envoi d'ordre. Aucun client de signature n'est introduit avant
une autorisation canary.

## 10. Risque opérationnel

Le `RiskSupervisor` bloque au minimum :

- donnée stale ou gap dans l'opening drive ;
- horloge désynchronisée ou session ambiguë ;
- marché halted/delisted ou définition modifiée sans validation ;
- passage en oracle interne pour une baseline externe ;
- spread, slippage estimé ou divergence oracle/mark hors cap ;
- reward/risk net insuffisant ;
- taille ou leverage hors limites ;
- limite quotidienne, position déjà ouverte ou cooldown ;
- ordre orphelin, fill inconnu ou divergence de position ;
- perte de flux pendant une position.

Paramètres prudents à utiliser en shadow et proposés pour un éventuel canary,
sans les activer maintenant : risque maximal de 0,25 % du capital par trade,
perte quotidienne maximale de 1R, deux trades par jour et aucun accroissement
automatique de la taille. Ces valeurs devront être revues après observation de
la distribution OOS.

## 11. Plan d'exécution par lots

### M0 — bootstrap et sécurité

Livrables :

- package Python, `uv`, pytest, ruff, mypy ;
- README, licence à décider, `.gitignore`, configuration factice ;
- modèles de configuration stricts ;
- guards `live_enabled=false` et `shadow_only=true` ;
- test prouvant l'absence de gateway live et de lecture de secrets.

Acceptation : tests/lint/types passent ; aucune dépendance runtime TRIDENT ou
HyperBot ; aucune capacité d'envoi d'ordre.

### M1 — contrats de données, calendriers et provenance

Livrables :

- modèles immuables et sérialisation versionnée ;
- calendriers timezone-aware Europe/US et sessions XYZ externes ;
- event store append-only avec manifestes/checksums ;
- adaptateur Hyperliquid candle/meta ;
- inventaire puis adaptateur legacy TRIDENT read-only ;
- rapport de qualité des datasets.

Acceptation : DST, holidays, doublons, gaps, timestamps et checksum testés ; un
dataset dérivé est reproductible bit-à-bit.

### M2 — constructeur de bougies et features causales

Livrables :

- agrégation trades/points vers 1m et 5m ;
- détection des gaps et bougies incomplètes ;
- `OpeningDrive`, percentiles roulants exclusifs du présent ;
- pivots confirmés et previous-day levels ;
- fixtures synthétiques illustrant long, short et invalidations.

Acceptation : aucun lookahead dans les tests ; parité vérifiée contre des
bougies officielles sur une fenêtre commune.

### M3 — stratégie et superviseur de risque

Livrables :

- machine d'état `waiting_open → drive_complete → waiting_pullback →
  waiting_confirmation → intent|expired|invalidated` ;
- trois confirmations, trois targets et trois drive filters configurables ;
- gates économiques et journal de refus ;
- sélecteur désactivé par défaut ;
- limites quotidiennes et fail-closed.

Acceptation : tests exhaustifs de transitions, exact-once intent, aucune
réentrée, causalité et invariants de prix.

### M4 — moteur de replay et coûts

Livrables :

- replay déterministe événementiel ;
- modèles OHLC conservateur/optimiste et trades/BBO central/stress ;
- ordre intrabar pessimiste ;
- fees HIP-3/core, slippage, funding et latence ;
- métriques et rapport JSON + Markdown.

Acceptation : fixtures couvrant same-bar stop/TP, gap, stale, impact, changement
de frais, target maker non rempli et stop retardé ; répétition bit-identique.

### M5 — datasets longs et étude par marché

Livrables :

- manifests des sources U, H0, H1/H2 et L ;
- rapports de parité sous-jacent/Hyperliquid ;
- replays séparés GOLD, SILVER, SP500, HYPE, BTC ;
- ETH/SOL utilisés comme contrôles ;
- entonnoirs de fréquence et analyse MFE/MAE.

Acceptation : couverture et limites publiées ; aucune conclusion de rentabilité
tirée de H0 ou L seuls.

### M6 — ablations, walk-forward et OOS verrouillé

Livrables :

- matrice préenregistrée ;
- ablations niveau/confirmation/session ;
- walk-forward et bootstrap par journées ;
- choix d'une spécification unique ou rejet de la thèse ;
- run OOS unique et rapport de décision.

Acceptation : toutes les variantes publiées ; gates de la section 8.1 évaluées
sans réoptimisation du holdout.

### M7 — shadow

Livrables :

- collector public continu ;
- runner shadow sans signature ;
- replay quotidien des décisions shadow ;
- dashboard signaux/refus/stale/coûts ;
- procédures incident, arrêt et reprise.

Acceptation : critères de la section 8.2 puis observation jusqu'aux critères 8.3.

### M8 — canary éventuel, explicitement bloqué

Non autorisé par ce plan initial. Une proposition future devra définir gateway,
clés, isolation, reconciliation, bracket orders, kill switch, montant, limites,
monitoring et rollback, puis recevoir une autorisation explicite avant code ou
déploiement live.

## 12. Matrice de tests minimale

### Causalité et stratégie

- bougie d'ouverture incomplète ;
- direction nulle, range nul, close hors quartile ;
- percentile sans historique minimal ;
- touch exact, gap au-delà du midpoint, touch et confirmation même bougie ;
- stop franchi avant confirmation ;
- pivot dont les bougies de droite ne sont pas encore disponibles ;
- target déjà touché avant l'entrée ;
- confirmation sur bougie non clôturée interdite ;
- expiration, absence de réentrée et intent exact-once.

### Temps et calendriers

- passages DST Europe et US, y compris semaines de décalage entre les deux ;
- week-end, jour férié et demi-session ;
- session externe → interne XYZ ;
- données reçues hors ordre et timestamps dupliqués ;
- reprise après gap sans reconstruire artificiellement l'opening drive.

### Exécution

- next-open sans lookahead ;
- stop et TP même bougie : stop prioritaire ;
- gap à travers stop ;
- slippage fonction de taille/profondeur ;
- changement growth mode ou deployer fee ;
- tick/size rounding défavorable ;
- funding à la frontière horaire ;
- target limit touché sans trade-through : non rempli au modèle central ;
- perte de flux avec position : fail-closed et alerte.

### Reproductibilité et sécurité

- checksum invalide, segment tronqué, manifest absent ;
- config inconnue ou champ supplémentaire refusé ;
- replay répété bit-identique ;
- aucune lecture de `.env` par les commandes de recherche ;
- aucun secret dans logs/rapports ;
- gateway shadow incapable d'envoyer un ordre.

## 13. Rapports attendus

```text
reports/
├── data_quality/
├── parity/
├── market_screen/
├── ablations/
├── walk_forward/
├── oos/
└── shadow/
```

Chaque synthèse Markdown référence le JSON immuable du run et son SHA-256. Les
baselines publiées ne sont jamais écrasées ; une nouvelle configuration produit
un nouvel ID.

La première décision de recherche doit répondre sans ambiguïté :

1. l'open réel est-il meilleur que les pseudo-opens ?
2. le pullback est-il meilleur que l'entrée immédiate ?
3. le niveau 0,5 est-il spécifique ou seulement représentatif d'une zone ?
4. la confirmation ajoute-t-elle assez d'edge pour compenser l'entrée tardive ?
5. quels marchés restent positifs après leurs frais propres ?
6. le sélecteur multi-marché améliore-t-il le portefeuille sans concentration ?

## 14. Ordre immédiat recommandé

1. Livrer M0 et figer les conventions du dépôt.
2. Implémenter les modèles/calendriers M1 avec fixtures DST avant toute stratégie.
3. Importer un petit snapshot legacy checksumé et interroger H0 sans collecte massive.
4. Livrer M2/M3 sur fixtures synthétiques.
5. Livrer le replay OHLC conservateur M4.
6. Produire le premier rapport de fréquence, sans optimiser le PnL.
7. Acquérir l'historique long U et qualifier H1/H2.
8. Exécuter M5 puis préenregistrer M6.
9. Décider : rejet, collecte supplémentaire ou candidat shadow.

Le premier jalon utile n'est donc pas « un backtest rentable », mais une chaîne
capable de prouver qu'un signal donné aurait été connu, sélectionné et exécuté
au moment annoncé, avec ses vrais coûts et sans degré de liberté caché.

## 15. Références de départ

Sources fonctionnelles à figer dans les manifests ou la documentation lors de
leur première utilisation :

- [vidéo « stratégie 0,5 »](https://www.youtube.com/watch?v=2mS6zUP_L9Q) et
  transcript local en lecture seule
  `/workspaces/hyperbot/video_passivebot.txt` ;
- [API Info Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint) ;
- [endpoints de métadonnées perps](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals) ;
- [subscriptions WebSocket](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) ;
- [frais Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
  et [funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding) ;
- [spécification HIP-3](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals) ;
- [archives historiques Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data) ;
- documentation opérateur XYZ sur les
  [commodities](https://docs.trade.xyz/asset-directory/commodities), les
  [indices](https://docs.trade.xyz/xyz-perps-specification/equity-perpetuals/xyz100-and-index-perpetuals),
  l'[oracle](https://docs.trade.xyz/perp-mechanics/oracle-price), les
  [discovery bounds](https://docs.trade.xyz/perp-mechanics/discovery-bounds) et
  le [Specification Index](https://docs.trade.xyz/consolidated-resources/specification-index).

Ces pages sont des sources vivantes. Le backtest ne doit pas supposer que leur
contenu actuel était historiquement applicable : les paramètres observés sont
snapshotés avec date, payload brut et checksum.
