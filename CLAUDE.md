# Simulateur Monte-Carlo de trading crypto (Bitvavo, paper trading)

## Ce qu'est ce projet

Un moteur Python qui fait tourner en parallèle 300 simulations de stratégies de trading crypto en paper trading live, sur des données de marché réelles Bitvavo, avec un dashboard de suivi. Le but est de comparer des configurations (indicateurs, stops, règles de portefeuille) et d'identifier les variables qui influencent le plus le P&L.

La spécification complète est dans `docs/SPEC.md`. Lis-la entièrement avant d'écrire du code. La maquette validée du dashboard est dans `docs/maquette/` (voir `docs/maquette/LISEZMOI.md`). Déploiement et rodage : `docs/DEPLOIEMENT.md`.

## Règles absolues

- **Aucun ordre réel.** Phase 1 = paper trading uniquement. Le code ne doit contenir aucun appel aux endpoints d'ordre de Bitvavo tant que l'utilisateur ne l'a pas explicitement demandé. Seul le `PaperBroker` est implémenté. Exception validée (mode production, palier 2) : la lecture seule du solde (`GET /balance`, `mcs/bitvavo/account.py`) ; les ordres réels (palier 3) attendent une validation explicite.
- **Secrets.** La clé et le secret API Bitvavo vivent uniquement dans `.env` (non versionné, `.gitignore`). Jamais dans le code, la base SQLite, les logs ou le dashboard. Les données publiques (bougies, carnets, ticker) ne nécessitent pas de clé.
- **Argent et prix en `Decimal`**, jamais en `float`, dans le moteur (stratégie, broker, frais, P&L). Les `float` sont tolérés seulement pour le calcul vectorisé des indicateurs et l'affichage.
- **Bougies clôturées uniquement** pour les signaux (4 h) et le filtre de fond (1 j). Aucune fuite d'information future dans le backtest d'optimisation.
- **Peu de fichiers générés** : une seule base SQLite (mode WAL) pour tout l'état, pas de fichier par simulation.

## Conventions

- Python 3.11+, typage complet, `ruff` + `pytest`.
- Code et identifiants en anglais, interface du dashboard et messages utilisateur en français.
- Formatage des nombres à l'affichage en `fr-FR` ; prix < 0,001 € en notation à indice (0,0₅9512 €).
- Toute hypothèse non couverte par `docs/SPEC.md` : poser la question à l'utilisateur plutôt que trancher seul (voir la section « Décisions ouvertes » de la spec).

## Commandes (à compléter au fil du projet)

```bash
pip install -e ".[dev]"
pytest                                   # MCS_LIVE_TESTS=1 pour les tests contre l'API réelle
ruff check . && ruff format --check . && mypy mcs dashboard
python -m mcs.collector markets          # caractéristiques des marchés, historique, volume 24 h
python -m mcs.collector backfill         # historique des bougies (--markets, --intervals, --minute-days)
python -m mcs.collector live             # collecte continue (REST + WebSocket)
python -m mcs.sampling generate         # plan Monte-Carlo (--seed N, --force)
python -m mcs.optimizer run --workers 8  # optimisation 70/30 des jumelles optimisées
python -m mcs.engine                     # moteur live (collecte + 300 simulations + commandes)
streamlit run dashboard/app.py
python -m mcs.health --hours 72          # rapport de santé (rodage)
python -m mcs.control status             # pilotage sans dashboard (start, stop, resume, reset --yes…)
```
