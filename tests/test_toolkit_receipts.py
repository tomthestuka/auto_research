"""A toolkit call's receipts name the toolkit checkout and commit it ran against (no LLM).

Drives the real `SandboxToolProvider` (a worker's call); only the tool process is a fake.
"""

import asyncio
import subprocess

from auto_research.agents.react import SandboxToolProvider
from auto_research.provenance import ReceiptStore
from tests.test_sandbox import _FakeRegistry, _FakeSandbox

TOOL = "research_toolkit.tk.fit"


def _toolkit(tmp_path):
    root = tmp_path / "toolkit"
    root.mkdir()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return root, head


def _call(tmp_path, store, args, toolkit_root=None, kind=TOOL):
    registry = _FakeRegistry(known={TOOL, "mcp.builtin.bash_exec"}, mcp={"mcp.builtin.bash_exec"},
                             schema={"properties": {"subject": {"kind": "setting"}}})
    registry._root = toolkit_root
    provider = SandboxToolProvider(registry, _FakeSandbox(tool_fn=lambda m, f, a: {"exit_code": 0, "stdout": "ok"}),
                                   cwd=tmp_path, receipts=store, receipt_source="analyst_worker_0")
    asyncio.run(provider.execute(kind, args))
    return store.receipts(kind="result")[-1]


def test_a_toolkit_receipt_names_its_root_and_commit(tmp_path):
    root, head = _toolkit(tmp_path)
    store = ReceiptStore(tmp_path / "receipts")
    receipt = _call(tmp_path, store, {"subject": "s1"}, toolkit_root=root)
    assert (receipt.toolkit_root, receipt.toolkit_commit) == (root, head)
    unknown = _call(tmp_path, store, {"subject": "s1"}, toolkit_root=None)
    assert (unknown.toolkit_root, unknown.toolkit_commit) == (None, None)
    shell = _call(tmp_path, store, {"command": "true"}, toolkit_root=root, kind="mcp.builtin.bash_exec")
    assert shell.toolkit_root is None  # a harness call names no toolkit tool


