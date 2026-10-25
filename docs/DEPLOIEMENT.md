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
l'optimisation. Moteur arrêté des deux côtés, copier le fichier :

```bash
scp utilisateur@mac:/Volumes/home/Github/Crypto-Trading-Sim/data/mcs.sqlite data/mcs.sqlite
sudo chown 1000:1000 data/mcs.sqlite
```

Le moteur passe la base en WAL au premier démarrage. Ne plus utiliser la copie du NAS ensuite.

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

## 6. Mettre à jour

```bash
git pull
docker compose up -d --build
```

Le schéma de la base est migré automatiquement au démarrage.
