# Spécification : simulateur Monte-Carlo de trading crypto sur Bitvavo

Version issue de la phase de conception (octobre 2026). Les sections « Décisions ouvertes » et « Plan de développement » sont à traiter en priorité.

## 1. Objectif

Faire tourner en parallèle 300 simulations de stratégies de trading automatisé en paper trading live, sur données de marché réelles Bitvavo, jusqu'à ce que l'utilisateur décide de les arrêter. Chaque simulation démarre avec 1 000 € et combine des choix différents : indicateurs techniques, filtre de tendance de fond, stops, trailing, take profit, règles de portefeuille. Le dashboard classe les simulations par P&L, détaille le top 5 et affiche une tornade des variables les plus influentes.

## 2. Principes de la stratégie

- **Signaux** sur bougies 4 h clôturées.
- **Filtre de fond** sur bougies 1 jour clôturées : autorise ou non les achats (bloquant, sans poids).
- **Stops** : niveau calculé sur la résolution du signal (4 h, éventuellement recalculé chaque heure selon la configuration), ne peut que monter. Surveillance du franchissement en temps réel (WebSocket ou bougies 1 min).
- **Spot uniquement** : long ou liquidités en EUR, pas de levier, pas de vente à découvert.
- **Score d'entrée** : chaque indicateur en signal d'achat apporte son poids ; achat si score ≥ seuil de la simulation et filtre de fond favorable.

## 3. Plan d'expérience Monte-Carlo

- **300 simulations = 150 paires.** On tire 150 « configurations de base ». Chacune est lancée deux fois : une version avec fenêtres d'indicateurs **typiques**, une version **optimisée**. Les deux jumelles ne diffèrent que par les valeurs des indicateurs (et du filtre de fond).
- **Tirage** : variables par catégories réparties de façon équilibrée (chaque option représentée à parts quasi égales) ; variables numériques par hypercube latin (`scipy.stats.qmc.LatinHypercube`). Contraintes gérées à la génération (EMA lente > EMA rapide, etc.). Graine aléatoire enregistrée : le tirage doit être reproductible.
- **Optimisation** (moitié optimisée seulement) : sur l'historique disponible, 70 % les plus anciens pour chercher les valeurs, 30 % suivants pour valider (découpage chronologique, jamais aléatoire). Le score de validation est enregistré mais **aucune simulation n'est écartée** sur cette base. L'optimisation ne touche que les fenêtres et seuils des indicateurs et du filtre de fond, pas les stops ni les règles de portefeuille. Le backtest d'optimisation doit simuler les stops avec des données au moins 1 h (idéalement 1 min) pour ne pas ignorer les mèches intra-bougie.
- **Mesure** : le P&L n'est mesuré que sur le live, depuis le lancement. L'historique sert uniquement à calibrer.

## 4. Variables d'entrée (tirées)

Valeur « typique » = réglage utilisé par la moitié non optimisée.

### Filtre de fond (1 jour)

| Variable | Plage / options | Typique |
|---|---|---|
| Utilisation du filtre | Oui / Non | Oui |
| Type | Prix > SMA ; EMA rapide > EMA lente ; momentum ROC | Prix > SMA |
| Fenêtre SMA | 50 à 200 j | 200 |
| Couple EMA | 20/50 à 50/200 | 50/200 |
| Fenêtre ROC | 30 à 90 j | 60 |
| Marge d'hystérésis | 0 à 3 % | 2 % |
| Action au retournement | Bloquer les achats ; bloquer et sortir | Bloquer |

### Signal principal (4 h) : 4 familles réparties équitablement

| Famille | Paramètres (plage) | Typique |
|---|---|---|
| Croisement EMA | rapide 8-30, lente 30-100 | 21 / 55 |
| MACD | rapide 8-15, lent 20-35, signal 7-12 | 12 / 26 / 9 |
| Supertrend | ATR 7-14, multiplicateur 2-4 | 10 / 3 |
| Cassure Donchian | période 20-55 | 20 |

