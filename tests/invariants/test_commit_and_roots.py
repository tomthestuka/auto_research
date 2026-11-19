"""Freezes CLAUDE.md §9.3.6 (commit ⇔ gate ∧ decorrelated review, one tool per commit, on a branch)
and §9.3.7 (no write outside declared roots) over today's code.

The commit cells run the lead's real coding pipeline on the fixture toolkit (``open_run(pipeline=True)``):
the real gate in the jail, the real review hook, ``approve_edit`` and the commit receipt; faked are
only the author process and the co-lead's and compactor's provider networks. The decorrelation cells
drive the check through the launch's registered preflight item and through a chain whose co-lead
group lists the author's vendor first. The root cells run each role's whole turn and the worker's
jailed REPL and shell against whole-tree snapshots (the repository and the analysis environment) and
against writability probes that write nothing. ``tests/test_env_build.py`` covers the human's conda
environments role-independently (no driver path writes them).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from tests.invariants import drivers as d
from tests.invariants.drivers import (
    NEW_TOOL, ROLES, brief_step, coder_edit, open_run, review_script, run_chain, submit_step,
)


def _git(repo: Path, *args: str) -> str:
    from tests.fixtures.toolkit import git

    return git(repo, *args)


@pytest.mark.parametrize(("passing", "approved"), [
    pytest.param(True, False, id="gate-passes-colead-rejects"),
    pytest.param(False, None, id="gate-fails"),
])
def test_no_commit_when_the_gate_fails_or_the_colead_rejects(passing, approved, tmp_path):
    # §9.3.6 commit ⇔ gate ∧ review; a refused change leaves a clean tree and its patch saved
    chain = asyncio.run(run_chain(tmp_path, passing=passing, approved=approved))
    lead, run = chain.lead, chain.run
    assert lead.error is None and lead.filed, [(r.name, r.text) for r in lead.tool_results]
    assert chain.status == "" and chain.log == []
    assert lead.commits() == [] and lead.receipts.receipts(kind="commit") == []
    # The refused work is saved as a patch outside the toolkit, naming the tool it tried to add.
    (reset,) = lead.tree_resets()
    patch = Path(reset["patch"])
    assert patch.is_file() and NEW_TOOL in patch.read_text(encoding="utf-8")
    assert chain.root.resolve() not in patch.resolve().parents
    # The gate failing means the co-lead is never called (an unscripted call raises ScriptError).
    assert len(run.colead_calls()) == (2 if passing else 0)
    submitted = lead.results_named("submit_for_review")[0]
    assert (d.CI_FAILED in submitted.text) == (not passing)
    assert (d.REVIEW_REJECTED in submitted.text) == passing


def test_a_commit_exists_when_the_gate_passes_and_the_colead_approves(approved_chain):
    # §9.3.6 commit ⇔ gate ∧ review: the accepted counterpart of the refusals above
    chain = approved_chain
    lead, run = chain.lead, chain.run
    assert lead.error is None and lead.filed, [(r.name, r.text) for r in lead.tool_results]
    assert chain.status == ""
    assert len(chain.log) == 1 and chain.log[0].endswith(f"Add {NEW_TOOL}")
    assert len(lead.commits()) == 1 and len(lead.receipts.receipts(kind="commit")) == 1
    assert len(run.colead_calls()) == 2  # the decorrelated co-lead reviewed it
    submitted = lead.results_named("submit_for_review")[0]
    assert d.CI_FAILED not in submitted.text and d.REVIEW_REJECTED not in submitted.text


def _added_lines(text: str, before: str) -> list[str]:
    """The stripped lines of ``text`` a change added to ``before``."""
    old = {line.strip() for line in before.splitlines()}
    return [line.strip() for line in text.splitlines() if line.strip() and line.strip() not in old]


def _diffs_sent(calls: list) -> str:
    """The diff the co-lead read on every call: its task arrives as one JSON message holding ``diff``."""
    import json

    found = []
    for sent in calls:
        for message in sent.messages:
            content = getattr(message, "content", None)
            if getattr(message, "role", None) == "user" and isinstance(content, str) and content.startswith("{"):
                found.append(json.loads(content)["diff"])
    assert found, "the co-lead was sent no diff"
    return "\n".join(found)


def test_the_commit_holds_the_bytes_the_coder_wrote_and_the_colead_was_shown_them(approved_chain, tmp_path):
    # §9.3.6 commit ⇔ gate ∧ review over the SAME tree: the committed blobs are the coder's files,
    # and the co-lead was sent a line each file added
    chain = approved_chain
    commit_sha = chain.log[0].split()[0]
    seen_by_colead = _diffs_sent(chain.run.colead_calls())
    # What the coder wrote: its manifest edit is applied to the manifest as the branch held it.
    base = tmp_path / "base"
    (base / "demo_utils" / "skills").mkdir(parents=True)
    (base / "demo_utils" / "skills" / "__init__.py").write_text(
        _git(chain.root, "show", f"{chain.head}:demo_utils/skills/__init__.py"), encoding="utf-8")
    wrote = coder_edit(passing=True)("", base)
    committed = set(_git(chain.root, "show", "--format=", "--name-only", commit_sha).split())
    assert committed == set(wrote)
    for rel, text in wrote.items():
        assert _git(chain.root, "show", f"{commit_sha}:{rel}") == text, rel
        try:
            before = _git(chain.root, "show", f"{chain.head}:{rel}")
        except subprocess.CalledProcessError:
            before = ""  # a file the commit added
        added = _added_lines(text, before)
        assert added, rel
        distinctive = max(added, key=len)
        assert distinctive in seen_by_colead, (rel, distinctive)


def test_a_tree_changed_between_the_gate_and_the_review_is_not_committed(tmp_path):
    # §9.3.6 the commit holds exactly what the gate judged (the CI manifest check at ``approve_edit``)
    root_box: dict = {}

    def edit_after_reading(sent):
        lib = root_box["root"] / "demo_utils" / "lib" / "demo_totals.py"
        lib.write_text(lib.read_text(encoding="utf-8") + "\nSNEAKED_IN = True\n", encoding="utf-8")
        return "I read the diff and the gate report."

    async def go():
        async with open_run(tmp_path, pipeline=True, config={"tool_author_submit_cap": 1}) as run:
            root_box["root"] = run.toolkit_root
            worker = await run.turn("worker", run.file_script("worker", tool_request=NEW_TOOL))
            head = _git(run.toolkit_root, "rev-parse", "HEAD").strip()
            colead = review_script(True)
            colead[0] = edit_after_reading
            lead = await run.turn(
                "lead", [brief_step(), submit_step(), submit_step(), *run.file_script("lead")],
                handed=[worker], coder=[coder_edit(passing=True)], colead=colead,
                compactor=["The coder added the tool and its test."],
            )
            return run, lead, head

    run, lead, head = asyncio.run(go())
    assert lead.error is None and lead.filed
    assert _git(run.toolkit_root, "log", "--format=%H", f"{head}..HEAD").strip() == ""
    assert lead.receipts.receipts(kind="commit") == [] and lead.commits() == []
    assert _git(run.toolkit_root, "status", "--porcelain").strip() == ""
    first = lead.results_named("submit_for_review")[0]
    assert first.is_error and d.NOT_COMMITTED in first.text
    assert d.CHANGED_AFTER in first.text and "demo_totals.py" in first.text, first.text


def test_one_commit_touches_one_tools_files_on_the_authoring_branch_only(approved_chain):
    # §9.3.6 one tool per commit, on a branch; the runtime has no concept of master
    from tests.fixtures.toolkit import BRANCH

    chain = approved_chain
    commit_sha = chain.log[0].split()[0]
    files = _git(chain.root, "show", "--format=", "--name-only", commit_sha).split()
    # One tool: its code, its manifest entry and its own test, in one package.
    assert {Path(f).parts[0] for f in files} == {"demo_utils"}
    assert sorted(Path(f).name for f in files) == ["__init__.py", "demo_totals.py", "test_demo_totals.py"]
    # The commit is on the authoring branch; the trusted branch never moved.
    assert chain.branch == BRANCH
    assert chain.tips[BRANCH] == commit_sha
    assert chain.tips[d.TRUSTED_BRANCH] == chain.run.trusted_tip
    assert set(chain.tips) == {BRANCH, d.TRUSTED_BRANCH}
    # The commit receipt's record names the tool and that commit.
    (commit,) = chain.lead.receipts.receipts(kind="commit")
    record = Path(commit.path).read_text(encoding="utf-8")
    assert commit_sha in record and NEW_TOOL in record
    assert len(chain.log) == 1


# ── §9.3.6 decorrelation ─────────────────────────────────────────────────────────────────────


def test_a_co_lead_sharing_the_authors_provider_is_refused_by_the_launchs_preflight(tmp_path, monkeypatch):
    # §9.3.6 decorrelation: the launch's registered preflight item refuses a shared provider and
    # accepts a decorrelated one
    import yaml

    from auto_research import preflight
    from auto_research.config_paths import CONFIG_DIR_ENV
    from auto_research.models import registry

    def models(colead: str) -> None:
        def member(vendor, **extra):
            return {"name": f"{vendor}_m", "provider": vendor, "model": "fake", **extra}

        roles = {r: {"members": [member("anthropic")]} for r in sorted(registry.REQUIRED_ROLES)}
        roles["coder"] = {"members": [member("anthropic", harness="claude_sdk")]}
        roles["analyst_colead"] = {"members": [member(colead)]}
        roles["compactor"] = {"members": [member("google")]}
        (tmp_path / "models.yaml").write_text(yaml.safe_dump({"roles": roles}), encoding="utf-8")

    # The item as the default checklist registers it (the checklist's other items need a launch).
    registered: dict = {}
    real_register = preflight.Preflight.register

    def capture(self, name, check):
        registered[name] = check
        return real_register(self, name, check)

    monkeypatch.setattr(preflight.Preflight, "register", capture)
    preflight.build_default_preflight()
    check = registered["author_reviewer_decorrelation"]

    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    models("openai")
    check()  # decorrelated: accepted
    models("anthropic")
    with pytest.raises(ValueError, match="decorrelated"):
        check()


def test_the_review_is_served_by_the_decorrelated_member_when_the_authors_vendor_is_listed_first(tmp_path):
    # §9.3.6 decorrelation at run time: with the co-lead group [anthropic, openai] and an anthropic
    # author, production picks the openai member (the script serves only that one), and the link commits
    chain = asyncio.run(run_chain(tmp_path, colead_members=("anthropic", "openai")))
    assert chain.colead_members == ("anthropic", "openai")
    coder, reviewer = chain.providers
    assert coder == "anthropic" and chain.colead_members[0] == coder  # the first member shares the author's vendor
    assert reviewer == "openai" and len(chain.run.colead_calls()) == 2
    assert chain.lead.error is None and chain.lead.filed
    assert len(chain.log) == 1 and len(chain.lead.commits()) == 1


# ── §9.3.7 no write outside declared roots ──────────────────────────────────────────────────

#: A toolkit's own contract: readable by every unit that uses the toolkit (ruling 2026-11-18).
_TOOLKIT_CONTRACT = ("CLAUDE.md", "AGENTS.md")
#: This repository's governance files: in no unit's reach (§3, §4); the repository is mounted in no jail.
_REPO_GOVERNANCE = (d.REPO / "CLAUDE.md", d.REPO / "src" / "auto_research" / "DOCS.md")
#: Every tool a role may declare, known not to write outside the unit's own report and runtime
#: folders: the channel, the report tools, reads, searches and fetches. A tool declared beyond these
#: (and beyond the worker's backend and toolkit tools below) is new, and its reach is to be judged.
#: ``file_edit`` (a reviewer's or lead's edit of its one ``editable_file``) is not declared in a run
#: that sets none.
_NON_WRITERS = frozenset({
    "edit_report", "write_report", "turn_end", "message", "list_peers", "wait_for_reply", "view_receipt",
    "read_file", "list_dir", "read_paper", "text_search", "search_literature", "web_fetch", "web_search",
})
#: The worker's own backend tools (the jailed shell and its listing, the environment navigation); its
#: toolkit tools write only declared products into the call's own folder under the runtime dir.
_WORKER_BACKEND = frozenset({"mcp_builtin_bash_exec", "mcp_builtin_list_dir", "list_env", "use_env"})
_TOOLKIT_PREFIX = "research_toolkit_"


def _unknown_tools(role: str, tools: list[str]) -> list[str]:
    known = _NON_WRITERS | (_WORKER_BACKEND if role == "worker" else frozenset())
    return sorted(t for t in set(tools) if t not in known and not (role == "worker" and t.startswith(_TOOLKIT_PREFIX)))


@pytest.mark.parametrize("role", ROLES)
def test_a_whole_turn_of_each_role_writes_only_under_the_runtime_dir(role, tmp_path):
    # §9.3.7 a full real turn (the reviewer's render, the librarian's PDF download and page store, the
    # worker's REPL and a toolkit call that writes a product) changes nothing in the repository or the
    # analysis environment, and nothing in the run but under the runtime dir
    repo_before = d.tree_snapshot()

    async def go():
        async with open_run(tmp_path) as run:
            # The worker's REPL is asked what it may write, before the turn: the repository and the
            # analysis environment are read-only to it, its own working folder is not.
            probes, workdir = {}, run.workdir(role)  # asked while the run's data dir is the one in force
            if role == "worker":
                for path in (d.REPO, d.REPO / "src", Path(sys.prefix), run.toolkit_root, workdir):
                    probes[path] = await run.repl_writable(path)
            before = d.tree_snapshot(run.root)
            head = _git(run.toolkit_root, "rev-parse", "HEAD").strip()
            script = run.file_script(role)
            if role == "worker":
                table = lambda sent: (d.tool_named(sent, "write_band_table"), {"csv_path": str(run.product)})  # noqa: E731
                script = [table, *script]
            result = await run.turn(role, script)
            return run, result, head, before, d.tree_snapshot(run.root), probes, workdir

    run, result, head, before, after, probes, workdir = asyncio.run(go())
    assert result.filed and result.error is None
    assert d.tree_changes(repo_before, d.tree_snapshot()) == []
    runtime = f"{run.root / 'runtime'}/"
    changed = d.tree_changes(before, after)
    assert [c for c in changed if not c[2:].startswith(runtime)] == []
    assert any(c.startswith("+ ") and c[2:].startswith(runtime) for c in changed), "the turn wrote nothing at all"
    assert _git(run.toolkit_root, "rev-parse", "HEAD").strip() == head
    assert _git(run.toolkit_root, "status", "--porcelain").strip() == ""
    assert _unknown_tools(role, [t for sent in result.sent for t in sent.tools]) == [], result.sent[0].tools
    if role == "worker":
        (written,) = result.results_named(result.declared("write_band_table"))
        assert not written.is_error, written.text
        # The product lies in the call's own folder under the worker's working folder.
        assert list((workdir / "products" / "write_band_table").glob("*/band_table.csv"))
        assert probes.pop(workdir) is True  # the accepted counterpart
        assert not any(probes.values()), probes


def _repo_lines() -> list[str]:
    """A distinctive line of each of this repository's governance files, to look for in what a model saw."""
    return [max(path.read_text(encoding="utf-8").splitlines(), key=len) for path in _REPO_GOVERNANCE]


