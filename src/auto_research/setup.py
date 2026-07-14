"""Interactive installer stage — the Python half of ``bash install.sh``.

``install.sh`` bootstraps conda + the ``auto_research`` env, then runs this module
(``python -m auto_research.setup``) to configure the machine-local files: it collects
paths, the analysis env, and (increments 3/4) domain dependencies + the toolkit, then
writes the gitignored ``config/orchestration.yaml`` + ``config/observations.yaml`` from
their tracked ``*.example`` templates and runs the launch preflight as a smoke test.

Two modes, one code path:
- **Interactive** (a TTY): prompts via ``questionary`` (arrow-menus / path entry), seeded
  with discovered defaults and any prior live config (so a re-run is an upgrade, not a
  re-interview).
- **Non-interactive** (``--answers FILE`` / no TTY): every value comes from the answers
  file; a needed value with no answer and no safe default fails loudly rather than hanging
  (the §9.8 headless contract).

Increment 2 implements pages 1 (conda), 2 (envs), 5 (paths), 6 (write configs), 7
(preflight). Pages 3 (domain deps + licensed/vendor tools) and 4 (research_toolkit) are
added in later increments; until then their config keys keep their prior/template values.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
ORCH_LIVE = CONFIG_DIR / "orchestration.yaml"
ORCH_TEMPLATE = CONFIG_DIR / "orchestration.yaml.example"
OBS_LIVE = CONFIG_DIR / "observations.yaml"
OBS_TEMPLATE = CONFIG_DIR / "observations.yaml.example"
SANDBOX_DIR = REPO_ROOT / "src" / "auto_research" / "sandbox"
ANALYST_ENV_LIVE = SANDBOX_DIR / "analyst_env.sh"
REPL_INIT_LIVE = SANDBOX_DIR / "repl_init.py"

# Placeholder values in the templates — treated as "unset" when seeding defaults.
_PLACEHOLDER_RE = re.compile(r"^/path/to(/|$)")


class SetupError(RuntimeError):
    """A setup step could not complete (e.g. a required answer is missing headless)."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested; no prompting, no side effects)
# ---------------------------------------------------------------------------


def _is_placeholder(value: object) -> bool:
    """True for an empty/template-placeholder path value (so it is not seeded as a default)."""
    return not value or (isinstance(value, str) and bool(_PLACEHOLDER_RE.match(value.strip())))


def derive_fs_allow(values: dict, extra_paths: list[str] | None = None) -> list[str]:
    """Derive the ``agent_fs_allow`` allow-list from the answered paths (P4 fix).

    The allow-list is the set of absolute roots every runtime agent may read/write. Deriving
    it from the other path answers removes the hand-sync drift the review flagged. ``auto_research``
    is deliberately never included (§2/§4 trust boundary). ``extra_paths`` carries domain-dependency
    roots (HEADAS/CALDB/license dirs) contributed by the page-3 domain-deps step (increment 3).
    """
    candidates = [
        values.get("toolkit_root"),
        values.get("obs_db_root"),
        values.get("analyze_env"),
        values.get("runtime_dir"),
        *(extra_paths or []),
        "/tmp",
    ]
    out: list[str] = []
    for c in candidates:
        if _is_placeholder(c):
            continue
        c = str(c)
        if c not in out:
            out.append(c)
    return out