### Confirmations optionnelles (chacune présente ou non)

| Indicateur | Paramètres (plage) | Typique |
|---|---|---|
| ADX | période 10-20, seuil 15-30 | 14 / 20 |
| Volume relatif | fenêtre 10-30, multiple 1,2-2 | 20 / 1,5 |
| RSI (éviter le surachat) | période 7-21, seuil max 60-80 | 14 / < 70 |

Poids : signal principal 0,5 à 0,8 (1 s'il n'y a pas de confirmation), le reste réparti entre les confirmations ; seuil du score 0,5 à 0,85. **Voir Décisions ouvertes** (tirés ou optimisés).

### Sortie et risque

| Variable | Plage / options | Typique |
|---|---|---|
| Stop initial | % fixe 2-10 % ; multiple ATR 4 h 1,5-4 ; plus bas des N bougies (5-20) | 2 × ATR |
| Trailing | Aucun ; ATR 2-4 ; % 3-15 % ; ligne Supertrend ; Chandelier | 3 × ATR |
| Activation du trailing | immédiate ; après gain de 1-5 % ou 1R | après 1R |
| Take profit | Aucun ; ratio 1,5-4R ; % fixe 5-30 % ; partiel (50 % à X R puis trailing) | Aucun |
| Sortie sur signal inverse | Oui / Non | Oui |
| Fréquence de recalcul du stop | clôture 4 h ; chaque heure | clôture 4 h |
| Durée maximale de détention | aucune ; 30-180 bougies 4 h | aucune |

### Exécution

| Variable | Plage / options | Typique |
|---|---|---|
| Type d'ordre d'entrée | Limite (maker) ; Marché (taker) | Limite |
| Expiration de l'ordre limite | 1 à 4 bougies 1 h | 1 |

### Règles de portefeuille

| Variable | Plage | Typique |
|---|---|---|
| % max sur une crypto | 20 à 100 % | 40 % |
| Exposition totale max | 50 à 100 % | 100 % |
| Montant max investi en une fois | 10 à 50 % du capital | 25 % |
| Dimensionnement | % fixe du capital ; basé sur le risque (0,5-2 % par trade) | risque 1 % |
| Renforcement | Non ; 2 à 3 entrées max | Non |
| **Positions simultanées max** | **2 à 10** | 5 |
| Pause après un stop | 0 à 6 bougies 4 h | 0 |
| Circuit breaker | drawdown 10-30 % → pause | 20 % |

## 5. Paramètres fixes

