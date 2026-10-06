"""One trial's private world: its configuration, its toolkit, its data, its runtime directory.

A trial here is a whole research run, and a run resolves its own seats, paths and caps out of the
configuration directory it was launched against. So an arm is carried into a run not as an argument
but as a *configuration*, and this module is what writes one: a directory of four YAML files, a
toolkit checkout at the pinned base, a private observation database holding only what the question
needs, and an empty runtime directory for everything the run produces.

Nothing is shared between trials except the machine -- python, the science stack, the network. Two
trials cannot see each other's commits, each other's reduction products, each other's knowledge
base or each other's catalog, and neither can reach the live checkout or the master archive.

**The whole trial root is opaque.** Every directory is named from the run-id hash and nothing under
it carries a seat, a vendor or an arm. That is not decoration: a read-only tool provider renders its
permitted roots into the tool descriptions the model reads, and shell commands print their working
directory, so a path named for the experiment is prompt content.

**The configuration directory is deliberately outside the filesystem allow-list.** It is the one
file in the trial that names the whole arm at once, and no actor has any reason to read it. The
allow-list the trial is given covers its toolkit, its data and its runtime directory, so the guard
denies the configuration and -- with the kernel jail on -- it is not even mounted.

### Why the observation database is copied rather than linked, and filtered rather than copied whole

The master archive is 1.2 TB of irreplaceable data, and a hardlink clone shares inodes with it: any
in-place write in a trial reaches the original with no undo. That is not hypothetical for this
question, whose own prescription instructs the tree to edit FITS headers in place with a HEASoft
header editor and to decompress files. So the clone is a real copy (ruling 2026-09-01), and it is
confined to Level 2, which is what the question analyses.

The filter is **per file, not per directory**, and that is the load-bearing part. The master's
``event_l2`` directory for the May observation holds 1,023 files, of which six are the archive's
own event lists; the rest are products of earlier runs -- including ``*_src_pcube.fits``, which is
precisely the quantity this question exists to measure. A trial cloned naively would find the
answer already sitting on disk. So only files matching the archive's own event-list naming are
copied, alongside the auxiliary and housekeeping directories, which hold nothing derived.

The catalog is then rebuilt by the toolkit's own recovery path rather than by a hand-written
transform: it reconciles a catalog against what is actually on disk, dropping rows whose files are
absent and recomputing each observation's levels. Using it means the clone's catalog is consistent
by the same rules the health check applies at launch, instead of by this module's guess about
theirs. It runs only ever against a trial's private copy.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from benchmarks.science_bench.arms import (
    CODER,
    forbidden_in_path,
    CODER_BASE_URL,
    CODER_HARNESS,
    CODER_KEY_ENV,
    Arm,
    Seat,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The observations this question is posed on: Cyg X-1, May 2022 and the June ToO.
QUESTION_OBSIDS: tuple[str, ...] = ("01002901", "01250101")

#: The mission subdirectory those observations live in.
QUESTION_MISSION = "IXPE"

#: Subdirectories of an observation copied whole, because the archive owns everything in them.
#: ``event_l1`` is absent: this question is a Level-2 analysis, and Level 1 is three quarters of
#: the bytes. Its absence is a stated difference from a production run rather than a hidden one.
RAW_DIRECTORIES: tuple[str, ...] = ("auxil", "hk", "response")

#: Files copied from an observation's root, as opposed to from one of the directories above.
RAW_FILES: tuple[str, ...] = ("README",)

#: The event lists copied out of ``event_l2``, and nothing else there.
#:
#: Two kinds, and the line between them is **"is it a measurement"** rather than "did a human make
#: it" (ruling 2026-09-01):
#:
#: - the archive's own Level-2 lists, ``ixpe<obsid>_det<N>_evt2_v<NN>.fits[.gz]``;
#: - the instrument-background-rejected lists beside them, ``..._rej.fits``, produced by the
#:   group's own ``filter_inst_bkg_IXPE.sh`` preprocessing script rather than by any agent run.
#:
#: The second kind is load-bearing, which block 0 found the expensive way. ``get_all_file_list`` at
#: the pinned toolkit base globs ``event_l2/*evt2_v[0-9][0-9]_rej.fits`` **and nothing else**, so a
#: clone without them makes ``load_data`` raise ``No files found`` on a pristine observation --
#: which cost that run its entire first authoring cycle and would have cost every one of the 14
#: arms the same, turning part of the sweep into a measurement of glob-fixing.
#:
#: Everything genuinely derived stays out: polarization cubes, PHA spectra, time-segment cuts
#: (``..._rej_<obsid>s<NNN>src.fits``), CSVs and region files. The May observation's event
#: directory holds 1,023 files, of which these patterns match nine; among the 1,014 left behind are
#: the ``*_src_pcube.fits`` files that ARE the answer this question exists to compute.
RAW_EVENT_LIST = re.compile(
    r"^ixpe\d+_det[123]_evt2_v\d+(_rej)?\.fits(\.gz)?$"
)


@dataclass(frozen=True)
class TrialEnvironment:
    """Where one trial's private world lives, and how to launch a process into it."""

    run_id: str
    root: Path
    config_dir: Path
    toolkit_root: Path
    runtime_dir: Path
    papers_dir: Path
    obs_root: Path
    base_sha: str

    @property
    def run_dir(self) -> Path:
        """The run's own directory, which the batch addresses directly.

        It is derived from *this* trial's runtime directory rather than from the batch's own
        configuration, which is the reason the batch can watch a trial it is not configured like.
        """
        return self.runtime_dir / "runs" / self.run_id

    def process_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """The environment a trial process is launched with: the caller's, plus the seam."""
        from auto_research.config_paths import CONFIG_DIR_ENV

        env = dict(os.environ if base is None else base)
        env[CONFIG_DIR_ENV] = str(self.config_dir)
        return env


