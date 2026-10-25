"""Pilotage du moteur en ligne de commande (même effet que les boutons du dashboard).

python -m mcs.control status
python -m mcs.control optimize | start | stop | resume
python -m mcs.control reset --yes
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from mcs.config import load_settings
from mcs.live.state import Command, read_state, request_command
from mcs.storage.db import connect, now_ms

STATUS_LABELS = {"NOT_STARTED": "Non démarré", "RUNNING": "En cours", "HOLD": "Hold"}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m mcs.control",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("action", choices=["status", *(c.value for c in Command)])
    parser.add_argument("--yes", action="store_true", help="confirme la remise à zéro")
    args = parser.parse_args(argv)
    settings = load_settings()
    conn = connect(settings.db_path, settings.journal_mode)
    try:
        if args.action == "status":
            state = read_state(conn)
            last = conn.execute(
                "SELECT command, requested_at, processed_at, result FROM commands "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            print(f"Statut : {STATUS_LABELS[state.status.value]}")
            print(f"Optimisation : {state.optimization_status}")
            print(f"Panier : {', '.join(state.basket) or '—'}")
            if last:
                done = last["result"] if last["processed_at"] else "en attente du moteur"
                print(f"Dernière commande : {last['command']} → {done}")
            return
        command = Command(args.action)
        if command is Command.RESET and not args.yes:
            raise SystemExit(
                "Remise à zéro : positions, ordres et P&L live seront effacés (configurations "
                "conservées). Relancer avec --yes pour confirmer."
            )
        request_command(conn, command, now_ms())
        print(f"Commande « {command.value} » déposée : le moteur la traitera sous 2 secondes.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
