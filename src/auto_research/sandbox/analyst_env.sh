#!/bin/bash
# Dedicated headless analyst environment for the auto_research sandbox.
# Sourced by PersistentSandbox via _source_ini_env(). Analysis-only:
# no SSH agent, no gnome-keyring, no interactive conda activation.
# The sandbox invokes {analyze_env}/bin/python directly, so no PATH-level
# conda activation is needed.

# HEAsoft
export HEADAS=/opt/heasoft/heasoft-6.34/x86_64-pc-linux-gnu-libc2.34
. "$HEADAS/headas-init.sh"

# CALDB
export CALDB=/man1/CALDB
export CALDBCONFIG="$CALDB/software/tools/caldb.config"
export CALDBALIAS="$CALDB/software/tools/alias_config.fits"
export GEOMAG_PATH=/man1/CALDB/data/gen/pcf/geomag