def _format_scalar(value: object) -> str:
    """Render a scalar YAML value, quoting when needed (spaces, colons, leading specials)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    if s == "":
        return '""'
    if re.search(r"[\s:#,]", s) or s[0] in "!&*?{}[]>|@`\"'%":
        return '"' + s.replace('"', '\\"') + '"'
    return s


def render_orchestration(template_text: str, values: dict, fs_allow: list[str]) -> str:
    """Render the live orchestration.yaml from the template, substituting answered values.

    Line-based so **all template comments/structure are preserved** — only the value token of a
    scalar key present in ``values`` is swapped (inline ``# comment`` kept), and the
    ``agent_fs_allow`` list block is regenerated from ``fs_allow``. Keys absent from ``values``
    keep their template value, so a partially-answered install (early increments) still produces
    a valid file.
    """
    lines = template_text.splitlines()
    out: list[str] = []
    i = 0
    scalar_key_re = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>\s+\S.*)?$")
    while i < len(lines):
        line = lines[i]
        # Regenerate the agent_fs_allow block: its header line, then its indented list items.
        if line.rstrip() == "agent_fs_allow:":
            out.append("agent_fs_allow:")
            for p in fs_allow:
                out.append(f"  - {p}")
            i += 1
            while i < len(lines) and (re.match(r"^\s*-\s", lines[i]) or lines[i].strip() == ""):
                # consume the template's list items; stop at the first non-list, non-blank line
                if lines[i].strip() == "":
                    break
                i += 1
            continue
        m = scalar_key_re.match(line)
        if m and m.group("key") in values:
            key = m.group("key")
            rest = m.group("rest") or ""
            comment_m = re.search(r"(\s{2,}#.*)$", rest)
            comment = comment_m.group(1) if comment_m else ""
            out.append(f"{key}: {_format_scalar(values[key])}{comment}")
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out) + "\n"


def render_observations(template_text: str, obs_db_root: str) -> str:
    """Render the live observations.yaml from the template, substituting ``obs_db_root``."""
    lines = template_text.splitlines()
    out = []
    for line in lines:
        if re.match(r"^obs_db_root:\s", line):
            out.append(f"obs_db_root: {_format_scalar(obs_db_root)}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Domain dependencies (page 3) — typed entries → the two sandbox hooks + fs_allow + probe
# ---------------------------------------------------------------------------
#
# Each entry is `{"kind": ..., "name": ..., <kind fields>, "probe"?, "fs_paths"?}`. Kinds:
#   shell_init   {"path"}                       → `source "<path>"` in analyst_env.sh (bucket A)
#   env_var      {"var", "value"}               → `export VAR="value"` in analyst_env.sh (A/D)
#   conda_pkg    {"spec"}                        → conda-installed into analyze_env at setup
#   pip_pkg      {"spec"}                        → pip-installed into analyze_env at setup
#   python_init  {"code"} | {"path"}            → appended to repl_init.py (bucket C session setup)
#   license_tool {"binary_dir"?, "license_var"?, "license_value"?}  → PATH + license export (bucket E)
# Double-quoted rendering so `$VAR` references in a value (e.g. `$HEADAS/headas-init.sh`) expand.

_ANALYST_ENV_HEADER = (
    "#!/bin/bash\n"
    "# Headless analyst environment — GENERATED by `python -m auto_research.setup` from the\n"
    "# domain-dependency entries. Sourced per tool subprocess (no interactive cruft). Hand-edit\n"
    "# freely; a re-run of the installer regenerates it from your recorded entries.\n"
)
_REPL_INIT_HEADER = (
    "# Live REPL-init hook — GENERATED by `python -m auto_research.setup`. Exec'd once at each\n"
    "# analyst REPL subprocess boot, inside analyze_env; best-effort (failures swallowed).\n"
)


def _dq(value: str) -> str:
    """Double-quote a shell value (allows ``$VAR`` expansion; escapes ``"``/`` ` ``/``\\``)."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") + '"'


def render_analyst_env(entries: list[dict]) -> str:
    """Render analyst_env.sh from the boot-command entries (shell_init / env_var / license_tool)."""
    lines = [_ANALYST_ENV_HEADER]
    for e in entries:
        kind = e.get("kind")
        if kind == "env_var" and e.get("var"):
            lines.append(f'export {e["var"]}={_dq(e.get("value", ""))}')
        elif kind == "shell_init" and e.get("path"):
            lines.append(f'source {_dq(e["path"])}')
        elif kind == "license_tool":
            if e.get("binary_dir"):
                lines.append(f'export PATH={_dq(str(e["binary_dir"]) + ":$PATH")}')
            if e.get("license_var") and e.get("license_value"):
                lines.append(f'export {e["license_var"]}={_dq(e["license_value"])}')
    return "\n".join(lines) + "\n"


def render_repl_init(entries: list[dict]) -> str:
    """Render repl_init.py from the python_init entries (inline code or a file to read)."""
    blocks = [_REPL_INIT_HEADER.rstrip()]
    for e in entries:
        if e.get("kind") != "python_init":
            continue
        code = e.get("code")
        if not code and e.get("path"):
            try:
                code = Path(e["path"]).read_text(encoding="utf-8")
            except OSError:
                code = None
        if code:
            blocks.append(code.rstrip())
    return "\n\n".join(blocks) + "\n"


def domain_fs_paths(entries: list[dict]) -> list[str]:
    """Absolute roots the domain-dep entries need in ``agent_fs_allow`` (bucket A/D/E)."""
    out: list[str] = []

    def add(p):
        if p and str(p).startswith("/") and str(p) not in out:
            out.append(str(p))

    for e in entries:
        for p in e.get("fs_paths", []) or []:
            add(p)
        if e.get("kind") == "env_var":
            add(e.get("value"))
        elif e.get("kind") == "license_tool":
            add(e.get("binary_dir"))
            add(e.get("license_value"))
    return out


def domain_probe(entries: list[dict], existing: str = "") -> str:
    """Assemble the ``analyze_probe`` from per-entry probe snippets (deduped, ``;``-joined)."""
    parts: list[str] = []
    for src in [existing] + [e.get("probe", "") for e in entries]:
        for piece in str(src or "").split(";"):
            piece = piece.strip()
            if piece and piece not in parts:
                parts.append(piece)
    return "; ".join(parts)


def package_specs(entries: list[dict]) -> tuple[list[str], list[str]]:
    """Split entries into (conda_specs, pip_specs) for install into analyze_env."""
    conda = [e["spec"] for e in entries if e.get("kind") == "conda_pkg" and e.get("spec")]
    pip = [e["spec"] for e in entries if e.get("kind") == "pip_pkg" and e.get("spec")]
    return conda, pip


def discover_domain_deps() -> list[dict]:
    """Suggest domain-dep entries from the environment ($HEADAS, $CALDB) for the interactive page."""
    suggestions: list[dict] = []
    headas = os.environ.get("HEADAS")
    if headas and Path(headas).exists():
        suggestions.append({"kind": "env_var", "name": "HEADAS", "var": "HEADAS", "value": headas, "fs_paths": [headas]})
        suggestions.append({"kind": "shell_init", "name": "headas-init", "path": "$HEADAS/headas-init.sh"})
    caldb = os.environ.get("CALDB")
    if caldb and Path(caldb).exists():
        suggestions.append({"kind": "env_var", "name": "CALDB", "var": "CALDB", "value": caldb, "fs_paths": [caldb]})
    return suggestions


def _load_yaml(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except OSError:
        return {}


def seed_defaults() -> dict:
    """Build default answers: prior live config values (an upgrade) over template placeholders."""
    defaults: dict = {}
    template = _load_yaml(ORCH_TEMPLATE)
    live = _load_yaml(ORCH_LIVE)
    for key in (
        "runtime_dir", "papers_dir", "toolkit_root", "analyze_env",
        "analyze_ini_sh", "repl_init_py", "analyze_probe",
    ):
        val = live.get(key, template.get(key))
        if not _is_placeholder(val) or key == "analyze_probe":
            defaults[key] = val
    obs_live = _load_yaml(OBS_LIVE)
    obs_val = obs_live.get("obs_db_root")
    if not _is_placeholder(obs_val):
        defaults["obs_db_root"] = obs_val
    return defaults


# ---------------------------------------------------------------------------
# Prompting (interactive) / answer-file (non-interactive)
# ---------------------------------------------------------------------------


class Answers:
    """Resolve a value from the answers file, an interactive prompt, or a default (else fail)."""

    def __init__(self, preset: dict, interactive: bool) -> None:
        self.preset = preset or {}
        self.interactive = interactive
        self.collected: dict = {}

    def _record(self, key: str, value):
        self.collected[key] = value
        return value

    def path(self, key: str, prompt: str, default: str | None = None) -> str:
        return self._resolve(key, prompt, default, kind="path")

    def text(self, key: str, prompt: str, default: str | None = None) -> str:
        return self._resolve(key, prompt, default, kind="text")

    def choice(self, key: str, prompt: str, choices: list[str], default: str | None = None) -> str:
        return self._resolve(key, prompt, default, kind="choice", choices=choices)

    def confirm(self, key: str, prompt: str, default: bool = True) -> bool:
        if key in self.preset:
            return bool(self._record(key, self.preset[key]))
        if not self.interactive:
            return self._record(key, default)
        import questionary  # lazy — only needed interactively
        return self._record(key, bool(questionary.confirm(prompt, default=default).ask()))

    def _resolve(self, key, prompt, default, kind, choices=None):
        if key in self.preset:
            return self._record(key, self.preset[key])
        if self.interactive:
            import questionary  # lazy — only needed interactively
            if kind == "choice":
                ans = questionary.select(prompt, choices=choices, default=default).ask()
            else:
                ans = questionary.text(prompt, default=default or "").ask()
            if ans is None:
                raise SetupError("setup cancelled")
            return self._record(key, ans)
        if default is not None:
            return self._record(key, default)
        raise SetupError(
            f"missing answer '{key}' (non-interactive run; provide it in --answers or run in a terminal)"
        )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _find_conda() -> str | None:
    """Locate the conda executable (CONDA_EXE, PATH, then common install roots)."""
    exe = os.environ.get("CONDA_EXE")
    if exe and Path(exe).exists():
        return exe
    found = shutil.which("conda")
    if found:
        return found
    for root in ("~/miniforge3", "~/miniconda3", "~/anaconda3", "/opt/conda"):
        cand = Path(root).expanduser() / "bin" / "conda"
        if cand.exists():
            return str(cand)
    return None


def page_conda() -> str:
    """Page 1 — confirm conda is available (install.sh ensures it; this reports what it found)."""
    conda = _find_conda()
    if not conda:
        raise SetupError(
            "conda not found. Install Miniforge (https://github.com/conda-forge/miniforge) and re-run install.sh."
        )
    print(f"  conda: {conda}")
    return conda


def page_envs(ans: Answers, conda: str, *, skip_envs: bool) -> None:
    """Page 2 — the analyst env (the auto_research env is created by install.sh already).

    Prompts for the analyst env location (a name, or a ``-p`` prefix for cluster/quota setups),
    then creates/updates it from ``environment-analyst.yml`` and records it as ``analyze_env``.
    """
    default_prefix = ans.preset.get("analyze_env") or seed_defaults().get("analyze_env") or ""
    prefix = ans.path("analyze_env", "Analyst conda env prefix (bin/python with the analysis stack):", default_prefix)
    if skip_envs:
        print(f"  analyst env: {prefix} (env creation skipped)")
        return
    analyst_yml = REPO_ROOT / "environment-analyst.yml"
    exists = Path(prefix, "bin", "python").exists()
    action = "update" if exists else "create"
    if exists:
        action = ans.choice(
            "analyst_env_action",
            f"Analyst env exists at {prefix}:",
            choices=["update", "recreate", "skip"],
            default="update",
        )
    if action == "skip":
        return
    cmd = [conda, "env", ("update" if action == "update" else "create"), "-f", str(analyst_yml), "-p", prefix]
    if action == "update":
        cmd.append("--prune")
    if action == "recreate" and exists:
        subprocess.run([conda, "env", "remove", "-p", prefix, "-y"], check=False)
    print(f"  {action} analyst env: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def page_paths(ans: Answers, defaults: dict) -> dict:
    """Page 5 — the machine paths outside both repos."""
    runtime_dir = ans.path("runtime_dir", "Runtime data dir (SQLite/figures/logs/reports):", defaults.get("runtime_dir"))
    papers_dir = ans.path("papers_dir", "Downloaded-papers dir:", defaults.get("papers_dir") or f"{runtime_dir}/papers")
    obs_db_root = ans.path("obs_db_root", "Observation/data root (one subdir per mission/dataset):", defaults.get("obs_db_root"))
    return {"runtime_dir": runtime_dir, "papers_dir": papers_dir, "obs_db_root": obs_db_root}


def seed_extra_fs_paths() -> list[str]:
    """Prior ``agent_fs_allow`` roots that are NOT the auto-derived core — the domain roots to
    preserve when the domain-deps page is skipped, so a re-run never drops HEADAS/CALDB/etc."""
    live = _load_yaml(ORCH_LIVE)
    prior = live.get("agent_fs_allow") or []
    core = {
        live.get("toolkit_root"), live.get("obs_db_root"),
        live.get("analyze_env"), live.get("runtime_dir"), "/tmp",
    }
    return [p for p in prior if p not in core]


def _build_domain_entries_interactive(defaults: list[dict]) -> list[dict]:
    """Interactive add/edit/done loop for domain-dependency entries (seeded by discovery)."""
    import questionary

    entries = list(defaults)
    kind_labels = {
        "shell_init": "shell init script (source)",
        "env_var": "environment variable (export)",
        "conda_pkg": "conda package (install into analyze_env)",
        "pip_pkg": "pip package (install into analyze_env)",
        "python_init": "python session snippet (REPL boot)",
        "license_tool": "licensed / vendor tool (point at an installed binary + license)",
    }
    while True:
        names = ", ".join(e.get("name") or e.get("kind") for e in entries) or "(none)"
        print(f"  domain deps: {names}")
        action = questionary.select(
            "Domain dependency:", choices=["add", "done"], default="done"
        ).ask()
        if action != "add":
            break
        kind = questionary.select("Kind:", choices=list(kind_labels.values())).ask()
        kind = next(k for k, v in kind_labels.items() if v == kind)
        e: dict = {"kind": kind, "name": questionary.text("Label:").ask() or kind}
        if kind == "shell_init":
            e["path"] = questionary.text("Script to source:").ask()
        elif kind == "env_var":
            e["var"] = questionary.text("Variable name:").ask()
            e["value"] = questionary.text("Value:").ask()
            if e["value"].startswith("/"):
                e["fs_paths"] = [e["value"]]
        elif kind in ("conda_pkg", "pip_pkg"):
            e["spec"] = questionary.text("Package spec (e.g. numpy=2.3):").ask()
        elif kind == "python_init":
            e["code"] = questionary.text("Python snippet:").ask()
        elif kind == "license_tool":
            e["binary_dir"] = questionary.text("Installed binary directory (added to PATH):").ask()
            e["license_var"] = questionary.text("License env var (e.g. LM_LICENSE_FILE; blank if none):").ask()
            if e["license_var"]:
                e["license_value"] = questionary.text("License value (port@host or a license-file path):").ask()
            e["fs_paths"] = [p for p in [e.get("binary_dir"), e.get("license_value")] if p and p.startswith("/")]
            print("  (Confirm the vendor tool is installed and its license accepted out of band.)")
        probe = questionary.text("Optional probe snippet (blank to skip):").ask()
        if probe:
            e["probe"] = probe
        entries.append(e)
    return entries


def page_domain_deps(ans: Answers, analyze_env: str, conda: str, *, skip_envs: bool) -> list[dict]:
    """Page 3 — collect domain-dependency entries, materialize the two hooks, install packages.

    Entries come from the answers file (``domain_deps``) headless, or an interactive add/edit loop
    seeded by discovery. From them: write ``analyst_env.sh`` (boot commands) + ``repl_init.py``
    (session snippets), and install any conda/pip package specs into ``analyze_env``. Returns the
    entry list (empty ⇒ caller preserves existing hooks + prior fs-allow domain roots).
    """
    if "domain_deps" in ans.preset:
        entries = list(ans.preset["domain_deps"] or [])
    elif ans.interactive:
        entries = _build_domain_entries_interactive(discover_domain_deps())
    else:
        entries = []
    if not entries:
        print("  domain deps: (unchanged — preserving existing hooks)")
        return entries

    ANALYST_ENV_LIVE.write_text(render_analyst_env(entries), encoding="utf-8")
    REPL_INIT_LIVE.write_text(render_repl_init(entries), encoding="utf-8")
    print(f"  wrote {ANALYST_ENV_LIVE}")
    print(f"  wrote {REPL_INIT_LIVE}")

    conda_specs, pip_specs = package_specs(entries)
    if (conda_specs or pip_specs) and not skip_envs:
        if conda_specs:
            subprocess.run([conda, "install", "-p", analyze_env, "-y", *conda_specs], check=True)
        if pip_specs:
            subprocess.run([str(Path(analyze_env) / "bin" / "pip"), "install", *pip_specs], check=True)
    elif conda_specs or pip_specs:
        print(f"  (skipped installing {len(conda_specs) + len(pip_specs)} package(s) into analyze_env)")
    return entries


# ---------------------------------------------------------------------------
# research_toolkit (page 4) — scan / point / clone / start-fresh scaffold
# ---------------------------------------------------------------------------
#
# The scaffold mirrors the §9.2 uniform layout (xspec_utils is the template every package
# mirrors). Templates live here in auto_research and are written into the toolkit only at
# install-runtime — the build-time agent never edits research_toolkit.


def _skeleton_files(domain: str) -> dict[str, str]:
    """The `<domain>_utils` skeleton (§9.2): human-half `lib/`, pure-data ai-half `skills/`, tests."""
    d = domain
    return {
        f"{d}_utils/__init__.py": (
            f'"""{d}_utils — research_toolkit package scaffolded by auto_research setup.\n\n'
            "Re-exports TOOL_MANIFEST for ToolkitRegistry and puts lib/ on sys.path. Keep this module\n"
            "free of heavy domain imports — it must import in the orchestration env (no science stack).\n"
            '"""\n\n'
            "from .skills import TOOL_MANIFEST\n\n"
            '__all__ = ["TOOL_MANIFEST"]\n'
        ),
        f"{d}_utils/lib/__init__.py": '"""Human-half: importable, runnable domain code (§9.2)."""\n',
        f"{d}_utils/lib/{d}_help.py": (
            f'"""High-level, abstracted entry API for {d} — the human-half a person calls.\n\n'
            "Hide the failure-prone choices (paths, ordering, options) inside tested code here so\n"
            "neither a human nor the AI can get them wrong (§9.2). Fill in as tools are authored.\n"
            '"""\n'
        ),
        f"{d}_utils/skills/__init__.py": (
            f'"""TOOL_MANIFEST — the pure-data tool interface for {d}_utils.\n\n'
            "PURE DATA ONLY: no domain/science imports here, so ToolkitRegistry discovers it in the\n"
            "orchestration env. Each entry wraps a lib/ function:\n"
            f'  {{"module": "{d}_utils.lib.{d}_help", "fn": ..., "description": ..., "args_schema": {{...}}}}\n'
            '"""\n\n'
            "TOOL_MANIFEST: dict = {}\n"
        ),
        f"{d}_utils/skills/session.py": (
            '"""Science-stack-dependent wrappers — imported ONLY inside the REPL subprocess (§9.2)."""\n'
        ),
        f"{d}_utils/tests/__init__.py": "",
        f"{d}_utils/tests/test_{d}.py": (
            f'"""Layer-B tests for {d}_utils tools — authored WITH each tool (§9.4)."""\n\n\n'
            "def test_manifest_is_pure_data():\n"
            f"    from {d}_utils import TOOL_MANIFEST\n\n"
            "    assert isinstance(TOOL_MANIFEST, dict)\n"
        ),
    }


def scaffold_toolkit_package(toolkit_root: Path, domain: str) -> list[Path]:
    """Write a fresh `<domain>_utils` skeleton under ``toolkit_root``. Returns the files written."""
    written: list[Path] = []
    for rel, content in _skeleton_files(domain).items():
        path = Path(toolkit_root) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def init_toolkit_git(toolkit_root: Path, branch: str = "ai") -> None:
    """git-init ``toolkit_root``, ensure a commit identity, and check out ``branch`` (§9.9).

    The runtime commits authored tools onto ``branch`` and the launch preflight asserts the
    checkout is on it with a clean tree, so a start-fresh toolkit must already be there.
    """
    root = str(toolkit_root)
    if not (Path(toolkit_root) / ".git").exists():
        subprocess.run(["git", "init", root], check=True)
    # Ensure a local commit identity (preflight requires one; keep it neutral if unset).
    for key, default in (("user.name", "auto_research setup"), ("user.email", "auto_research@localhost")):
        have = subprocess.run(["git", "-C", root, "config", key], capture_output=True, text=True)
        if not have.stdout.strip():
            subprocess.run(["git", "-C", root, "config", key, default], check=True)
    # Create/switch to the branch (an empty repo has no commits yet, so seed one).
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    subprocess.run(["git", "-C", root, "commit", "-m", "scaffold toolkit", "--allow-empty"], check=True)
    subprocess.run(["git", "-C", root, "checkout", "-B", branch], check=True)


def stage_raw_code(src: Path, runtime_dir: Path, name: str) -> Path:
    """Copy the user's undisciplined domain code into ``<runtime_dir>/staging/<name>/``.

    Kept OUT of the toolkit worktree on purpose: a failed authoring attempt runs ``git clean -fd``
    there (§9.9), which would delete raw code staged inside it. The onboard mini-pipeline (increment
    5) reads this as reference input and authors fresh two-halves files into the worktree.
    """
    dest = Path(runtime_dir) / "staging" / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return dest


def discover_toolkit() -> list[str]:
    """Suggest research_toolkit locations (prior config, then a sibling of this repo)."""
    out: list[str] = []
    prior = _load_yaml(ORCH_LIVE).get("toolkit_root")
    if prior and Path(prior).exists():
        out.append(str(prior))
    sibling = REPO_ROOT.parent / "research_toolkit"
    if sibling.exists() and str(sibling) not in out:
        out.append(str(sibling))
    return out


def _kickoff_message(toolkit_root: str, staging: Path | None, domain: str) -> str:
    """The next-step handoff to the (deferred, increment-5) onboard authoring pipeline."""
    lines = [
        f"  scaffolded {domain}_utils at {toolkit_root} (on branch 'ai').",
        "  Next: turn your everyday analysis code into gated, tested tools via the authoring pipeline.",
    ]
    if staging:
        lines.append(f"    - your raw code is staged (read-only reference) at: {staging}")
    lines.append("    - run:  python -m auto_research.onboard_toolkit   # (deferred — increment 5)")
    return "\n".join(lines)


def page_toolkit(ans: Answers, defaults: dict, runtime_dir: str) -> str:
    """Page 4 — resolve ``toolkit_root`` (scan → point / clone / start-fresh). Returns the path."""
    found = discover_toolkit()
    default_root = (found[0] if found else None) or defaults.get("toolkit_root")
    action = ans.choice(
        "toolkit_action",
        "research_toolkit:",
        choices=["point", "clone", "start_fresh"],
        default="point" if default_root else "start_fresh",
    )
    if action == "point":
        return ans.path("toolkit_root", "Path to the research_toolkit checkout:", default_root)
    if action == "clone":
        url = ans.text("toolkit_url", "Git URL to clone:")
        dest = ans.path("toolkit_root", "Clone into:", default_root)
        subprocess.run(["git", "clone", url, dest], check=True)
        return dest
    # start_fresh
    dest = ans.path("toolkit_root", "Create a new research_toolkit at:", default_root)
    domain = ans.text("toolkit_domain", "New toolkit domain name (e.g. xspec, genome):")
    Path(dest).mkdir(parents=True, exist_ok=True)
    scaffold_toolkit_package(Path(dest), domain)
    init_toolkit_git(Path(dest))
    staging = None
    raw = ans.text("raw_code_dir", "Directory of existing analysis code to stage (blank to skip):", "")
    if raw:
        staging = stage_raw_code(Path(raw), Path(runtime_dir), f"{domain}_raw")
    print(_kickoff_message(dest, staging, domain))
    return dest


def write_configs(values: dict, extra_fs_paths: list[str] | None = None) -> tuple[Path, Path]:
    """Page 6 — render + write the live orchestration.yaml and observations.yaml.

    Renders from the tracked ``*.example`` templates (so new keys/comments are picked up) and
    derives ``agent_fs_allow`` from the answered paths. Returns the two written paths.
    """
    fs_allow = derive_fs_allow(values, extra_fs_paths)
    orch_out = render_orchestration(ORCH_TEMPLATE.read_text(encoding="utf-8"), values, fs_allow)
    ORCH_LIVE.write_text(orch_out, encoding="utf-8")
    obs_out = render_observations(OBS_TEMPLATE.read_text(encoding="utf-8"), values["obs_db_root"])
    OBS_LIVE.write_text(obs_out, encoding="utf-8")
    return ORCH_LIVE, OBS_LIVE


def page_preflight() -> bool:
    """Page 7 — run the launch preflight as a smoke test. Returns True on pass."""
    import asyncio

    from auto_research.preflight import PreflightError, run_preflight

    try:
        asyncio.run(run_preflight())
        print("  preflight: ok")
        return True
    except PreflightError as exc:
        print(f"  preflight reported issues:\n{exc}")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auto_research.setup", description=__doc__)
    parser.add_argument("--answers", type=str, help="YAML/JSON file of preset answers (non-interactive)")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt; require --answers")
    parser.add_argument("--skip-envs", action="store_true", help="do not create/update conda envs")
    parser.add_argument("--skip-preflight", action="store_true", help="do not run the preflight smoke test")
    args = parser.parse_args(argv)

    preset = _load_yaml(Path(args.answers)) if args.answers else {}
    interactive = sys.stdin.isatty() and not args.non_interactive
    ans = Answers(preset, interactive)
    defaults = seed_defaults()

    print("auto_research setup")
    conda = page_conda()
    page_envs(ans, conda, skip_envs=args.skip_envs)
    analyze_env = ans.collected.get("analyze_env") or defaults.get("analyze_env") or ""
    entries = page_domain_deps(ans, analyze_env, conda, skip_envs=args.skip_envs)
    paths = page_paths(ans, defaults)
    toolkit_root = page_toolkit(ans, defaults, paths["runtime_dir"])

    # Merge: page answers over seeded defaults (prior/template) for the config keys.
    values = {**defaults, **paths, **ans.collected, "toolkit_root": toolkit_root}
    _transient = ("analyze_env_action", "domain_deps", "toolkit_action", "toolkit_url", "toolkit_domain", "raw_code_dir")
    values = {k: v for k, v in values.items() if k not in _transient}

    # Page-3 entries own the hook paths, the probe, and the domain fs-allow roots; when the page is
    # skipped, preserve the prior hooks + domain roots so a partial re-run never drops them (P4).
    if entries:
        values["analyze_ini_sh"] = str(ANALYST_ENV_LIVE)
        values["repl_init_py"] = str(REPL_INIT_LIVE)
        values["analyze_probe"] = domain_probe(entries, existing=defaults.get("analyze_probe", ""))
        extra_fs = domain_fs_paths(entries)
    else:
        extra_fs = seed_extra_fs_paths()

    orch_path, obs_path = write_configs(values, extra_fs_paths=extra_fs)
    print(f"  wrote {orch_path}")
    print(f"  wrote {obs_path}")

    if not args.skip_preflight:
        page_preflight()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