# ── The three configuration files a trial differs in ────────────────────────
#
# Pure dict-to-dict, so what a trial is configured with can be asserted without materialising
# anything. The fourth file, the gate's scan lists, is copied byte for byte: it is part of the
# instrument being measured, not part of the treatment.


def _member(seat: Seat, *, coder: bool = False) -> dict[str, Any]:
    """One models.yaml member from a seat.

    The member is named for its model, exactly as the live configuration names its own. A model
    knowing which model it is leaks nothing -- it knows anyway -- and the arm is the *combination*,
    which appears nowhere an actor can reach.
    """
    entry: dict[str, Any] = {
        "name": re.sub(r"[^a-z0-9]+", "_", seat.model.lower()).strip("_"),
        "provider": seat.provider,
        "model": seat.model,
        "vision": seat.vision,
        "documents": seat.documents,
    }
    price = seat.price()
    if price:
        entry["priceMTk"] = price
    parameters = seat.parameters()
    if parameters:
        entry["parameters"] = parameters
    if coder:
        entry["harness"] = CODER_HARNESS
        entry["base_url"] = CODER_BASE_URL
        entry["key_env"] = CODER_KEY_ENV
    return entry


def models_config(arm: Arm, source: dict[str, Any]) -> dict[str, Any]:
    """The trial's ``models.yaml``: the arm's five seats, every other role left alone.

    ``librarian_io`` and ``status_monitor`` are copied from the live configuration untouched. They
    are not on any axis, so varying them would add a factor nobody is measuring, and pinning them
    to something other than what production uses would make the trial a different pipeline.
    """
    roles = {name: group for name, group in (source.get("roles") or {}).items()}
    roles["reviewer"] = {"members": [_member(arm.reviewer)]}
    roles["analyst_lead"] = {"members": [_member(arm.lead)]}
    roles["analyst_worker"] = {"members": [_member(m) for m in arm.workers.members]}
    roles["analyst_colead"] = {"members": [_member(arm.colead)]}
    roles["coder"] = {"members": [_member(CODER, coder=True)]}
    return {"roles": roles}


