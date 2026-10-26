"""Rapport de santé (rodage) et commande de pilotage."""

import sqlite3
from pathlib import Path

import pytest

from mcs import control
from mcs.health import Level, check_accounts, check_logs, read_logs, report, run_checks
from mcs.live.state import Command, request_command
from tests.backtest_data import HOUR
from tests.test_dashboard import prepare, run_live


def write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_logs_and_log_checks(tmp_path: Path) -> None:
    write_log(
        tmp_path / "engine.log",
        [
            "2026-10-25 10:00:00,001 WARNING mcs.bitvavo.ws : WebSocket coupé (OSError), reconnexion dans 1 s.",  # noqa: E501
            "2026-10-25 10:00:02,001 INFO    mcs.bitvavo.ws : WebSocket reconnecté après 2.0 s de coupure.",  # noqa: E501
            "2026-10-25 10:05:00,001 INFO    mcs.broker.paper : Sim 3 : ordre buy X refusé (below_minimum : x).",  # noqa: E501
            "2026-10-25 10:06:00,001 ERROR   mcs.engine : Échec du cycle, nouvel essai à la minute suivante.",  # noqa: E501
            "2026-10-25 10:07:05,001 WARNING mcs.engine : Collecte impossible (ConnectError : [Errno -3] Temporary failure in name resolution), nouvel essai à la minute suivante.",  # noqa: E501
            "2026-10-25 10:08:05,001 WARNING mcs.engine : Clôture 4 h reportée : bougies non récupérées (collecte en échec).",  # noqa: E501
            "ligne sans horodatage",
        ],
    )
    write_log(tmp_path / "engine.log.1", ["2020-01-01 00:00:00,000 ERROR   mcs.engine : ancien"])
    lines = read_logs(tmp_path, since=0)
    assert len(lines) == 7 and lines[0].message == "ancien"
    recent = read_logs(tmp_path, since=lines[1].ts)
    checks = {c.name: c for c in check_logs(recent)}
    assert checks["WebSocket"].detail == "1 coupure(s), 1 reconnexion(s)"
    assert checks["Ordres refusés"].detail == "below_minimum 1"
    assert checks["Collecte REST"].level is Level.WARN
    assert (
        checks["Collecte REST"].detail.startswith("1 minute(s) en échec")
        and "1 clôture" in checks["Collecte REST"].detail
    )
    assert checks["Erreurs"].level is Level.FAIL and "mcs.engine 1" in checks["Erreurs"].detail


async def test_report_on_running_engine(conn: sqlite3.Connection, tmp_path: Path) -> None:
    prepare(conn)
    engine = await run_live(conn, bars=4)
    checks = run_checks(conn, tmp_path, hours=72, now=engine.clock() + 60_000)
    by_name = {c.name: c for c in checks}
    assert by_name["Signe de vie"].level is Level.OK
    assert by_name["Valeurs horaires"].level is Level.OK, by_name["Valeurs horaires"].detail
    assert by_name["Métriques"].level is Level.OK
    assert by_name["Comptes"].level is Level.OK, by_name["Comptes"].detail
    text = report(checks, 72)
    assert text.index("Moteur") < text.index("Données") < text.index("Simulations")

    # Le moteur s'arrête : plus de signe de vie, valeurs horaires manquantes.
    later = engine.clock() + 3 * HOUR
    by_name = {c.name: c for c in run_checks(conn, tmp_path, hours=72, now=later)}
    assert by_name["Signe de vie"].level is Level.FAIL
    assert by_name["Valeurs horaires"].level is Level.FAIL


async def test_accounts_check_detects_mismatch(conn: sqlite3.Connection) -> None:
    prepare(conn)
    engine = await run_live(conn, bars=6)
    assert check_accounts(conn, list(engine.sims)).level is Level.OK
    # Un achat rempli sans position correspondante : incohérence à signaler.
    conn.execute(
        """INSERT INTO orders (sim_id, market, order_type, side, status, price, quantity,
               notional, fee, reason, created_at) VALUES
           (1, 'AAA-EUR', 'market', 'buy', 'filled', '10', '2', '20', '0.05', 'signal', 1)"""
    )
    check = check_accounts(conn, list(engine.sims))
    assert check.level is Level.FAIL and "≠ position" in check.detail


def test_control_cli(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "test.sqlite"  # base de la fixture `conn`
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("SQLITE_JOURNAL_MODE", "WAL")
    monkeypatch.setenv("CRYPTO_BASKET", "BTC-EUR")
    control.main(["start"])
    with pytest.raises(SystemExit):
        control.main(["reset"])  # confirmation exigée
    control.main(["reset", "--yes"])
    assert [r[0] for r in conn.execute("SELECT command FROM commands ORDER BY id")] == [
        "start",
        "reset",
    ]
    request_command(conn, Command.STOP, 1)
    control.main(["status"])
    out = capsys.readouterr().out
    assert "Statut : Non démarré" in out and "stop → en attente du moteur" in out
