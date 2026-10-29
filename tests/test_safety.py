"""Garde-fous : aucun endpoint d'ordre, aucun secret dans le code. Seule exception (mode
production, palier 2) : la lecture du solde, `GET /balance`, dans `mcs/bitvavo/account.py`."""

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


def test_sources_found() -> None:
    assert len(SOURCES) > 5


ACCOUNT = ROOT / "mcs" / "bitvavo" / "account.py"
BALANCE_LINE = 'BALANCE_PATH = "/balance"'


def test_no_order_or_private_endpoint() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for path in SOURCES
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line) and not (path == ACCOUNT and line.strip() == BALANCE_LINE)
    ]
    assert offenders == []


def test_account_client_only_reads_balance() -> None:
    source = ACCOUNT.read_text(encoding="utf-8")
    assert source.count("BALANCE_PATH") == 3  # définition, signature, requête
    assert "self._client.get(BALANCE_PATH" in source
    for verb in ("POST", "PUT", "PATCH", "DELETE", ".post(", ".put(", ".patch(", ".delete(",
                 ".request(", ".send("):  # fmt: skip
        assert verb not in source, verb


def test_env_not_versioned() -> None:
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored


def test_guard_catches_order_paths() -> None:
    assert FORBIDDEN.search('self._get("/order", params)')
    assert FORBIDDEN.search("f'/{market}/orders?x'")
    assert not FORBIDDEN.search('RESET_TABLES = ("orders", "positions")')