| Paramètre | Valeur |
|---|---|
| Capital initial | 1 000 € par simulation |
| Univers | paires de `CRYPTO_BASKET` (voir §7) **validées** par l'utilisateur, identiques pour toutes les simulations |
| Frais Bitvavo | 0,15 % maker, 0,25 % taker (palier < 100 000 €/mois), achat et vente. À vérifier sur la grille officielle au démarrage du projet. |
| Spread / slippage | estimé par paire depuis le carnet d'ordres (pas une valeur unique) ; pénalité supplémentaire sur stops déclenchés |
| Exécution simulée | ordre au marché au meilleur prix opposé du carnet + slippage ; ordre limite rempli seulement si le prix **traverse** le niveau ; montant minimum et précisions de prix / quantité de chaque marché respectés (lus via l'API) |
| Réinvestissement | oui (capital composé) |
| Graine | enregistrée par simulation |

## 6. Variables mesurées

| Variable | Définition |
|---|---|
| P&L | valeur courante − 1 000 €, frais déduits, positions valorisées nettes des frais de vente estimés (taux taker), en € et % |
| Drawdown max | plus forte baisse depuis un plus haut de la valeur du portefeuille |
| Nombre de trades | trades clôturés (positions ouvertes affichées à part) |
| Taux de réussite | part des trades clôturés gagnants, frais inclus |
| Frais totaux | cumul des frais d'achat et de vente simulés |
| Écart au buy & hold | P&L − P&L du buy & hold |

**Buy & hold** : 1 000 € répartis **à poids égaux** entre les paires retenues au lancement, frais d'achat taker déduits, achat unique sans rééquilibrage, valorisé net des frais de vente estimés (même convention que les simulations).

## 7. Configuration

Fichier `.env` (jamais versionné) :

```
CRYPTO_BASKET=ETH-EUR,BTC-EUR,PHA-EUR,NIL-EUR,WLD-EUR,PEPE-EUR,ARB-EUR,DOT-EUR,DOS-EUR,UP-EUR,KAT-EUR,ACU-EUR,IRYS-EUR,BIRB-EUR,ENA-EUR,XRP-EUR,SOL-EUR,ICNT-EUR,TREE-EUR
BITVAVO_API_KEY=        # optionnel en phase 1, lecture seule obligatoire
BITVAVO_API_SECRET=
DB_PATH=data/mcs.sqlite
SQLITE_JOURNAL_MODE=DELETE  # WAL (défaut, production) ; DELETE sur le partage SMB du NAS
LOG_DIR=logs
FEE_MAKER=0.0015             # frais en fraction, selon le palier Bitvavo de l'utilisateur
FEE_TAKER=0.0025
```

- `CRYPTO_BASKET` est la liste candidate. La sélection effective est validée dans le dashboard (onglet « Marchés du panier »), stockée en base, et ne s'applique qu'au prochain lancement après une remise à zéro.
- Par défaut, seules les paires avec **au moins 200 jours** d'historique journalier sont cochées. L'utilisateur peut en cocher d'autres ; le dashboard signale alors celles dont le filtre de fond et l'optimisation sont dégradés. **Voir Décisions ouvertes** pour leur traitement par le moteur.
- Pas de saisie de clé API dans le dashboard.

## 8. Machine à états (pilotage)

États : `NOT_STARTED` (« Non démarré »), `RUNNING` (« En cours »), `HOLD` (« Hold »). Statut d'optimisation : `TODO`, `RUNNING`, `DONE`.

| Action | Autorisée si | Effet |
|---|---|---|
| Optimiser les indicateurs | `NOT_STARTED` et optimisation pas en cours | recalibre la moitié optimisée sur l'historique |
| Lancer | `NOT_STARTED` et optimisation `DONE` | démarre le live, fige le panier et les configurations |
| Arrêter | `RUNNING` | passe en `HOLD` |
| Reprendre | `HOLD` | repasse en `RUNNING`, la gestion reprend là où elle s'était arrêtée |
| Remettre à zéro | `HOLD`, avec confirmation | efface positions, ordres, P&L live et historique de valeur ; conserve les configurations tirées ; retour à `NOT_STARTED` |

**Hold** = arrêt sans remise à zéro : aucun nouvel ordre, ni signal ni stop ; positions conservées et valorisées au cours Bitvavo ; le P&L continue d'évoluer ; les périodes de hold sont enregistrées et grisées sur le graphique de valeur. (En phase réelle, prévoir une variante qui garde les stops natifs actifs.)

Le dashboard pilote le moteur en écrivant une commande dans la table `commands` ; le moteur la lit en continu, l'exécute et met à jour `engine_state`.

## 9. Architecture

```
mcs/
  config.py        # lecture .env, validation du panier
  bitvavo/         # client données publiques : REST (historique, marchés) + WebSocket (ticker, carnet), reconnexion auto, rate limits
  storage/         # SQLite WAL, schéma, accès
  indicators/      # EMA, MACD, Supertrend, Donchian, ADX, RSI, volume relatif, ATR, SMA, ROC (pandas / pandas-ta ou TA-Lib)
  strategy/        # score d'entrée, filtre de fond, logique de sortie
  risk/            # stops, trailing, take profit, circuit breaker, règles de portefeuille
  broker/          # interface Broker ; PaperBroker (seul implémenté en phase 1)
  sampling.py      # génération des 150 paires (équilibrage + hypercube latin + graine)
  optimizer.py     # optimisation 70/30 par simulation optimisée
  engine.py        # boucle live : un flux de données partagé, 300 simulations, commandes
  metrics.py       # P&L, drawdown, trades, réussite, frais, buy & hold, tornade
dashboard/
  app.py           # Streamlit (voir Décisions ouvertes)
tests/
```

**Interface `Broker` commune** : la stratégie ne sait pas si elle parle au `PaperBroker` ou à un futur `BitvavoBroker`. Le passage au réel doit être un changement de configuration.

Les 300 simulations partagent **un seul** flux de données ; chaque bougie ou tick est distribué à toutes.

## 10. Schéma de données (une seule base SQLite)

| Table | Contenu principal | Écriture |
|---|---|---|
| `simulations` | id, pair_id, mode (typique/optimisé), toutes les variables tirées (JSON + colonnes clés), graine, score de validation | au tirage / après optimisation |
| `indicator_params` | sim_id, rôle (principal / confirmation / filtre de fond), indicateur, valeurs utilisées, valeurs typiques, poids, seuil du score | au tirage / après optimisation |
| `markets` | paire, jours d'historique, sélectionnée (bool), spread estimé, volume 24 h, ATR 4 h, précisions et minimum d'ordre | quotidien + à la validation du panier |
| `candles` | paire, intervalle (1m, 1h, 4h, 1d), timestamp, OHLCV | au fil de l'eau |
| `orders` | sim_id, paire, type, côté, prix, quantité, frais, motif (signal, stop, trailing, TP, signal inverse, durée max) | à chaque événement |
| `positions` | sim_id, paire, lots, quantité, prix d'entrée moyen, coût investi, frais d'achat, stop, trailing, take profit | à chaque modification |
| `portfolio_values` | sim_id, timestamp, valeur totale, liquidités | **chaque heure** (pas à la minute) |
| `latest_prices` | paire, cours, meilleur bid / ask, horodatage | chaque minute, en écrasant |
| `metrics` | sim_id, P&L, drawdown max, trades, réussite, frais, écart buy & hold | chaque heure |
| `buy_and_hold` | paire, poids, montant alloué, frais, prix de départ, quantité | au lancement |
| `engine_state` | statut, statut d'optimisation, dates, durées actives et en hold | à chaque changement |
| `run_periods` | début, fin, type (actif / hold / interruption) | à chaque changement d'état |
| `sim_state` | sim_id, pause après stop, circuit breaker, plus haut, P&L des trades | à chaque clôture 4 h et sortie |
| `commands` | commande, demandée le, traitée le, résultat | dashboard → moteur |

La valeur à la minute du top 5 est calculée à la volée (positions × derniers prix), pas stockée.

## 11. Dashboard

Référence visuelle : `docs/maquette/`. Rafraîchissement automatique des cours et du détail du top 5 chaque minute ; le reste chaque heure.

**Barre de pilotage** (toutes pages) : statut, infos d'optimisation et de durée, boutons Optimiser / Lancer-Arrêter-Reprendre / Remettre à zéro (avec confirmation), selon la machine à états du §8.

**Onglet « Suivi des simulations »**
- Phrase de synthèse + 6 indicateurs : meilleur P&L, P&L médian, P&L buy & hold, part des simulations qui le battent, écart moyen optimisé − typique entre jumelles, trades clôturés moyens (seuil de 10 pour une tornade exploitable, sinon bandeau « résultats préliminaires »).
- Graphique de la valeur du top 5 et du buy & hold, périodes de hold grisées.
- **Tornade** (recalculée chaque heure) : pour chaque variable par catégories, P&L moyen de la pire et de la meilleure option ; pour chaque variable numérique, tiers bas vs tiers haut ; barres autour du P&L moyen, triées par amplitude ; barre pâle si l'écart < 2 erreurs-types ; paramètres propres à une famille calculés sur cette famille seulement (effectif affiché) ; le calibrage mesuré par l'écart entre jumelles.
- Top 5 par P&L (P&L, %, drawdown, trades, réussite, frais, écart buy & hold). Clic sur une simulation : sa configuration, ses positions (actif, lots, quantité, prix d'entrée moyen, cours actuel rafraîchi à la minute, coût investi, valeur, frais achat, frais vente estimés, P&L latent, stop, trailing, take profit) et ses liquidités.
- Buy & hold détaillé par actif.
- Liste des 300 simulations : tri par colonne, recherche, filtre typique / optimisé.

**Onglet « Paramètres des indicateurs »** : pour chaque simulation, chaque indicateur (rôle, valeurs utilisées, valeurs typiques, poids, seuil) ; repère visuel quand une valeur optimisée diffère du typique ; recherche, filtres calibrage et rôle.

**Onglet « Marchés du panier »** : pour chaque paire, case à cocher « prendre en compte », jours d'historique, 200 jours atteints ou non, spread estimé (médiane 24 h du carnet), coût aller-retour (2 × taker + spread), volume 24 h, ATR 4 h ; aperçu de la ligne `CRYPTO_BASKET` résultante ; bouton de validation.

## 12. Contraintes techniques

- Respect des limites de requêtes de l'API Bitvavo ; reconnexion automatique du WebSocket ; journalisation des coupures.
- Vérifier dans la documentation officielle Bitvavo (et le SDK Python officiel s'il est utilisé) : endpoints de bougies et intervalles disponibles, profondeur d'historique, carnet d'ordres, précisions et minimums par marché, limites de requêtes.
- Précision : calculs en `Decimal`, arrondis selon les règles de chaque marché (PEPE : quantités énormes, prix minuscules).
- Journal complet : chaque signal, ordre simulé, prix du carnet au moment de la décision, motif de sortie.
- Constaté dans la documentation officielle et sur l'API (octobre 2026) :
  - limite de 1 000 points de poids par minute et par IP (en-têtes `bitvavo-ratelimit-remaining` / `-resetat`) ; bougies, carnet, ticker 24 h d'un marché : 1 point ;
  - `GET /{market}/candles` : intervalles 1m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 1W, 1M ; 1 440 bougies au plus par requête, de la plus récente à la plus ancienne ; `start` inclus, `end` exclu ; bougies alignées sur UTC ; aucune bougie pour une période sans transaction ; la dernière bougie renvoyée est en cours ;
  - `GET /markets` : `pricePrecision` est désormais nul ; les règles de prix et de quantité sont `tickSize`, `quantityDecimals`, `notionalDecimals`, `minOrderInBaseAsset`, `minOrderInQuoteAsset` (5 € pour tout le panier) ; tout le panier est en catégorie de frais A ;
  - WebSocket `wss://ws.bitvavo.com/v2/` : le ticker n'envoie que les champs modifiés ; l'événement `candle` renvoie la bougie 1 min en cours à chaque transaction ;
  - **frais non vérifiés** : la grille publique n'est pas accessible sans navigateur et `GET /account/fees` exige une clé. Les taux du §5 restent à confirmer (par exemple avec la clé en lecture seule).
- Tests : indicateurs (valeurs de référence), exécution simulée (limite traversée vs touchée, frais, minimums), stops (ne redescendent jamais), machine à états, reproductibilité du tirage, absence de fuite d'information future dans l'optimisation.

## 13. Décisions (tranchées par l'utilisateur le 24/10/2026)

1. **Poids des indicateurs et seuil du score** : **tirés au hasard**, identiques entre jumelles (ils font partie de la configuration de base, l'optimisation n'y touche pas).
2. **Paires sans 200 jours d'historique** mais cochées : **exclues des signaux** (aucun nouvel achat) jusqu'à atteindre l'historique ; elles restent valorisées et comptent dans le buy & hold.
3. **Dashboard** : **Streamlit**.
4. **Exécution** :
   - développement : Python sur le Mac, code **et base SQLite sur le partage SMB du NAS** (pas de base sur le disque local du Mac). WAL n'étant pas fiable sur un partage réseau, la base y tourne en journal classique (`SQLITE_JOURNAL_MODE=DELETE`) ;
   - production : conteneur **Docker sur un serveur Proxmox**, base sur un volume local, `SQLITE_JOURNAL_MODE=WAL`.
5. **Profondeur d'historique** : tout l'historique disponible en 1 j, 4 h et 1 h ; **90 jours en 1 min** (puis collecte continue). Les stops de l'optimisation sont simulés en 1 h, les 90 jours en 1 min servent à contrôler cette approximation.
6. **Signaux = états** (étape 2) : un indicateur apporte son poids tant que sa condition d'achat est vraie (EMA rapide > lente, MACD > signal, Supertrend haussier ; confirmations ADX > seuil avec +DI > −DI, volume relatif ≥ multiple, RSI < seuil). Exception : la cassure Donchian n'est vraie que sur les bougies qui clôturent au-dessus du plus haut des N bougies précédentes. Le signal inverse (sortie) est l'état opposé (Donchian : clôture sous le plus bas des N précédentes).
7. **Hystérésis du filtre de fond** : bande symétrique. Écart = prix / SMA − 1, EMA rapide / EMA lente − 1, ou ROC / 100 ; favorable au-dessus de +marge, défavorable sous −marge, état conservé entre les deux ; défavorable tant que l'indicateur n'est pas calculable.
8. **Bougies manquantes** (aucune transaction, panne Bitvavo) : comblées pour les indicateurs avec O = H = L = C = clôture précédente et volume 0.
9. **Implémentation des indicateurs** : code maison pandas/numpy (pas de pandas-ta) avec les conventions de TA-Lib, qui sert de référence dans les tests seulement. Une paire est exclue des signaux tant qu'elle n'a pas l'historique minimal (`warmup`) de chaque indicateur de la simulation.
10. **Exécution simulée (étape 3)** :
    - ordre au marché : parcours du carnet réel (profondeur 1 000, cache de 2 s par paire), prix moyen pondéré, frais taker ; carnet insuffisant : reste exécuté au dernier niveau, alerte journalisée ;
    - **stop ou trailing déclenché** : prix de vente dégradé en plus **d'un spread médian 24 h** de la paire (à défaut, spread instantané du carnet) ;
    - **ordre limite d'achat** : liquidités réservées au placement ; rempli **en entier, au prix limite**, frais maker, dès qu'une bougie 1 min entièrement postérieure au placement a un plus bas **strictement** inférieur ; pas de remplissage partiel ;
    - frais prélevés en € (achat : montant + frais ; vente : montant − frais), arrondis à 1e-8 € ; taux réglables dans `.env` (`FEE_MAKER`, `FEE_TAKER`), **à confirmer par l'utilisateur** ;
    - spread estimé = médiane 24 h des écarts bid / ask relevés chaque minute par le collecteur, écrite chaque heure dans `markets.spread_estimate_pct` (au moins 30 relevés).
11. **Entrée** : le signal principal doit être en achat **et** le score doit atteindre le seuil (les confirmations seules ne déclenchent jamais d'achat).
12. **Prix de l'ordre limite d'entrée** : meilleur bid au moment de la décision.
13. **Dimensionnement** : % fixe → taille = montant max investi en une fois × capital ; basé sur le risque → capital × risque par trade ÷ distance relative au stop initial, plafonné par le montant max en une fois. Dans les deux cas, plafonds : % max sur une crypto, exposition totale max (positions + achats en attente), liquidités. Capital = valeur totale du portefeuille, positions valorisées nettes des frais de vente estimés.
14. **Renforcement** : nouvelle entrée seulement si le score atteint encore le seuil et si le cours a progressé d'au moins 1R depuis la dernière entrée ; le stop de toute la position remonte alors au moins au prix d'entrée moyen. R reste celui de la première entrée.
15. **Pause après un stop** (stop ou trailing) : seule la paire concernée est bloquée pendant N bougies 4 h.
16. **Circuit breaker** : drawdown ≥ seuil depuis le plus haut → plus de nouvelle entrée pendant 7 jours (42 bougies 4 h), positions et stops conservés ; à la reprise, le plus haut de référence est remis à la valeur courante. Valeur et plus haut mesurés à chaque clôture 4 h.
17. **Take profit** : ordre limite de vente posé dès l'entrée (frais maker), rempli quand une bougie 1 min a un plus haut strictement supérieur ; la quantité est bloquée et l'ordre est annulé avant toute sortie au marché. Partiel : 50 % au niveau X R, puis le reste suit le trailing (activé d'office ; sans trailing configuré, le stop remonte au prix d'entrée moyen).
18. **Trailing Chandelier** : plus haut des 22 dernières bougies 4 h − k × ATR, k entre 2 et 4.
19. **Choix d'implémentation (étape 4)** : ATR 4 h de période 14 pour les stops et trailings ; trailing ATR et % calculés depuis le plus haut atteint depuis l'entrée ; trailing Supertrend = ligne Supertrend des paramètres de la simulation si sa famille est Supertrend, sinon 10 / 3 ; un stop est déclenché quand le plus bas d'une bougie 1 min atteint son niveau ; sorties évaluées à chaque clôture 4 h dans l'ordre : retournement du filtre (si « bloquer et sortir »), signal inverse, durée max ; entrées classées par score décroissant.
20. **Valeurs des indicateurs non tirées** (étape 5) : jumelle typique = valeurs typiques du §4 ; jumelle optimisée = résultat de l'optimiseur (étape 6), valeurs typiques en attendant. Le tirage porte sur les familles, la présence des confirmations, le filtre de fond (utilisation, type, action), les stops, sorties, exécution, règles de portefeuille, le poids principal et le seuil.
21. **Poids des confirmations** : le reste (1 − poids principal) est réparti à parts égales entre les confirmations présentes.
22. **Mise en œuvre du tirage** (`python -m mcs.sampling generate [--seed N] [--force]`) :
    - variables par catégories équilibrées (écart ≤ 1) dans le sous-groupe où elles s'appliquent (ex. type de filtre parmi les configurations qui l'utilisent), mélangées indépendamment ;
    - variables numériques : un hypercube latin par groupe de variables partageant la même condition, sur le sous-groupe concerné ; entiers répartis uniformément ; fractions arrondies à 4 décimales (risque par trade à 5), multiples à 3 ;
    - renforcement tiré parmi 1 (non), 2 ou 3 entrées ; durée max tirée entre « aucune » et « limitée », puis 30 à 180 bougies ;
    - simulations numérotées 2k − 1 (typique) et 2k (optimisée) pour la configuration k ; graine maîtresse dans `engine_state.master_seed`, graine propre à chaque simulation dérivée de la maîtresse ; tirage brut conservé dans `simulations.draw_json` (pour la tornade) ;
    - génération refusée hors état « Non démarré », et si un plan existe sauf avec `--force`.
23. **Critère d'optimisation** : rendement net de frais ÷ drawdown max (ratio de Calmar, drawdown plancher 1 %) ; score nul sous 10 trades clôturés. Même critère pour le score de validation (`simulations.validation_score`, score d'apprentissage dans `train_score`).
24. **Recherche** : 40 essais par simulation optimisée — les valeurs typiques puis 39 jeux tirés par hypercube latin dans les plages du §4 (graine de la simulation : reproductible) ; le meilleur sur l'apprentissage est retenu. Contraintes : EMA lente > rapide (signal et filtre). Seules les valeurs des indicateurs présents et du filtre de fond (fenêtre, hystérésis 0-3 %) changent.
25. **Période** : tout l'historique (mars 2019 → aujourd'hui), coupé à 70 % du temps, aligné sur les bougies 4 h ; la validation repart de 1 000 € à la coupure, indicateurs initialisés par l'historique antérieur. La règle des 200 jours s'applique à chaque date.
26. **Exécution historique** : ordre au marché au dernier cours 1 h ± un demi-spread estimé (médiane actuelle de la paire, 0,1 % à défaut), frais taker ; stop au niveau du stop, ou à l'ouverture de l'heure si elle est dessous, moins un spread ; limite et take profit remplis si le plus bas / plus haut 1 h traverse strictement ; stop vérifié avant le take profit dans la même heure. Minimums et arrondis de chaque marché respectés (une position valant moins de 5 € ne peut pas être vendue, comme sur Bitvavo).
27. **Mise en œuvre** (`python -m mcs.optimizer run [--workers N] [--trials 40] [--only ID…] [--force]`) : backtest avec la même classe `Simulation` que le live, déroulé heure par heure ; indicateurs précalculés sur tout l'historique et lus à la dernière bougie clôturée (un test vérifie qu'un historique tronqué donne les mêmes décisions) ; calcul parallèle, reprise des simulations non traitées après interruption ; statut `TODO` → `RUNNING` → `DONE` dans `engine_state`.
28. **Hold** : les entrées en attente sont annulées ; les take profit sont annulés puis re-posés au même niveau à la reprise. Aucun ordre ne s'exécute pendant le Hold ; les valeurs des portefeuilles continuent d'être écrites.
29. **Coupure non prévue** (plantage, redémarrage, réseau) : à la relance, la période depuis le dernier signe de vie (> 5 min) est enregistrée comme `outage` dans `run_periods` (grisée comme un Hold) ; aucune décision n'est rejouée ; les stops franchis pendant la coupure s'exécutent au cours du moment.
30. **Optimiser** depuis le dashboard : le moteur lance `python -m mcs.optimizer run --workers N` dans un processus séparé (`OPTIMIZER_WORKERS` dans `.env`, 4 par défaut) ; la collecte continue.
31. **Un seul processus** : le moteur (`python -m mcs.engine`) intègre la collecte (un seul WebSocket partagé par les 300 simulations) ; `collector live` reste utilisable seul.
32. **Mise en œuvre du moteur (étape 7)** :
    - bougie 1 min WebSocket (en cours) : stops des simulations qui détiennent la paire, puis ordres limite ; chaque minute : expiration des ordres limite et signe de vie (`engine_state.heartbeat_at`) ; clôture 1 h : nouvelles bougies chargées, trailing horaire, valeurs écrites dans `portfolio_values` ; clôture 4 h : indicateurs précalculés dans un thread puis sorties, trailings et entrées des 300 simulations (≈ 20 s mesurées) ;
    - « Lancer » fige le panier (`engine_state.basket_json`) et achète le buy & hold au carnet réel ; « Remettre à zéro » vide ordres, positions, valeurs, métriques, buy & hold, périodes et `sim_state`, et conserve les configurations (y compris optimisées) ;
    - redémarrage : comptes reconstitués à partir des ordres, positions depuis `positions`, pause / circuit breaker / plus haut / P&L des trades depuis la table `sim_state` ;
    - production : service `engine` dans `docker-compose.yml`.

## 14. Plan de développement proposé

1. Socle : projet, config, SQLite + schéma, client Bitvavo données publiques, collecte et stockage des bougies.
2. Indicateurs + tests.
3. `PaperBroker` (frais, spread, limite traversée, minimums, précisions) + tests.
4. Stratégie (score, filtre de fond) et gestion du risque (stops, trailing, TP, règles de portefeuille).
5. Génération du plan Monte-Carlo (150 paires, graine).
6. Optimiseur 70/30.
7. Moteur live (flux partagé, 300 simulations, commandes, états, hold).
8. Métriques et tornade.
9. Dashboard (3 onglets + barre de pilotage).
10. Période de rodage : vérifier la mécanique (données, reconnexions, logs) avant d'interpréter les résultats.
