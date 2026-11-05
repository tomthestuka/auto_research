"""Z1e — the launch check that sends every configured member one tiny image and one tiny PDF through
the real `ModelHandle.call`, with the vendor network faked at its client."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import yaml


class _Vendor:
    """The Responses client a vendor would be: answers, or refuses a request carrying a file for
    one model, the way a 400 would surface."""

    requests: list[dict] = []
    refuse_files_for: str | None = None

    def __init__(self, *a, **k):
        pass

    @property
    def responses(self):
        return self

    async def create(self, **request):
        _Vendor.requests.append(request)
        carries_file = "input_file" in json.dumps(request.get("input"), default=str)
        if request["model"] == _Vendor.refuse_files_for and carries_file:
            raise RuntimeError("Error code: 400 - this model does not accept file inputs")
        return SimpleNamespace(
            output=[SimpleNamespace(type="message", role="assistant",
                                    content=[SimpleNamespace(type="output_text", text="OK")])],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


def _configure(tmp_path, monkeypatch, reviewer_model: str, coder: bool = False):
    """Every required role on the stand-in `fake-model`, the reviewer on `reviewer_model`; with
    ``coder``, the coder is a harness member on a model of its own."""
    import openai

    from auto_research.models.registry import REQUIRED_ROLES

    roles = {role: {"members": [{"name": f"{role}_member", "provider": "openai",
                                 "model": "fake-model", "parameters": {}}]}
             for role in REQUIRED_ROLES}
    roles["reviewer"]["members"][0]["model"] = reviewer_model
    if coder:
        roles["coder"]["members"][0].update(model="fake-lead", harness="codex")
    config = tmp_path / "cfg"
    config.mkdir()
    (config / "models.yaml").write_text(yaml.safe_dump({"roles": roles}), encoding="utf-8")
    monkeypatch.setenv("AUTO_RESEARCH_CONFIG_DIR", str(config))
    monkeypatch.setattr(openai, "AsyncOpenAI", _Vendor)
    _Vendor.requests = []


def test_every_distinct_member_answers_one_image_and_one_pdf(tmp_path, monkeypatch):
    from auto_research.models.media_probe import check_member_media
    from auto_research.preflight import build_default_preflight

    _configure(tmp_path, monkeypatch, reviewer_model="fake")
    _Vendor.refuse_files_for = None
    asyncio.run(check_member_media())
    # Two distinct provider/model pairs (every other role shares `fake-model`), two media each.
    assert sorted(r["model"] for r in _Vendor.requests) == ["fake", "fake", "fake-model", "fake-model"]
    carried = [json.dumps(r["input"], default=str) for r in _Vendor.requests]
    assert sum("input_image" in c for c in carried) == 2 and sum("input_file" in c for c in carried) == 2
    names = build_default_preflight().names()
    assert names.index("member_capabilities") < names.index("member_media") == len(names) - 1


def test_a_member_whose_vendor_refuses_a_medium_fails_the_check_naming_it(tmp_path, monkeypatch):
    from auto_research.models.media_probe import check_member_media

    _configure(tmp_path, monkeypatch, reviewer_model="fake")
    _Vendor.refuse_files_for = "fake"
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(check_member_media())
    message = str(caught.value)
    assert "openai/fake for reviewer_member (role reviewer) with a PDF" in message
    assert "this model does not accept file inputs" in message
    assert "with an image" not in message and "fake-model" not in message  # only the refusal is named


def test_a_harness_member_is_not_probed_and_shared_pairs_name_every_member(tmp_path, monkeypatch):
    """Review findings: the coder is driven by its harness, never a chat call; a refusal on a pair
    several roles share names all of them."""
    from auto_research.models.media_probe import check_member_media

    _configure(tmp_path, monkeypatch, reviewer_model="fake", coder=True)
    _Vendor.refuse_files_for = "fake-model"
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(check_member_media())
    assert "fake-lead" not in {r["model"] for r in _Vendor.requests}
    message = str(caught.value)
    assert "openai/fake-model for " in message and "analyst_lead_member (role analyst_lead)" in message
    assert "coder_member" not in message


def test_a_vendor_that_does_not_answer_in_time_is_named(tmp_path, monkeypatch):
    from auto_research.models import media_probe

    _configure(tmp_path, monkeypatch, reviewer_model="fake")
    _Vendor.refuse_files_for = None

    async def slow(self, **request):
        await asyncio.sleep(5)

    monkeypatch.setattr(_Vendor, "create", slow)
    monkeypatch.setattr(media_probe, "PROBE_TIMEOUT_S", 0.2)
    with pytest.raises(RuntimeError, match=r"no answer within 0\.2 s, retries included"):
        asyncio.run(media_probe.check_member_media())


def test_a_rate_limit_storm_names_the_last_error_it_waited_out(tmp_path, monkeypatch):
    """Z1f: a bound expiring mid-backoff keeps the vendor's own words in the refusal."""
    from auto_research.models import media_probe

    _configure(tmp_path, monkeypatch, reviewer_model="fake")

    class _RateLimited(Exception):
        status_code = 429

    async def storm(self, **request):
        raise _RateLimited("Error code: 429 - rate limit reached for requests")

    monkeypatch.setattr(_Vendor, "create", storm)
    monkeypatch.setattr(media_probe, "PROBE_TIMEOUT_S", 0.5)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(media_probe.check_member_media())
    message = str(caught.value)
    assert "no answer within 0.5 s, retries included; the last error retried: _RateLimited: " in message
    assert "rate limit reached for requests" in message


def test_the_setup_smoke_test_meters_its_preflight_like_a_run(monkeypatch, capsys):
    """Z1f: the install-time preflight ends with the paid probe, so its spend lands on a ledger."""
    from auto_research import preflight, setup
    from auto_research.observability.costs import current_cost_meter

    seen = {}

    async def preflight_stand_in(*, human_handler=None, monitor=None):
        seen.update(meter=current_cost_meter(), monitor=monitor)
        await monitor.record("run", "preflight.summary", level=3, summary="stand-in", data={})

    monkeypatch.setattr(preflight, "run_preflight", preflight_stand_in)
    assert setup.page_preflight() is True
    assert seen["meter"] is not None and seen["meter"].monitor is seen["monitor"]
    ledger = seen["monitor"].jsonl_path
    from auto_research.sandbox.workspace import ledger_dir

    assert ledger.is_file() and ledger.parent == ledger_dir(seen["monitor"].run_id)
    out = capsys.readouterr().out
    assert "preflight spend: $0.0000" in out and str(ledger) in out


def test_an_unwritable_runtime_dir_is_reported_with_the_checklist(tmp_path, monkeypatch, capsys):
    """Z1g: the setup ledger that cannot be written is an issue in the report, not a crash."""
    import os

    from auto_research import preflight, setup

    async def preflight_stand_in(*, human_handler=None, monitor=None):
        return None

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    runtime.chmod(0o500)  # readable, not writable: the ledgers folder cannot be made
    monkeypatch.setenv("AUTO_RESEARCH_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(preflight, "run_preflight", preflight_stand_in)
    try:
        if os.access(runtime, os.W_OK):
            pytest.skip("this user can write a read-only folder (root)")
        assert setup.page_preflight() is False
    finally:
        runtime.chmod(0o700)
    out = capsys.readouterr().out
    assert "preflight reported issues:" in out and "[setup_ledger] the setup ledger could not be written" in out
    assert "Permission denied" in out and "(ledger not written)" in out
