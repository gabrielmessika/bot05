# BOT05 — audit local des gates de données D2

Cet audit inspecte uniquement les manifests et assets locaux. Aucun appel
réseau et aucun scan événementiel massif n’ont été effectués.

- SHA-256 du JSON : `8f6e1fd89c955ccd349050971f3093c8172ee674000938b1990ac59cf1138193`
- Assets H0 officiels chevauchant la fenêtre : 0
- Sessions antérieures requises : 20
- Borne haute locale de dates candidates : 5
- Déficit minimal : 15

## Conclusion

- La parité H0 ne peut pas être fermée avec les fichiers locaux.
- Le seuil de 20 historiques ne peut pas être atteint avant la cible,
  même en comptant les week-ends comme candidats.
- Les fenêtres H1 restent manifest-only : chacune doit
  encore passer la qualification événementielle avant réutilisation.
