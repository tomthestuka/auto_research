"""One paged read for every role (no LLM): the worker's read_file through its real provider."""

import asyncio
import os

import pytest

from auto_research.agents.react import (
    _MAX_LINE_CHARS,
    _MAX_WINDOW_CHARS,
    SandboxToolProvider,
)
from auto_research.provenance import ReceiptStore
from auto_research.provenance.store import is_evidence
from tests.test_sandbox import _FakeRegistry, _FakeSandbox


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from auto_research.sandbox import fs_guard

    allowed = tmp_path / "allowed"
    (allowed / "w0").mkdir(parents=True)
    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [allowed.resolve()])
    store = ReceiptStore(tmp_path / "receipts")
    provider = SandboxToolProvider(_FakeRegistry(known=set(), mcp=set()), _FakeSandbox(), cwd=allowed / "w0",
                                   receipts=store, receipt_source="analyst_worker_0")
    return provider, allowed, store


def _read(provider, **args):
    return asyncio.run(provider.execute("read_file", args))


def test_a_worker_pages_a_long_file_to_its_last_line(worker):
    provider, allowed, store = worker
    (allowed / "w0" / "long.txt").write_text("".join(f"line {i}\n" for i in range(1, 2501)))
    seen, offset = [], 1
    while True:
        out = _read(provider, path="long.txt", offset=offset)  # relative: the worker's folder
        assert not out.is_error
        seen += [line for line in out.text.splitlines() if "\tline " in line]
        if "offset=" not in out.text:
            break
        offset = int(out.text.rsplit("offset=", 1)[1].split()[0])
    assert seen[-1].endswith("line 2500") and len(seen) == 2500
    receipt = store.receipts()[0]
    assert receipt.produced_by == "read_file" and not is_evidence(receipt)


def test_a_huge_line_is_cut_and_marked_and_a_read_stays_bounded(worker):
    provider, allowed, _ = worker
    (allowed / "w0" / "one.jsonl").write_text(("y" * 50_000_000) + "\n" + ("z" * 10) + "\n")
    out = _read(provider, path=str(allowed / "w0" / "one.jsonl"))
    assert len(out.text) <= _MAX_WINDOW_CHARS + 300
    assert f"… [+{50_000_000 - _MAX_LINE_CHARS:,} chars]" in out.text and "zzzzzzzzzz" in out.text


@pytest.mark.parametrize("args, first, error", [
    ({"offset": -5}, "1\ta", False), ({"offset": 10**9}, "3\tc", False), ({"limit": 0}, "1\ta", False),
    ({"offset": None}, "1\ta", False), ({"offset": "1, "}, "invalid argument", True),
    ({"offset": float("inf")}, "invalid argument", True), ({"limit": 1e400}, "invalid argument", True),
])
def test_odd_offsets_are_clamped_or_answered(worker, args, first, error):
    provider, allowed, _ = worker
    (allowed / "w0" / "f.txt").write_text("a\nb\nc\n")
    out = _read(provider, path="f.txt", **args)
    assert out.is_error is error and first in out.text, out.text
    if args == {"limit": 0}:
        assert "offset=2" in out.text  # one line, then the next offset


def test_a_worker_cannot_read_outside_its_allow_list(worker, tmp_path):
    provider, allowed, _ = worker
    secret = tmp_path / "secret.txt"
    secret.write_text("key")
    os.symlink(secret, allowed / "w0" / "link.txt")
    (allowed / "w0" / "CLAUDE.md").write_text("governance")
    for path in (str(secret), "link.txt", "../../secret.txt", "CLAUDE.md", ""):
        out = _read(provider, path=path)
        assert out.is_error and "not permitted" in out.text, path


def test_a_file_with_no_newline_is_read_in_bounded_chunks(worker):
    provider, allowed, _ = worker
    (allowed / "w0" / "blob.txt").write_text("q" * 30_000_000)
    out = _read(provider, path="blob.txt")
    assert not out.is_error and f"… [+{30_000_000 - _MAX_LINE_CHARS:,} chars]" in out.text


def test_binary_files_and_fifos_are_answered_not_read(worker):
    provider, allowed, _ = worker
    (allowed / "w0" / "data.npy").write_bytes(b"\x93NUMPY\x00\x00" + bytes(5000))
    os.mkfifo(allowed / "w0" / "pipe")
    for path in ("data.npy", "pipe"):
        out = _read(provider, path=path)
        assert out.is_error and ("binary" in out.text or "regular file" in out.text), out.text


def test_with_the_jail_on_a_worker_is_sent_to_its_shell_for_tmp(worker, monkeypatch):
    from auto_research.sandbox import jail
    from auto_research.sandbox import fs_guard

    provider, allowed, _ = worker
    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [allowed.resolve(), __import__("pathlib").Path("/tmp").resolve()])
    provider._guard = fs_guard.PathGuard.from_config()
    monkeypatch.setattr(jail, "jail_enabled", lambda *a, **k: True)
    out = _read(provider, path="/tmp/anything.log")
    assert out.is_error and "bash_exec" in out.text


def test_web_fetch_answers_a_malformed_offset():
    from auto_research.agents.react import execute_web_fetch

    class _Web:
        async def fetch(self, url, depth=None):
            return "a\nb\n"

    out = asyncio.run(execute_web_fetch(_Web(), {"url": "https://x.org", "offset": "2, "}))
    assert out.is_error and "invalid argument" in out.text


def test_past_the_count_ahead_cap_the_footer_says_more_lines_follow(worker, monkeypatch):
    import auto_research.agents.react as react

    provider, allowed, _ = worker
    monkeypatch.setattr(react, "_COUNT_AHEAD_CHARS", 50)
    (allowed / "w0" / "empty_lines.txt").write_text("x\n" + "\n" * 500)
    out = _read(provider, path="empty_lines.txt", limit=1)
    assert "more lines follow" in out.text and "offset=2" in out.text


def test_the_tmp_rule_with_the_jail_off_and_through_links(worker, monkeypatch, tmp_path):
    from pathlib import Path

    from auto_research.sandbox import fs_guard, jail

    provider, allowed, _ = worker
    monkeypatch.setattr(fs_guard, "load_fs_allowlist", lambda config=None: [allowed.resolve(), Path("/tmp").resolve()])
    provider._guard = fs_guard.PathGuard.from_config()
    outside = Path("/tmp") / f"ar-probe-{tmp_path.name}.log"
    outside.write_text("host tmp\n")
    try:
        (allowed / "w0" / "to_tmp.log").symlink_to(outside)
        monkeypatch.setattr(jail, "jail_enabled", lambda *a, **k: True)
        for path in (str(outside), "to_tmp.log", "/tmp"):
            out = _read(provider, path=path)
            assert out.is_error and "bash_exec" in out.text, path
        monkeypatch.setattr(jail, "jail_enabled", lambda *a, **k: False)
        assert "host tmp" in _read(provider, path=str(outside)).text
    finally:
        outside.unlink()


def test_a_nul_in_a_read_only_roles_path_is_answered(tmp_path):
    from auto_research.agents.react import ReadOnlyToolProvider

    out = asyncio.run(ReadOnlyToolProvider(roots=[tmp_path]).execute("read_file", {"path": "/x\x00y"}))
    assert out.is_error and "not permitted" in out.text