@pytest.mark.parametrize("role", ["reviewer", "lead", "worker"])
def test_this_repositorys_governance_files_are_in_no_roles_reach(role, tmp_path):
    # §3 / §4 / §9.3.7 this repository's CLAUDE.md and DOCS.md reach no role, by its file tool or (the
    # worker) its shell; the accepted counterpart reads the toolkit README
    async def go():
        async with open_run(tmp_path) as run:
            read = run.harness_call(role, "read_file")
            readme = run.toolkit_root / "README.md"
            steps = [read.step(path=str(path)) for path in _REPO_GOVERNANCE]
            if role == "worker":
                shell = run.harness_call("worker", "shell")
                steps += [shell.step(command=f"cat {path}") for path in _REPO_GOVERNANCE]
            result = await run.turn(role, [*steps, read.step(path=str(readme)), *run.file_script(role)])
            return result, readme.read_text(encoding="utf-8")

    result, readme = asyncio.run(go())
    *refused, ordinary = result.results_named("read_file")
    assert len(refused) == len(_REPO_GOVERNANCE) and all(r.is_error for r in refused)
    assert not ordinary.is_error and readme.splitlines()[0] in ordinary.text
    shown = "\n".join(s.text() for s in result.sent)
    assert not [line for line in _repo_lines() if line in shown]  # no governance text reached the model


