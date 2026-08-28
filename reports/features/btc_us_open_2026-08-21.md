# BOT05 — audit causal de l’opening drive BTC

Ce rapport dérive localement les bougies depuis les trades H1 qualifiés.
Aucun appel réseau ni donnée non qualifiée n’a été utilisé.

- SHA-256 du JSON : `4b44bf9e36c110e280fde5de8d1bc796cbf916b28ab9d2f0fad944afb9eabc56`
- Fenêtre : `2026-08-21T13:30:00.000Z` → `2026-08-21T13:45:00.000Z`
- Trades : 18617
- Bougies 1m : 15 (gaps : 0)
- Bougies 5m : 3 (gaps : 0)
- Parité 5m directe / rollup 1m : `True`
- Opening drive : accepté par le filtre de quartile externe

## Limites

- La parité H0 reste en attente : aucune source officielle checksummée
  n’est présente sur cette fenêtre.
- Une session ne suffit pas aux filtres roulants q50/q75 ;
  vingt sessions comparables antérieures sont requises.
- Le niveau de preuve reste H1 partagé et ne devient pas H2 BOT05.
