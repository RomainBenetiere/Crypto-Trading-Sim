# Déploiement sur Proxmox et rodage (étape 10)

Le moteur et le dashboard tournent dans deux conteneurs Docker qui partagent la base SQLite
(`./data/mcs.sqlite`, mode WAL) et les journaux (`./logs`). Aucun ordre réel : seul le
`PaperBroker` existe.

## 1. Préparer le serveur

Dans une VM ou un conteneur LXC Proxmox avec Docker et le plugin Compose :

```bash
git clone https://github.com/RomainBenetiere/Crypto-Trading-Sim.git
cd Crypto-Trading-Sim
mkdir -p data logs
sudo chown -R 1000:1000 data logs   # l'image tourne avec l'utilisateur mcs (uid 1000)
```

Créer `.env` à partir de `.env.example` (le panier, les frais ; la clé API reste vide en phase 1).
`DB_PATH`, `SQLITE_JOURNAL_MODE` et `LOG_DIR` sont imposés par `docker-compose.yml`.

## 2. Reprendre la base du développement

La base du NAS contient déjà l'historique des bougies, le plan des 300 simulations et
l'optimisation. Aucun moteur ne doit écrire dedans pendant la copie. Le serveur la
télécharge directement depuis le partage SMB du NAS (sans montage, ce qui fonctionne aussi
dans un conteneur LXC non privilégié) ; remplacer `IP_DU_NAS` par l'adresse du NAS :

```bash
apt update && apt install -y smbclient
smbclient //IP_DU_NAS/home -U RomainB -c 'get Github/Crypto-Trading-Sim/data/mcs.sqlite data/mcs.sqlite'
chown -R 1000:1000 data logs && ls -la data
```

Les commandes s'exécutent depuis le dossier du projet (`cd ~/Crypto-Trading-Sim`). Le moteur
passe la base en WAL au premier démarrage. Ne plus utiliser la copie du NAS ensuite.

## 3. Démarrer

```bash
docker compose up -d --build
docker compose logs -f engine          # Ctrl+C pour quitter le suivi
```

Dashboard : `http://<ip-du-serveur>:8501` depuis le réseau local (port à ne pas exposer à
Internet). Le moteur collecte les données dès son démarrage, à l'état « Non démarré ».

## 4. Lancer les simulations

Avec le bouton « Lancer les simulations » du dashboard, ou :

```bash
docker compose exec engine python -m mcs.control start
docker compose exec engine python -m mcs.control status
```

Les métriques apparaissent à l'heure pleine suivante ; la première clôture 4 h place les
premiers ordres.

## 5. Rodage : 7 jours

