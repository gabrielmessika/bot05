# BOT05 — procédure d’incident shadow

Cette procédure concerne uniquement le runner shadow public. BOT05 ne possède
aucune capacité de signature ou d’envoi d’ordre.

## Déclenchement fail-closed

Le kill switch est verrouillé dès qu’une position théorique coexiste avec une
perte de connexion, une donnée stale, une dérive d’horloge, un gap de séquence,
ou dès que le replay quotidien diverge des intents observés. Le verrou reste
actif après reconnexion : aucun retour automatique à l’état nominal n’est
autorisé.

## Diagnostic et reprise

1. Conserver les manifests, métriques, séquences et checksums de l’incident.
2. Réparer tout gap avec la plage exacte de séquences manquantes et son SHA-256.
3. Rejouer la journée et exiger la parité exacte des identifiants d’intent.
4. Vérifier connexion, fraîcheur, délai de transport et offset d’horloge.
5. Produire une approbation humaine liée à l’identifiant checksumé de l’incident.
6. Réinitialiser le kill switch seulement si le flux et la réconciliation sont
   tous deux propres.

Une reprise shadow ne constitue jamais une autorisation canary ou live.
