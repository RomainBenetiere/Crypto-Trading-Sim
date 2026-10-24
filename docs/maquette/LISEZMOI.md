# Maquette du dashboard

- `index.html` : code de la page de la maquette validée par l'utilisateur. Elle a été écrite pour l'environnement des artefacts Claude (objet global `dash`, `d3` fourni) : elle ne s'ouvre pas telle quelle dans un navigateur. Elle sert de **référence** pour la mise en page, les libellés, les calculs (synthèse, tornade, valorisation des positions, buy & hold) et le formatage des nombres (fonction `tiny` pour les prix < 0,001 €).
- `donnees_exemple/` : données **fictives** utilisées par la maquette. Utiles comme fixtures de test ou pour développer le dashboard avant que le moteur produise de vraies données. Elles correspondent aux tables de `docs/SPEC.md` §10 (noms de colonnes en français dans la maquette).
