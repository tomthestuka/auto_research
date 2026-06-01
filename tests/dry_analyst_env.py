"""Dry test of the analyst sandbox environment — run manually, not via pytest.

Mimics what PersistentSandbox does for each tool call:
  - interpreter: orchestration.yaml::analyze_env / bin / python
  - env: source analyst_env.sh, then PYTHONPATH = toolkit_root : $PYTHONPATH
Then imports every TOOL_MANIFEST tool module + resolves its callable, and
exercises the heavy ixpeobssim + PyXSPEC path via a real IXPE load.

Usage (from repo root):
    source src/auto_research/sandbox/analyst_env.sh
    export PYTHONPATH=/data/fzhou/research_toolkit:$PYTHONPATH
    /grad/fzhou/.conda/envs/astropy/bin/python tests/dry_analyst_env.py
"""
import importlib
import sys
import traceback

print("python:", sys.version.split()[0], sys.executable)

import xspec_utils

manifest = xspec_utils.TOOL_MANIFEST
print(f"manifest tools: {len(manifest)}\n")

# Phase 1 — import each tool's module and resolve its callable (what the REPL does).
fails = 0
for name, spec in manifest.items():
    try:
        mod = importlib.import_module(spec["module"])
        getattr(mod, spec["fn"])
        print(f"IMPORT OK   {name}")
    except Exception:
        fails += 1
        print(f"IMPORT FAIL {name}  ({spec['module']}.{spec['fn']})")
        traceback.print_exc()
        print()

print(f"\n--- import phase: {len(manifest) - fails}/{len(manifest)} ok ---\n")

# Phase 2 — exercise the heavy ixpeobssim + xspec path: real IXPE load + status.
try:
    import xspec_help

    print("Calling load_data(IXPE 02008401, HSS)...")
    indices = xspec_help.load_data(
        epoch={"IXPE": ["02008401"], "STATE": "HSS"},
        ixpe_path="/data/fzhou/XSPEC/Cyg_X-1/IXPE",
        clear_existing=True,
        chatter=False,
    )
    print("load_data OK; indices keys:", list(indices.keys()))
    from xspec_utils.skills import session

    print("xspec_status:", session.xspec_status().get("stdout"))
except Exception:
    print("LOAD/STATUS FAIL")
    traceback.print_exc()
