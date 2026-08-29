# BOT05 — premier smoke replay historique

- SHA-256 du JSON : `4471b8dbc3743e4b7b2012c833ac3b781a8c052935aab59dc3e488875f5e4338`
- Marché/session : `BTC` / `us_cash_open`
- Signal : `long` à `2026-08-21T14:25:00.669Z`
- Gate risque : `True`
- Conclusion : `données insuffisantes`
- Promotion : interdite

## Modèles

- `ohlc_conservative` : `closed`, sortie `time`, PnL net `0.0229000`
- `ohlc_optimistic` : `closed`, sortie `time`, PnL net `0.0549000`
- `trade_bbo_central` : `closed`, sortie `time`, PnL net `0.0529000`
- `trade_bbo_stress` : `closed`, sortie `time`, PnL net `-0.06365000`

## Limites

Une seule session H1 est observée. Les frais, le calendrier, la
définition de marché et le funding ne disposent pas tous d’un snapshot
historique local qualifié. Ce run valide la chaîne technique, pas l’edge.
