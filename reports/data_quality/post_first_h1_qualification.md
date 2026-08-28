# BOT05 — couverture locale initiale

Ce rapport est un plan d'acquisition local-first. Aucun appel réseau n'a été
effectué. Les datasets partagés restent candidats tant que leur schéma, leur
intégrité et leurs gaps n'ont pas été qualifiés par BOT05.

- SHA-256 du JSON : `46dcf754640c2eb8ab1217bd56cb568198e66e4f333aefb243a1b69a810ca32e`
- SHA-256 du code : `d39c33da0ad01ab1a301a8dfefaa44e4aeee8082861fa21f30c501a410763611`
- SHA-256 de configuration : `fac99338b9c6d23ac99a4ec5c50910f996e6908172df19c88b84e77d4f20c090`
- Assets découverts : 64
- Problèmes d'inventaire : 0
- Besoins marché/canal : 42
- Fetch distant activé : `False`

## Décisions par besoin

| Marché | Canal | Action | Candidats locaux | Gaps distants |
|---|---|---|---:|---|
| `xyz:SILVER` | `trades` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SILVER` | `bbo` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SILVER` | `l2` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SILVER` | `candles_1m` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SILVER` | `candles_5m` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SILVER` | `market_context` | `remote_fetch_disabled` | 0 | 2026-08-16T00:00:00.000Z → 2026-08-25T00:00:00.000Z |
| `HYPE` | `trades` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `HYPE` | `bbo` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `HYPE` | `l2` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `HYPE` | `candles_1m` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `HYPE` | `candles_5m` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `HYPE` | `market_context` | `remote_fetch_disabled` | 0 | 2026-08-16T00:00:00.000Z → 2026-08-25T00:00:00.000Z |
| `xyz:SP500` | `trades` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SP500` | `bbo` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SP500` | `l2` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SP500` | `candles_1m` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SP500` | `candles_5m` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:SP500` | `market_context` | `remote_fetch_disabled` | 0 | 2026-08-16T00:00:00.000Z → 2026-08-25T00:00:00.000Z |
| `xyz:GOLD` | `trades` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:GOLD` | `bbo` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:GOLD` | `l2` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:GOLD` | `candles_1m` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:GOLD` | `candles_5m` | `qualify_local_remote_disabled` | 5 | 3 intervalles |
| `xyz:GOLD` | `market_context` | `remote_fetch_disabled` | 0 | 2026-08-16T00:00:00.000Z → 2026-08-25T00:00:00.000Z |
| `BTC` | `trades` | `qualify_local_remote_disabled` | 18 | 3 intervalles |
| `BTC` | `bbo` | `qualify_local_remote_disabled` | 16 | 3 intervalles |
| `BTC` | `l2` | `qualify_local_remote_disabled` | 18 | 3 intervalles |
| `BTC` | `candles_1m` | `qualify_local_remote_disabled` | 18 | 3 intervalles |
| `BTC` | `candles_5m` | `qualify_local_remote_disabled` | 18 | 3 intervalles |
| `BTC` | `market_context` | `remote_fetch_disabled` | 0 | 2026-08-16T00:00:00.000Z → 2026-08-25T00:00:00.000Z |
| `ETH` | `trades` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `ETH` | `bbo` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `ETH` | `l2` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `ETH` | `candles_1m` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `ETH` | `candles_5m` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `ETH` | `market_context` | `remote_fetch_disabled` | 0 | 2026-08-16T00:00:00.000Z → 2026-08-25T00:00:00.000Z |
| `SOL` | `trades` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `SOL` | `bbo` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `SOL` | `l2` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `SOL` | `candles_1m` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `SOL` | `candles_5m` | `qualify_local_remote_disabled` | 11 | 3 intervalles |
| `SOL` | `market_context` | `remote_fetch_disabled` | 0 | 2026-08-16T00:00:00.000Z → 2026-08-25T00:00:00.000Z |

## Limites

- H1 partagé ne vaut pas H2 BOT05 : la provenance du collector est conservée.
- L legacy sert à la pré-recherche, jamais seul à une preuve d'exécution.
- Un candidat local doit passer checksum, schéma, timestamps, doublons et gaps.
- Les gaps listés ne sont exécutables que dans un futur lot de fetch public.
