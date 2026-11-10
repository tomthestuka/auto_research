"""Benchmark tests start and end with the toolkit contract unbound (``toolkit_contract.reset``), as
``tests/conftest.py`` does for the suite: the process binds it on first use."""

import pytest


@pytest.fixture(autouse=True)
def _toolkit_contract_unbound():
    from auto_research import toolkit_contract

    toolkit_contract.reset()
    yield
    toolkit_contract.reset()