def orchestration_config(
    source: dict[str, Any],
    *,
    toolkit_root: Path,
    runtime_dir: Path,
    papers_dir: Path,
    obs_root: Path,
) -> dict[str, Any]:
    """The trial's ``orchestration.yaml``: the live one with its four paths redirected.

    Every cap, flag and timeout is inherited, because those are the pipeline being measured. What
    changes is only where the trial reads and writes.

    The filesystem allow-list is rewritten alongside them, and forgetting that is the failure this
    function exists to prevent: the list names the old roots literally, so a trial pointed at
    private paths whose allow-list still named the shared ones would have every one of its own
    file reads denied by the guard, and -- with the kernel jail on -- its private data simply
    absent from the mount namespace.
    """
    config = json.loads(json.dumps(source))  # deep copy through plain data
    old = {
        str(Path(str(config.get(key, ""))).expanduser())
        for key in ("toolkit_root", "runtime_dir", "papers_dir")
        if config.get(key)
    }
    old |= {str(obs) for obs in _source_obs_roots(config)}

    config["toolkit_root"] = str(toolkit_root)
    config["runtime_dir"] = str(runtime_dir)
    config["papers_dir"] = str(papers_dir)

    # The runtime dir's `runs/` only, as in production: the rest of it holds the trial's ledgers
    # (receipt blobs, filed reports), which no actor may read. The papers are read by actors and
    # written only by the harness's own downloads, which run outside the jail, so they are
    # read-only to every jailed process (F25, 2026-10-06).
    private = [str(toolkit_root), str(Path(runtime_dir) / "runs"), str(papers_dir), str(obs_root)]
    for key in ("agent_fs_allow", "agent_fs_readonly"):
        roots = [str(Path(str(r)).expanduser()) for r in (config.get(key) or [])]
        kept = [r for r in roots if r not in old]
        config[key] = (private + kept) if key == "agent_fs_allow" else [*kept, str(papers_dir)]
    return config


def _source_obs_roots(config: dict[str, Any]) -> list[str]:
    """Allow-list entries that are the shared observation database, to be dropped.

    Read from the live observation configuration rather than pattern-matched, so a machine that
    moves its archive does not silently keep the old root in a trial's allow-list.
    """
    try:
        from auto_research.config_paths import observations_yaml

        with observations_yaml().open("r", encoding="utf-8") as stream:
            obs = yaml.safe_load(stream) or {}
        root = obs.get("obs_db_root")
        return [str(Path(str(root)).expanduser())] if root else []
    except OSError:
        return []


def observations_config(obs_root: Path) -> dict[str, Any]:
    """The trial's ``observations.yaml``: one line, pointing at its private copy."""
    return {"obs_db_root": str(obs_root)}


# ── Materialising it ────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def clone_toolkit(dest: Path, source: Path, base_ref: str, branch: str) -> str:
    """A private toolkit checkout at the pinned base, on the branch the runtime authors onto.

    Returns the resolved base SHA. The branch is the one the live configuration names, and it can
    be: the checkout is this trial's alone, so a shared name collides with nothing and keeps the
    authoring path byte-identical to production.
    """
    base_sha = _git(Path(source), "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(dest)],
        check=True, capture_output=True, text=True,
    )
    _git(dest, "checkout", "--quiet", "-B", branch, base_sha)
    return base_sha


def copy_observation(source: Path, dest: Path) -> dict[str, int]:
    """Copy one observation's archive files, and only those.

    Returns a count of what was taken and what was left, so a caller can assert the filter did
    something rather than trusting that it ran. A directory that produced nothing is a filter that
    has drifted from the archive's layout, which is worth failing on.
    """
    dest.mkdir(parents=True, exist_ok=True)
    taken = skipped = 0

    for name in RAW_DIRECTORIES:
        directory = source / name
        if directory.is_dir():
            shutil.copytree(directory, dest / name, dirs_exist_ok=True)
            taken += sum(1 for p in directory.rglob("*") if p.is_file())

    for name in RAW_FILES:
        if (source / name).is_file():
            shutil.copy2(source / name, dest / name)
            taken += 1

    events = source / "event_l2"
    if events.is_dir():
        (dest / "event_l2").mkdir(exist_ok=True)
        for path in sorted(events.iterdir()):
            if not path.is_file():
                continue
            if RAW_EVENT_LIST.match(path.name):
                shutil.copy2(path, dest / "event_l2" / path.name)
                taken += 1
            else:
                skipped += 1
    return {"copied": taken, "skipped": skipped}


