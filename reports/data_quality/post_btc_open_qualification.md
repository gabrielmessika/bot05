# BOT05 — couverture locale initiale

Ce rapport est un plan d'acquisition local-first. Aucun appel réseau n'a été
effectué. Les datasets partagés restent candidats tant que leur schéma, leur
intégrité et leurs gaps n'ont pas été qualifiés par BOT05.

- SHA-256 du JSON : `621add2d5712b037a6196802f61697df3496fa6d7a2c1a03b1712ddd4ed44d3b`
- SHA-256 du code : `fbc38fa53b8bae67e99b5fa4a777aceff1c69f56d82cc5a6b25c7760b12369eb`
- SHA-256 de configuration : `f64d828ce18514bb91b7c9d7250b46b6dedb58cef8af2e0d08f411861bbb9247`
- Assets découverts : 66
- Problèmes d'inventaire : 0
- Besoins marché/canal : 4
- Fetch distant activé : `False`

## Décisions par besoin

| Marché | Canal | Action | Candidats locaux | Gaps distants |
|---|---|---|---:|---|
| `BTC` | `trades` | `reuse_local` | 0 | — |
| `BTC` | `bbo` | `reuse_local` | 0 | — |
| `BTC` | `candles_1m` | `reuse_local` | 0 | — |
| `BTC` | `candles_5m` | `reuse_local` | 0 | — |

## Limites

- H1 partagé ne vaut pas H2 BOT05 : la provenance du collector est conservée.
- L legacy sert à la pré-recherche, jamais seul à une preuve d'exécution.
- Un candidat local doit passer checksum, schéma, timestamps, doublons et gaps.
- Les gaps listés ne sont exécutables que dans un futur lot de fetch public.