def _contract_turn(role, tmp_path, via):
    async def go():
        async with open_run(tmp_path) as run:
            for name in _TOOLKIT_CONTRACT:
                (run.toolkit_root / name).write_text(f"# {name}\nSENTINEL-{name} is the toolkit's contract.\n",
                                                     encoding="utf-8")
            call = run.harness_call(role, via)
            arg = (lambda p: {"path": str(p)}) if via == "read_file" else (lambda p: {"command": f"cat {p}"})
            steps = [call.step(**arg(run.toolkit_root / name)) for name in _TOOLKIT_CONTRACT]
            result = await run.turn(role, [*steps, *run.file_script(role)])
            return result, call.tool

    return asyncio.run(go())


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "built in Phase 3 step 3: a toolkit's own CLAUDE.md/AGENTS.md is readable by every unit that uses it "
    "(ruling 2026-11-18); today PathGuard refuses governance basenames to read_file"))
@pytest.mark.parametrize("role", ["reviewer", "lead", "worker"])
def test_a_toolkits_own_contract_is_readable_by_read_file(role, tmp_path):
    # §3 a toolkit's contract is that toolkit's, readable by a unit that works on or uses it
    result, tool = _contract_turn(role, tmp_path, "read_file")
    read = result.results_named(tool)
    assert len(read) == len(_TOOLKIT_CONTRACT)
    for name, outcome in zip(_TOOLKIT_CONTRACT, read):
        assert not outcome.is_error and f"SENTINEL-{name}" in outcome.text, outcome.text