def build_observation_database(
    dest: Path, source: Path, obsids: tuple[str, ...], mission: str, toolkit_root: Path
) -> dict[str, Any]:
    """A private observation database holding only the question's observations, archive files only.

    The catalog is seeded from the master's, trimmed to these observations, and then reconciled
    against what was actually copied by the toolkit's own recovery path -- which drops every row
    whose file is absent and recomputes the levels. Doing it that way rather than by hand is what
    makes the result consistent with the health check the launch runs, since both come from the
    same rules.
    """
    mission_dir = dest / mission
    mission_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"observations": {}}

    for obsid in obsids:
        origin = source / mission / obsid
        if not origin.is_dir():
            raise FileNotFoundError(f"observation {obsid} is not in {source / mission}")
        counts = copy_observation(origin, mission_dir / obsid)
        if counts["copied"] == 0:
            raise RuntimeError(
                f"copied nothing for {obsid}: the archive-file filter no longer matches the "
                f"layout of {origin}. A trial with no data would run and fail as science."
            )
        report["observations"][obsid] = counts

    catalog = source / mission / "catalog.json"
    if catalog.is_file():
        data = json.loads(catalog.read_text(encoding="utf-8"))
        entries = data.get("entries") or {}
        data["entries"] = {k: v for k, v in entries.items() if k in obsids}
        (mission_dir / "catalog.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        report["catalog"] = _reconcile_catalog(mission_dir, toolkit_root)
    return report


def _reconcile_catalog(mission_dir: Path, toolkit_root: Path) -> str:
    """Run the toolkit's catalog recovery over the private copy.

    Deliberate by definition -- the harness is asking for it, on a directory it just built -- and
    it can never reach the master, which this function is never given.
    """
    out = subprocess.run(
        ["python", "-m", "obsdb_catalog", "repair", str(mission_dir)],
        cwd=str(toolkit_root),
        env={**os.environ, "PYTHONPATH": str(toolkit_root)},
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"catalog reconciliation failed:\n{out.stdout}\n{out.stderr}")
    return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(data, stream, sort_keys=False, default_flow_style=False)


def materialize(
    arm: Arm,
    run_id: str,
    *,
    workspace_root: Path,
    base_ref: str,
    source_config_dir: Path | None = None,
    toolkit_source: Path | None = None,
    obs_source: Path | None = None,
    obsids: tuple[str, ...] = QUESTION_OBSIDS,
    mission: str = QUESTION_MISSION,
) -> TrialEnvironment:
    """Build one trial's private world and return where everything landed.

    ``workspace_root`` must sit outside the filesystem allow-list a trial is given, which is what
    keeps the configuration -- the one file naming the whole arm -- unreadable by any actor.
    """
    from auto_research.config_paths import config_dir as live_config_dir

    source_config_dir = Path(source_config_dir or live_config_dir())
    source_orchestration = _read_yaml(source_config_dir / "orchestration.yaml")
    toolkit_source = Path(toolkit_source or source_orchestration["toolkit_root"]).expanduser()
    if obs_source is None:
        obs_source = Path(_read_yaml(source_config_dir / "observations.yaml")["obs_db_root"])
    obs_source = Path(obs_source).expanduser()

    root = Path(workspace_root) / run_id
    # The structural guard, and the reason it is here rather than in a checklist: every path below
    # descends from this one, and a trial world is built in exactly one place. Block 0 caught the
    # campaign's own directory name reaching a librarian's prompt five minutes into the first real
    # run -- the workspace was called `science_bench`, and a read-only tool provider renders its
    # roots into the tool descriptions a model reads. A better name would have fixed that run; this
    # fixes every future one, because no workspace choice can leak past it.
    leaks = forbidden_in_path(root.resolve() if root.is_absolute() else root)
    if leaks:
        raise ValueError(
            f"the trial root {root} carries {', '.join(leaks)}, which an actor would read in "
            f"every tool description and shell command. Choose a workspace whose path says "
            f"nothing about the experiment."
        )
    # The second structural guard, and it was paid for the same way as the first. A slot's
    # `run_id` is deterministic — the seats and the replicate hash to it — so re-running a slot
    # lands on the root the previous attempt used. On 2026-09-02 that was measured: a relaunch
    # built into the prior attempt's world, the run **appended to its `monitor.jsonl`** (its
    # preflight lines are numbered from event 648, carrying the prior attempt's $19.43) and
    # inherited its `checkpoint.sqlite`, and the toolkit path the earlier teardown had removed
    # came back as an NFS `Errno 116 Stale file handle` inside the OS jail. One of those is a
    # corrupted measurement that reads like a real one; and the collision had already overwritten
    # a *completed, accepted* attempt's run directory before anyone noticed.
    #
    # So a world is built in a fresh root or not at all. Sweeping instead would be worse: the
    # runtime directory is kept on purpose (it holds the ledger, the figures and the report the
    # whole scoring pass reads), so a silent sweep would delete the evidence of the attempt
    # somebody is presumably re-running *because* they read it.
    if root.exists():
        contents = ", ".join(sorted(child.name for child in root.iterdir())) or "nothing"
        raise FileExistsError(
            f"the trial root {root} already exists (holding {contents}). A slot's run_id is "
            f"deterministic, so this is a previous attempt's world: building into it appends to "
            f"its ledger and inherits its checkpoint, which corrupts both records. Move or remove "
            f"it deliberately, then re-run:\n\n  mv {root} {root}.superseded\n"
        )
    config_dir = root / "config"
    toolkit_root = root / "toolkit"
    runtime_dir = root / "runtime"
    papers_dir = root / "papers"
    obs_root = root / "obs"
    for directory in (config_dir, runtime_dir, papers_dir, obs_root):
        directory.mkdir(parents=True, exist_ok=True)

    base_sha = clone_toolkit(
        toolkit_root, toolkit_source, base_ref,
        str(source_orchestration.get("tool_author_branch", "ai")),
    )
    build_observation_database(obs_root, obs_source, obsids, mission, toolkit_root)

    _write_yaml(
        config_dir / "orchestration.yaml",
        orchestration_config(
            source_orchestration,
            toolkit_root=toolkit_root, runtime_dir=runtime_dir,
            papers_dir=papers_dir, obs_root=obs_root,
        ),
    )
    _write_yaml(
        config_dir / "models.yaml",
        models_config(arm, _read_yaml(source_config_dir / "models.yaml")),
    )
    _write_yaml(config_dir / "observations.yaml", observations_config(obs_root))
    # Byte for byte: the scan lists are part of the instrument, not part of the treatment, and the
    # batch stamps their hash so a report names the gate configuration it was measured under.
    shutil.copy2(source_config_dir / "code_gate.yaml", config_dir / "code_gate.yaml")

    return TrialEnvironment(
        run_id=run_id, root=root, config_dir=config_dir, toolkit_root=toolkit_root,
        runtime_dir=runtime_dir, papers_dir=papers_dir, obs_root=obs_root, base_sha=base_sha,
    )


def teardown(environment: TrialEnvironment, *, keep_artifacts: bool = True) -> None:
    """Reclaim the bulky halves of a finished trial.

    The runtime directory stays by default: it holds the ledger, the figures and the report, and
    the whole scoring pass is offline over exactly those. What goes is the toolkit checkout and the
    data copy, which are reproducible from the pinned base and the archive.
    """
    for directory in (environment.toolkit_root, environment.obs_root):
        shutil.rmtree(directory, ignore_errors=True)
    if not keep_artifacts:
        shutil.rmtree(environment.root, ignore_errors=True)
