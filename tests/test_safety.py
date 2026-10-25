"""Garde-fous de la phase 1 : aucun endpoint d'ordre, aucun secret dans le code."""

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


def test_no_order_or_private_endpoint() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}:{n}: {line.strip()}"
        for path in SOURCES
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if FORBIDDEN.search(line)
    ]
    assert offenders == []


def test_env_not_versioned() -> None:
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored


def test_guard_catches_order_paths() -> None:
    assert FORBIDDEN.search('self._get("/order", params)')
    assert FORBIDDEN.search("f'/{market}/orders?x'")
    assert not FORBIDDEN.search('RESET_TABLES = ("orders", "positions")')
