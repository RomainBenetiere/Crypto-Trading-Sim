"""Garde-fous : aucun endpoint d'ordre hors du client dédié, aucun secret dans le code.

Exceptions validées par l'utilisateur (SPEC §15) : la lecture du solde, `GET /balance`, dans
`mcs/bitvavo/account.py` (palier 2), et les ordres réels dans `mcs/bitvavo/trading.py`
(palier 3), utilisé seulement par `mcs/broker/live.py`. Aucun retrait n'est codé nulle part.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [p for d in ("mcs", "dashboard") for p in (ROOT / d).rglob("*.py")]

# Endpoints privés d'ordre / de compte de l'API Bitvavo et méthodes du SDK officiel.
FORBIDDEN = re.compile(
    r"""["'][^"']*/(order|orders|ordersOpen|cancelOrders|account|balance|withdrawal|deposit)\b"""
    r"""|placeOrder|cancelOrder|updateOrder"""
    r"""|(client|httpx|session|requests)\.(post|put|patch|delete)\(""",
)
WITHDRAWAL = re.compile(r"withdraw|/deposit", re.IGNORECASE)


def test_sources_found() -> None:
    assert len(SOURCES) > 5


ACCOUNT = ROOT / "mcs" / "bitvavo" / "account.py"
TRADING = ROOT / "mcs" / "bitvavo" / "trading.py"
BALANCE_LINE = 'BALANCE_PATH = "/balance"'
ORDER_LINE = 'ORDER_PATH = "/order"'


def _allowed(path: Path, line: str) -> bool:
    if path == ACCOUNT:
        return line == BALANCE_LINE
    if path == TRADING:
        return line in (BALANCE_LINE, ORDER_LINE)
    return False


def test_no_order_or_private_endpoint() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for path in SOURCES
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line) and not _allowed(path, line.strip())
    ]
    assert offenders == []


def test_no_withdrawal_anywhere() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{n}"
        for path in SOURCES
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if WITHDRAWAL.search(line) and "Retirer" not in line and "retrait" not in line.lower()
    ]
    assert offenders == []


def test_account_client_only_reads_balance() -> None:
    source = ACCOUNT.read_text(encoding="utf-8")
    assert source.count("BALANCE_PATH") == 3  # définition, signature, requête
    assert "self._client.get(BALANCE_PATH" in source
    for verb in ("POST", "PUT", "PATCH", "DELETE", ".post(", ".put(", ".patch(", ".delete(",
                 ".request(", ".send("):  # fmt: skip
        assert verb not in source, verb


def test_trading_client_used_only_by_live_broker_and_engine() -> None:
    users = {
        path.relative_to(ROOT).as_posix()
        for path in SOURCES
        if path != TRADING
        and re.search(r"from mcs\.bitvavo\.trading import|mcs\.bitvavo\.trading", path.read_text())
    }
    assert users <= {"mcs/broker/live.py", "mcs/live/real.py", "mcs/engine.py"}


def test_dashboard_never_sends_orders() -> None:
    for path in (ROOT / "dashboard").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in ("BitvavoTrading", "BitvavoBroker", "LiveRunner", "BitvavoAccount"):
            assert name not in source, f"{path.name} : {name}"


def test_env_not_versioned() -> None:
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored


def test_guard_catches_order_paths() -> None:
    assert FORBIDDEN.search('self._get("/order", params)')
    assert FORBIDDEN.search("f'/{market}/orders?x'")
    assert not FORBIDDEN.search('RESET_TABLES = ("orders", "positions")')
