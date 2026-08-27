# BOT05 — couverture locale initiale

Ce rapport est un plan d'acquisition local-first. Aucun appel réseau n'a été
effectué. Les datasets partagés restent candidats tant que leur schéma, leur
intégrité et leurs gaps n'ont pas été qualifiés par BOT05.

- SHA-256 du JSON : `096af9241b895f70e581d5e12110a74326898a77e08f00314f7dc447d516ab27`
- SHA-256 du code : `36cd9ffe4c4ae5c86734ad94330d683c4d80eb613134ed7cd1a4f838629000d9`
- SHA-256 de configuration : `08a014bcb67439009a3554f69037e7d0c029c0e5f57ab38a1a2502b0fa36cfc9`
- Assets découverts : 59
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
| `BTC` | `trades` | `qualify_local_remote_disabled` | 17 | 3 intervalles |
| `BTC` | `bbo` | `qualify_local_remote_disabled` | 15 | 3 intervalles |
| `BTC` | `l2` | `qualify_local_remote_disabled` | 17 | 3 intervalles |
| `BTC` | `candles_1m` | `qualify_local_remote_disabled` | 17 | 3 intervalles |
| `BTC` | `candles_5m` | `qualify_local_remote_disabled` | 17 | 3 intervalles |
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
