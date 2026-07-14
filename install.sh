#!/usr/bin/env bash
# auto_research installer — bootstrap conda + the orchestration env, then run the
# interactive Python setup stage (auto_research.setup) which configures the machine-local
# config, the analyst env, domain deps, and the toolkit.
#
# Thin by design: everything that prompts lives in Python. Run this from the repo root in a
# real terminal. Any flags are forwarded to the Python stage (e.g. --answers FILE for a
# non-interactive install, --skip-envs, --skip-preflight).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

ORCH_ENV="${AR_ORCH_ENV:-auto_research}"   # override to install into a differently-named env

# ── 1. Locate conda (CONDA_EXE → PATH → common install roots), or offer Miniforge ──
find_conda() {
  if [[ -n "${CONDA_EXE:-}" && -x "${CONDA_EXE}" ]]; then echo "${CONDA_EXE}"; return; fi
  if command -v conda >/dev/null 2>&1; then command -v conda; return; fi
  for root in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" /opt/conda; do
    if [[ -x "$root/bin/conda" ]]; then echo "$root/bin/conda"; return; fi
  done
  echo ""
}

CONDA="$(find_conda)"
if [[ -z "$CONDA" ]]; then
  echo "conda not found."
  if [[ -t 0 ]]; then
    read -r -p "Install Miniforge to \$HOME/miniforge3 now? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
      case "$(uname -s)-$(uname -m)" in
        Linux-x86_64)  MF="Miniforge3-Linux-x86_64.sh" ;;
        Linux-aarch64) MF="Miniforge3-Linux-aarch64.sh" ;;
        Darwin-arm64)  MF="Miniforge3-MacOSX-arm64.sh" ;;
        Darwin-x86_64) MF="Miniforge3-MacOSX-x86_64.sh" ;;
        *) echo "Unsupported platform $(uname -s)-$(uname -m); install conda manually." >&2; exit 1 ;;
      esac
      TMP="$(mktemp -d)"
      curl -fsSL "https://github.com/conda-forge/miniforge/releases/latest/download/${MF}" -o "$TMP/mf.sh"
      bash "$TMP/mf.sh" -b -p "$HOME/miniforge3"
      CONDA="$HOME/miniforge3/bin/conda"
    else
      echo "Install Miniforge (https://github.com/conda-forge/miniforge) and re-run." >&2; exit 1
    fi
  else
    echo "Install Miniforge (https://github.com/conda-forge/miniforge) and re-run." >&2; exit 1
  fi
fi
echo "Using conda: $CONDA"

# Make `conda activate`/`conda run` available in this non-login shell.
CONDA_BASE="$("$CONDA" info --base)"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"

# ── 2. Create or update the orchestration env from environment.yml ──
if "$CONDA" env list | awk '{print $1}' | grep -qx "$ORCH_ENV"; then
  echo "Updating '$ORCH_ENV' env from environment.yml …"
  "$CONDA" env update -n "$ORCH_ENV" -f environment.yml --prune
else
  echo "Creating '$ORCH_ENV' env from environment.yml …"
  "$CONDA" env create -n "$ORCH_ENV" -f environment.yml
fi

# ── 3. Hand off to the interactive Python setup stage (in the orchestration env) ──
echo "Launching setup …"
exec "$CONDA" run --no-capture-output -n "$ORCH_ENV" python -m auto_research.setup "$@"
