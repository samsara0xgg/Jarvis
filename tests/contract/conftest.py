"""The contract level (ADR 0325): the messages the brain and its clients exchange, as files."""

from __future__ import annotations

import pytest

from tests.support.contracts import Contracts


@pytest.fixture
def contracts(request: pytest.FixtureRequest) -> Contracts:
    """The files of ``contracts/``; with ``--update-contracts`` they are rewritten, not compared."""
    return Contracts(update=bool(request.config.getoption("--update-contracts")))