Rapport de santé (aussi dans l'encart « Santé du moteur » du dashboard) :

```bash
docker compose exec engine python -m mcs.health --hours 72
```

Épreuves à faire pendant la première semaine, chacune suivie d'un rapport de santé :

| Épreuve | Commande | Attendu |
|---|---|---|
| Arrêt brutal et relance | `docker compose kill engine` puis, 10 min plus tard, `docker compose up -d engine` | une interruption de ~10 min enregistrée et grisée ; état restauré ; aucun trou de bougies après rattrapage |
| Coupure réseau | `docker network disconnect crypto-trading-sim_default crypto-trading-sim-engine-1` puis `connect` 5 min plus tard | « WebSocket coupé » puis « reconnecté » dans les journaux ; bougies rattrapées |
| Hold puis reprise | `python -m mcs.control stop`, puis `resume` une heure plus tard | ordres limite annulés, take profit re-posés à la reprise, valeurs écrites pendant le Hold |
| Redémarrage du dashboard | `docker compose restart dashboard` | aucune incidence sur le moteur |

Critères de fin de rodage : 7 jours consécutifs, aucune anomalie dans le rapport, toutes les
épreuves réussies, cycles 4 h de moins de 60 s. Les simulations lancées au début du rodage
sont conservées (décision 39), sauf problème imposant une remise à zéro.

## Pannes réseau et DNS

Une coupure réseau ou DNS du serveur (« Temporary failure in name resolution » dans
`logs/engine.log`) est encaissée par le moteur : une ligne « Collecte impossible » par
minute, clôtures 4 h reportées jusqu'au retour des bougies, valeurs horaires toujours
écrites ; le rapport de santé les compte (« Réseau · Collecte REST »). Si elles se répètent,
fiabiliser le DNS du conteneur LXC (Proxmox : onglet DNS du conteneur) ou de Docker, par
exemple dans `docker-compose.yml` :

```yaml
x-common: &common
  dns: [1.1.1.1, 9.9.9.9]
```

Tester la résolution depuis le moteur :

```bash
docker compose exec engine python -c "import socket; print(socket.gethostbyname('api.bitvavo.com'))"
```

## 6. Mettre à jour

```bash
git pull
docker compose up -d --build
```

Le schéma de la base est migré automatiquement au démarrage.

## 7. Mode production : ombre

L'onglet « Production » du dashboard permet de choisir un scénario parmi les 5 meilleures simulations, puis de démarrer le mode ombre : le scénario tourne sur son propre portefeuille (300 € par défaut), ordres calculés au carnet réel mais **jamais envoyés**. L'ombre ne tourne que lorsque les simulations sont en marche.

« Arrêter l'ombre » met ses positions en pause, sans les vendre. Pour repartir de zéro, une fois l'ombre arrêtée : « Remettre l'ombre à zéro » efface son portefeuille (positions, ordres, historique), sans vente enregistrée ; le scénario choisi est conservé.

Pour afficher le solde réel du compte (facultatif) :

1. Sur Bitvavo : *Paramètres → API → Créer une clé*, cocher **uniquement « Consulter »** (ni « Trader », ni « Retirer ») ; restreindre si possible à l'IP publique du serveur.
2. Sur le serveur, dans `~/Crypto-Trading-Sim/.env` :
   ```
   BITVAVO_API_KEY=...
   BITVAVO_API_SECRET=...
   ```
3. Recréer le moteur (un `docker compose restart` ne relit pas `.env`) :
   ```bash
   docker compose up -d engine
   ```

Le moteur lit le solde toutes les 5 minutes dès que la clé est présente et l'enregistre en base ; le dashboard l'affiche sans avoir accès à la clé. La clé ne quitte jamais le `.env` (ni base, ni journaux, ni dashboard).

## 8. Mode production : ordres réels

**Attention : ce mode envoie de vrais ordres sur votre compte Bitvavo.** Laissez d'abord tourner l'ombre quelques jours (section 7).

1. Sur Bitvavo, remplacez la clé « Consulter » par une clé aux droits **« Consulter » et « Trader »**, jamais « Retirer » ; restreignez-la à l'IP publique du serveur.
2. Approvisionnez le compte en euros (au moins le capital engagé).
3. Dans `~/Crypto-Trading-Sim/.env` :
   ```
   BITVAVO_API_KEY=...
   BITVAVO_API_SECRET=...
   PRODUCTION_LIVE=1
   PRODUCTION_MAX_CAPITAL=300
   ```
4. Recréer le moteur (le journal affiche « Mode réel déverrouillé ») :
   ```bash
   docker compose up -d engine
   ```
5. Onglet « Production » : le scénario choisi, simulations en marche, saisir « ORDRES RÉELS » puis « Démarrer les ordres réels ». L'ombre tourne en parallèle pour comparer.

Garde-fous : plafond de capital engagé (`PRODUCTION_MAX_CAPITAL`) ; perte de 10 % dans la journée → achats bloqués jusqu'au lendemain ; en Hold, plus d'achat mais stops et take profit toujours gérés tant que le moteur tourne. **Si le moteur est arrêté, les stops ne sont plus surveillés** (ils ne sont pas posés chez Bitvavo).

Pour arrêter :
- « Arrêter le réel » : plus d'achat ; les positions restent gérées jusqu'à leur clôture, puis le mode s'éteint ;
- « Arrêt d'urgence » : annule les ordres et vend au marché toutes les positions du moteur (vos autres cryptos ne sont pas touchées), puis coupe le mode ;
- pour reverrouiller : `PRODUCTION_LIVE=0` dans `.env`, puis `docker compose up -d engine`.