def test_a_toolkits_own_contract_is_readable_by_the_workers_shell(tmp_path):
    # §3 the same file through the worker's jailed shell
    result, tool = _contract_turn("worker", tmp_path, "shell")
    read = result.results_named(tool)
    assert len(read) == len(_TOOLKIT_CONTRACT)
    for name, outcome in zip(_TOOLKIT_CONTRACT, read):
        assert f"SENTINEL-{name}" in outcome.text, outcome.text


def test_the_worker_shell_may_write_neither_the_orchestrations_source_nor_the_toolkit(tmp_path):
    # §9.3.7 the worker's jailed shell asked, by a probe that writes nothing, what it may write:
    # auto_research's tree, the analysis environment and the toolkit root are read-only to it; its
    # own private area is the accepted counterpart
    async def go():
        async with open_run(tmp_path) as run:
            shell = run.harness_call("worker", "shell")
            root = run.toolkit_root
            paths = [d.REPO, d.REPO / "src" / "auto_research", d.REPO / "prompts", Path(sys.prefix),
                     root, root / "demo_utils" / "lib", root / "README.md"]
            steps = [shell.step(command=d.writability_probe(p)) for p in paths]
            steps.append(shell.step(command=d.writability_probe("/tmp")))
            head = _git(root, "rev-parse", "HEAD").strip()
            result = await run.turn("worker", [*steps, *run.file_script("worker")])
            return run, result, shell.tool, paths, head

    run, result, tool, paths, head = asyncio.run(go())
    answers = [d.probed_writable(r.text) for r in result.results_named(tool)]
    assert len(answers) == len(paths) + 1 and result.filed
    assert answers[-1] is True  # a probe that can say writable did, for the shell's own area
    assert dict(zip(map(str, paths), answers[:-1])) == {str(p): False for p in paths}
    assert _git(run.toolkit_root, "rev-parse", "HEAD").strip() == head
    assert _git(run.toolkit_root, "status", "--porcelain").strip() == ""


def test_the_toolkit_changes_only_through_the_pipelines_commit(approved_chain):
    # §9.3.7 the toolkit's head moved once, by the commit the pipeline's event names, and by no tool of a role
    chain = approved_chain
    events = chain.lead.commits()
    assert len(events) == 1 and len(chain.log) == 1
    assert chain.log[0].split()[0] == events[0]["commit_sha"]
    assert chain.status == ""


def test_the_human_conda_environments_are_covered_outside_the_driver_paths():
    # §9.3.7 human conda environments: no driver path writes them; tests/test_env_build.py covers it
    pytest.skip("covered role-independently by tests/test_env_build.py (no driver path writes the human's environments)")
