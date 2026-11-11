"""Benchmark tests start and end with the toolkit contract unbound (``toolkit_contract.reset``), as
``tests/conftest.py`` does for the suite: the process binds it on first use."""

import pytest

#: PARKED (human ruling 2026-11-10, Phase 3 step 0b): the benchmarks still build briefs with the retired
#: observation fields and read the deleted ``config/observations.yaml``; their briefs move to store records
#: when the human next reruns them (a new baseline, not comparable with the published numbers). Until then
#: their tests are not collected.
collect_ignore_glob = ["*/tests/*"]


@pytest.fixture(autouse=True)
def _toolkit_contract_unbound():
    from auto_research import toolkit_contract

    toolkit_contract.reset()
    yield
    toolkit_contract.reset()
