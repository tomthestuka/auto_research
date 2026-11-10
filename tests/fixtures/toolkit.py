"""One builder for a minimal toolkit that meets the orchestration's toolkit contract (Phase 3, 0a).

Two callers build on it (B5: one builder, not two):

* ``tests/test_toolkit_contract.py`` builds the whole fixture toolkit (:func:`build_toolkit`) and
  points a configuration at it (:func:`write_config`), so the orchestration's entry points (the
  registry, the launch checks, the gate, a worker's sandbox) are shown to pair with a toolkit that
  is not ``research_toolkit``;
* ``benchmarks/gate_bench`` builds its synthetic base from the same ``demo_utils`` package
  (:func:`write_base_package`, :func:`init_repo`); its bytes are the frozen corpus base, so the
  defaults below write exactly what the benchmark measured.

The fixture follows the toolkit's own contract (research_toolkit ``CLAUDE.md`` §2, §5, §6b):
``*_utils`` packages with a pure-data ``skills`` manifest over ``lib`` code, every argument
declaring its kind, a ``tool_config.yaml`` naming the package's environment, an ``envs/<id>/``
definition, a root ``conftest.py`` with the ``slow`` and ``live`` test classes, a ``README.md`` per
package, and a git repository whose authoring branch is checked out and clean. Pure standard
library, so it loads in the orchestration environment and runs in any Python with pytest.
Store (obs-db) parts are step 0b's.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

PKG_NAME = "demo_utils"
TOOL_NAME = "band_pa_summary"
OTHER_PKG = "other_utils"
#: The name of the toolkit whose manifest keys the fixture writes by default (``<name>.<pkg>.<tool>``).
TOOLKIT_NAME = "research_toolkit"
ENV_ID = "demo"
BRANCH = "ai"

LIB_SOURCE = '''\
"""Two-band count ratio and position-angle statistics over a reduced product CSV.

Pure standard library, so the module imports in the orchestration environment as well as the
analysis one. The high-level entry point a person calls is :func:`summarize`; the arithmetic
helpers beneath it are separately testable.
"""

from __future__ import annotations

import csv
import math


def read_band_rows(csv_path):
    """Rows of ``{obsid, low_counts, high_counts, pa_deg, pa_err_deg}`` from a product CSV.

    Lines beginning with ``#`` are provenance header lines and are skipped.
    """
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as handle:
        payload = [line for line in handle if not line.startswith("#")]
    for row in csv.DictReader(payload):
        rows.append({
            "obsid": row["obsid"],
            "low_counts": float(row["low_counts"]),
            "high_counts": float(row["high_counts"]),
            "pa_deg": float(row["pa_deg"]),
            "pa_err_deg": float(row["pa_err_deg"]),
        })
    return rows


def band_ratio(low_counts, high_counts):
    """Hardness ratio ``(H - L) / (H + L)`` and its Poisson-propagated uncertainty.

    With independent Poisson counts the uncertainty is ``2 * sqrt(H * L) / (H + L) ** 1.5``.
    """
    total = low_counts + high_counts
    if total <= 0:
        raise ValueError("band ratio needs a positive total count")
    ratio = (high_counts - low_counts) / total
    error = 2.0 * math.sqrt(high_counts * low_counts) / total ** 1.5
    return ratio, error


def wrap_pa(angle_deg):
    """Wrap a position angle into ``[-90, 90)`` degrees."""
    return ((angle_deg + 90.0) % 180.0) - 90.0


def mean_position_angle(angles_deg, errors_deg):
    """Error-weighted mean position angle in degrees, on the 180-degree circle.

    A position angle is defined modulo 180 degrees, so each angle is doubled before the
    weighted vector average and the result halved: the mean is then independent of where the
    branch cut falls. Entries with a non-positive error carry no weight.
    """
    sin_sum = 0.0
    cos_sum = 0.0
    weight_sum = 0.0
    for angle, error in zip(angles_deg, errors_deg):
        if not error > 0:
            continue
        weight = 1.0 / (error * error)
        doubled = 2.0 * math.radians(angle)
        sin_sum += weight * math.sin(doubled)
        cos_sum += weight * math.cos(doubled)
        weight_sum += weight
    if weight_sum <= 0:
        raise ValueError("mean position angle needs at least one finite, positive error")
    # A vanishing resultant means the doubled-angle vectors cancel (equal weights on
    # orthogonal angles), so the mean is undefined: atan2 would return an angle determined by
    # floating-point residue, which is a plausible-looking number with no meaning behind it.
    resultant = math.hypot(sin_sum, cos_sum)
    if resultant <= 1e-12 * weight_sum:
        raise ValueError("mean position angle is undefined: the weighted resultant vanishes")
    return wrap_pa(0.5 * math.degrees(math.atan2(sin_sum, cos_sum)))


def summarize(csv_path, min_total_counts=0.0):
    """Summarize one reduced product CSV: band ratio and mean position angle.

    Rows whose total counts fall below ``min_total_counts`` are excluded and counted, so a
    caller can see how much of the input the summary rests on.
    """
    rows = read_band_rows(csv_path)
    kept = [r for r in rows if r["low_counts"] + r["high_counts"] >= min_total_counts]
    if not kept:
        raise ValueError(f"no row of {csv_path} reaches min_total_counts={min_total_counts}")
    low = sum(r["low_counts"] for r in kept)
    high = sum(r["high_counts"] for r in kept)
    ratio, ratio_error = band_ratio(low, high)
    mean_pa = mean_position_angle(
        [r["pa_deg"] for r in kept], [r["pa_err_deg"] for r in kept]
    )
    return {
        "source": str(csv_path),
        "n_rows_read": len(rows),
        "n_rows_used": len(kept),
        "low_counts": low,
        "high_counts": high,
        "ratio": ratio,
        "ratio_err": ratio_error,
        "mean_pa_deg": mean_pa,
    }
'''

MANIFEST_SOURCE = '''\
"""The AI-half: a pure-data tool manifest (no domain imports), per CLAUDE.md §9.2."""

TOOL_MANIFEST: dict = {
    "%(toolkit)s.demo_utils.%(tool)s": {
        "module": "demo_utils.lib.demo_help",
        "fn": "summarize",
        "description": (
            "Band ratio and error-weighted mean position angle from a reduced product CSV."
        ),
        "args_schema": {
            "type": "object",
            "properties": {
                "csv_path": {
                    "kind": "path",
                    "type": "string",
                    "description": "Path of the reduced product CSV to summarize.",
                },
                "min_total_counts": {
                    "kind": "parameter",
                    "type": "number",
                    "default": 0.0,
                    "description": "Exclude rows whose two bands sum below this count.",
                },
            },
            "required": ["csv_path"],
        },
    },
%(extra)s}
'''

TEST_SOURCE = '''\
"""Committed Layer-B checks for the band/position-angle summary (CLAUDE.md §9.4).

Known-input, independent-recompute, and metamorphic families: each arithmetic result is
checked against a value derived by hand or by a separate computation, and the position-angle
mean is checked for the branch-cut invariance that defines it.

Every fixture row carries a marked identifier (``synth<YYMMDD>-<hash>``, §9.5), so a fixture
number is never presented as a measurement of a real observation.
"""

import math

from demo_utils.lib.demo_help import (
    band_ratio,
    mean_position_angle,
    read_band_rows,
    summarize,
    wrap_pa,
)

ROWS = [
    ("synth260817-4d1f7a", 400.0, 900.0, 20.0, 2.0),
    ("synth260817-9c3b02", 500.0, 700.0, 26.0, 3.0),
    ("synth260817-e57d16", 300.0, 300.0, 14.0, 4.0),
]


def write_csv(path, rows=ROWS):
    lines = ["# produced_by=test_demo_summary", "obsid,low_counts,high_counts,pa_deg,pa_err_deg"]
    lines += [f"{o},{lo},{hi},{pa},{err}" for o, lo, hi, pa, err in rows]
    path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
    return str(path)


def test_band_ratio_matches_hand_computed_value():
    ratio, error = band_ratio(400.0, 900.0)
    assert ratio == 500.0 / 1300.0
    assert error == 2.0 * math.sqrt(900.0 * 400.0) / 1300.0 ** 1.5


def test_band_ratio_error_matches_independent_propagation():
    low, high = 400.0, 900.0
    total = low + high
    d_high = 2.0 * low / total ** 2
    d_low = -2.0 * high / total ** 2
    expected = math.sqrt(d_high ** 2 * high + d_low ** 2 * low)
    _, error = band_ratio(low, high)
    assert abs(error - expected) < 1e-12


def test_band_ratio_rejects_empty_bands():
    try:
        band_ratio(0.0, 0.0)
    except ValueError:
        return
    raise AssertionError("a zero total count must raise")


def test_mean_position_angle_matches_hand_computed_value():
    angles = [10.0, 50.0]
    errors = [1.0, 1.0]
    sin_sum = math.sin(math.radians(20.0)) + math.sin(math.radians(100.0))
    cos_sum = math.cos(math.radians(20.0)) + math.cos(math.radians(100.0))
    expected = 0.5 * math.degrees(math.atan2(sin_sum, cos_sum))
    assert abs(mean_position_angle(angles, errors) - expected) < 1e-10


def test_mean_position_angle_is_invariant_under_a_180_degree_shift():
    angles = [10.0, 50.0, 80.0]
    errors = [1.0, 2.0, 3.0]
    shifted = [angles[0] + 180.0] + angles[1:]
    assert abs(mean_position_angle(angles, errors) - mean_position_angle(shifted, errors)) < 1e-10


def test_mean_position_angle_weights_the_precise_measurement():
    angles = [10.0, 70.0]
    precise = mean_position_angle(angles, [0.5, 8.0])
    assert abs(precise - 10.0) < abs(precise - 70.0)


def test_mean_position_angle_ignores_non_positive_errors():
    angles = [10.0, 70.0]
    assert mean_position_angle(angles, [1.0, 0.0]) == mean_position_angle([10.0], [1.0])


def test_wrap_pa_maps_onto_the_half_open_interval():
    assert wrap_pa(90.0) == -90.0
    assert wrap_pa(-100.0) == 80.0
    assert wrap_pa(30.0) == 30.0


def test_read_band_rows_round_trip_skips_the_provenance_header(tmp_path):
    rows = read_band_rows(write_csv(tmp_path / "product.csv"))
    assert [r["obsid"] for r in rows] == [r[0] for r in ROWS]
    assert rows[0]["high_counts"] == 900.0


def test_summarize_uses_every_row_and_matches_a_direct_recompute(tmp_path):
    out = summarize(write_csv(tmp_path / "product.csv"))
    assert out["n_rows_read"] == len(ROWS)
    assert out["n_rows_used"] == len(ROWS)
    expected_ratio, expected_ratio_err = band_ratio(1200.0, 1900.0)
    assert out["ratio"] == expected_ratio
    assert out["ratio_err"] == expected_ratio_err
    assert out["mean_pa_deg"] == mean_position_angle(
        [r[3] for r in ROWS], [r[4] for r in ROWS]
    )


def test_summarize_error_matches_an_independent_propagation(tmp_path):
    """The reported uncertainty is checked on its own, not carried along untested."""
    out = summarize(write_csv(tmp_path / "product.csv"))
    low, high = 1200.0, 1900.0
    total = low + high
    d_high = 2.0 * low / total ** 2
    d_low = -2.0 * high / total ** 2
    expected = math.sqrt(d_high ** 2 * high + d_low ** 2 * low)
    assert abs(out["ratio_err"] - expected) < 1e-12


def test_mean_position_angle_rejects_a_vanishing_resultant():
    """Equal weights on orthogonal angles cancel after doubling, so the mean is undefined."""
    try:
        mean_position_angle([0.0, 90.0], [1.0, 1.0])
    except ValueError:
        return
    raise AssertionError("a vanishing weighted resultant must raise")


def test_repro_summary_is_stable_across_repeated_calls(tmp_path):
    """Reproducibility check (§9.4): the core outputs agree within tolerance on a re-run."""
    path = write_csv(tmp_path / "product.csv")
    first = summarize(path)
    second = summarize(path)
    for key in ("ratio", "ratio_err", "mean_pa_deg"):
        assert abs(first[key] - second[key]) <= 1e-12 * max(1.0, abs(first[key]))


def test_summarize_excludes_rows_below_the_count_threshold(tmp_path):
    out = summarize(write_csv(tmp_path / "product.csv"), min_total_counts=1000.0)
    assert out["n_rows_used"] == 2
    assert out["n_rows_read"] == 3


def test_summarize_rejects_a_threshold_that_keeps_nothing(tmp_path):
    try:
        summarize(write_csv(tmp_path / "product.csv"), min_total_counts=1e9)
    except ValueError:
        return
    raise AssertionError("a threshold that excludes every row must raise")
'''

# ----------------------------------------------------------------------------
# The rest of the fixture toolkit: every argument kind, products, a figure, scratch
# ----------------------------------------------------------------------------

PRODUCTS_SOURCE = '''\
"""Band tables, a ratio plot and a row count over a reduced product CSV.

The tools a person calls beside ``demo_help.summarize``: one writes a table it declares as a
product, one draws a figure it declares, one counts rows through a scratch copy it removes.
Standard library only, so the module imports in any Python.
"""

from __future__ import annotations

import csv
import hashlib
import os
import struct
import tempfile
import time
import zlib
from pathlib import Path

from demo_utils.lib.demo_help import band_ratio, read_band_rows


def write_band_table(csv_path, out_dir, min_total_counts=0.0):
    """Per-row band ratio and its error, written to ``<out_dir>/band_table.csv``.

    Rows whose two bands sum below ``min_total_counts`` are left out. The table is declared as a
    product, and the ratios are returned in row order.
    """
    rows = [r for r in read_band_rows(csv_path) if r["low_counts"] + r["high_counts"] >= min_total_counts]
    table = Path(out_dir) / "band_table.csv"
    ratios = []
    with open(table, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["obsid", "ratio", "ratio_err"])
        for row in rows:
            ratio, error = band_ratio(row["low_counts"], row["high_counts"])
            writer.writerow([row["obsid"], repr(ratio), repr(error)])
            ratios.append(ratio)
    return {
        "stdout": f"{len(rows)} rows written to {table}",
        "ratios": ratios,
        "products": [{"path": str(table), "caption": "Per-row band ratio and its error"}],
    }


def _png(width, height, rows):
    """A grey-scale PNG (8-bit, no filter) from ``rows`` of pixel bytes."""
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\\x00" + bytes(row) for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def plot_band_ratio(ratios, save_path, title="Band ratio per row"):
    """Draw each ratio in ``[-1, 1]`` as a bar of a grey-scale PNG at ``save_path``.

    The figure is declared in the return, with its caption and the hash of the written bytes.
    """
    height, bar = 64, 8
    heights = [round((max(-1.0, min(1.0, r)) + 1.0) / 2.0 * height) for r in ratios]
    width = bar * max(1, len(heights))
    rows = [[0 if i // bar < len(heights) and height - y <= heights[i // bar] else 255 for i in range(width)]
            for y in range(height)]
    data = _png(width, height, rows)
    Path(save_path).write_bytes(data)
    return {
        "stdout": f"Figure saved: {save_path}",
        "figures": [{
            "path": str(save_path),
            "caption": title,
            "mime_type": "image/png",
            "produced_by": "demo_utils.lib.demo_products.plot_band_ratio",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sha256": hashlib.sha256(data).hexdigest(),
        }],
    }


def count_rows(csv_path, min_counts, label="rows"):
    """How many rows reach ``min_counts`` in total, counted from a scratch copy of the CSV.

    The copy lives in a temporary folder the call removes; nothing it writes is a product.
    """
    with tempfile.TemporaryDirectory() as scratch:
        copy = os.path.join(scratch, "rows.csv")
        with open(csv_path, encoding="utf-8") as source, open(copy, "w", encoding="utf-8") as target:
            target.write(source.read())
        kept = [r for r in read_band_rows(copy) if r["low_counts"] + r["high_counts"] >= min_counts]
    return {"stdout": f"{label}: {len(kept)}"}
'''

PRODUCTS_MANIFEST = '''\
    "%(toolkit)s.demo_utils.write_band_table": {
        "module": "demo_utils.lib.demo_products",
        "fn": "write_band_table",
        "description": "Per-row band ratio and its error from a reduced product CSV, as a table.",
        "args_schema": {
            "type": "object",
            "properties": {
                "csv_path": {
                    "kind": "path",
                    "type": "string",
                    "description": "Path of the reduced product CSV.",
                },
                "out_dir": {
                    "kind": "output",
                    "shape": "folder",
                    "type": "string",
                    "description": "Folder the table is written to.",
                },
                "min_total_counts": {
                    "kind": "parameter",
                    "type": "number",
                    "default": 0.0,
                    "description": "Leave out rows whose two bands sum below this count.",
                },
            },
            "required": ["csv_path", "out_dir"],
        },
    },
    "%(toolkit)s.demo_utils.plot_band_ratio": {
        "module": "demo_utils.lib.demo_products",
        "fn": "plot_band_ratio",
        "description": "Bar plot of per-row band ratios, as a PNG figure.",
        "args_schema": {
            "type": "object",
            "properties": {
                "ratios": {
                    "kind": "data",
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "The ratios write_band_table returned.",
                },
                "save_path": {
                    "kind": "output",
                    "shape": "file",
                    "type": "string",
                    "default": "band_ratio.png",
                    "description": "File the PNG is written to.",
                },
                "title": {
                    "kind": "setting",
                    "type": "string",
                    "description": "The figure's caption.",
                },
            },
            "required": ["ratios"],
        },
    },
    "%(toolkit)s.demo_utils.count_rows": {
        "module": "demo_utils.lib.demo_products",
        "fn": "count_rows",
        "description": "How many rows of a reduced product CSV reach a total count.",
        "args_schema": {
            "type": "object",
            "properties": {
                "csv_path": {
                    "kind": "path",
                    "type": "string",
                    "description": "Path of the reduced product CSV.",
                },
                "min_counts": {
                    "kind": "parameter",
                    "type": "number",
                    "description": "The total count a row must reach.",
                },
                "label": {
                    "kind": "setting",
                    "type": "string",
                    "description": "Word the count is printed after.",
                },
            },
            "required": ["csv_path", "min_counts"],
        },
    },
'''

PRODUCTS_TEST_SOURCE = '''\
"""Checks for the table, plot and row-count tools, on a marked synthetic product (§9.5)."""

import csv
import hashlib
import os
import struct

from demo_utils.lib.demo_help import band_ratio
from demo_utils.lib.demo_products import count_rows, plot_band_ratio, write_band_table
from demo_utils.tests.test_demo_summary import ROWS, write_csv


def test_the_band_table_holds_each_rows_ratio_and_is_declared(tmp_path):
    out = write_band_table(write_csv(tmp_path / "product.csv"), str(tmp_path))
    [product] = out["products"]
    with open(product["path"], newline="", encoding="utf-8") as handle:
        table = list(csv.DictReader(handle))
    expected = [band_ratio(lo, hi)[0] for _, lo, hi, _, _ in ROWS]
    assert [float(r["ratio"]) for r in table] == expected == out["ratios"]


def test_the_band_table_leaves_out_rows_below_the_threshold(tmp_path):
    out = write_band_table(write_csv(tmp_path / "product.csv"), str(tmp_path), min_total_counts=1000.0)
    assert len(out["ratios"]) == 2


def test_the_plot_is_a_png_whose_declared_hash_is_its_bytes(tmp_path):
    out = plot_band_ratio([-0.5, 0.0, 0.5], str(tmp_path / "ratio.png"))
    [figure] = out["figures"]
    data = open(figure["path"], "rb").read()
    assert data.startswith(b"\\x89PNG") and struct.unpack(">II", data[16:24]) == (24, 64)
    assert figure["sha256"] == hashlib.sha256(data).hexdigest()


def test_count_rows_counts_the_rows_that_reach_the_total(tmp_path):
    path = write_csv(tmp_path / "product.csv")
    assert count_rows(path, 1000.0, label="kept") == {"stdout": "kept: 2"}
    assert os.listdir(tmp_path) == ["product.csv"]
'''

CLASSES_TEST_SOURCE = '''\
"""One test of each marked class the root conftest names: ``slow`` and ``live``."""

import socket

import pytest

from demo_utils.lib.demo_help import band_ratio


@pytest.mark.slow
def test_band_ratio_error_shrinks_as_counts_grow():
    errors = [band_ratio(n, 2.0 * n)[1] for n in (10.0 ** k for k in range(1, 7))]
    assert errors == sorted(errors, reverse=True)


@pytest.mark.live
def test_the_host_resolves_its_own_name():
    assert socket.gethostbyname("localhost")
'''

OTHER_LIB_SOURCE = '''\
"""Merge band tables into one CSV. Standard library only."""

from __future__ import annotations

import csv
from pathlib import Path


def merge_tables(tables, out_prefix):
    """Concatenate the rows of ``tables`` (CSV paths sharing one header) into ``<out_prefix>.csv``.

    The merged table is declared as a product.
    """
    header, rows = None, []
    for table in tables:
        with open(table, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            first = next(reader)
            if header is not None and first != header:
                raise ValueError(f"{table} has header {first}, not {header}")
            header = first
            rows.extend(reader)
    merged = Path(f"{out_prefix}.csv")
    with open(merged, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header or [])
        writer.writerows(rows)
    return {
        "stdout": f"{len(rows)} rows from {len(tables)} tables written to {merged}",
        "products": [{"path": str(merged), "caption": "Rows of every merged table"}],
    }
'''

OTHER_MANIFEST_SOURCE = '''\
"""The AI-half: a pure-data tool manifest (no domain imports)."""

TOOL_MANIFEST: dict = {
    "%(toolkit)s.other_utils.merge_tables": {
        "module": "other_utils.lib.other_help",
        "fn": "merge_tables",
        "description": "Merge band tables that share one header into one CSV table.",
        "args_schema": {
            "type": "object",
            "properties": {
                "tables": {
                    "kind": "data",
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The tables write_band_table declared.",
                },
                "out_prefix": {
                    "kind": "output",
                    "shape": "prefix",
                    "type": "string",
                    "default": "merged",
                    "description": "File-name prefix of the merged table.",
                },
            },
            "required": ["tables"],
        },
    },
}
'''

OTHER_TEST_SOURCE = '''\
"""Checks for merging band tables."""

import csv

import pytest

from other_utils.lib.other_help import merge_tables


def _table(path, rows, header=("obsid", "ratio")):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows([header, *rows])
    return str(path)


def test_merge_keeps_every_row_in_order(tmp_path):
    a = _table(tmp_path / "a.csv", [("synth260817-4d1f7a", "0.1")])
    b = _table(tmp_path / "b.csv", [("synth260817-9c3b02", "0.2"), ("synth260817-e57d16", "0.3")])
    out = merge_tables([a, b], str(tmp_path / "merged"))
    with open(out["products"][0]["path"], newline="", encoding="utf-8") as handle:
        merged = list(csv.reader(handle))
    assert merged[0] == ["obsid", "ratio"] and [r[1] for r in merged[1:]] == ["0.1", "0.2", "0.3"]


def test_merge_refuses_tables_with_different_headers(tmp_path):
    a = _table(tmp_path / "a.csv", [("synth260817-4d1f7a", "0.1")])
    b = _table(tmp_path / "b.csv", [("synth260817-9c3b02", "0.2")], header=("obsid", "pa"))
    with pytest.raises(ValueError):
        merge_tables([a, b], str(tmp_path / "merged"))
'''

ROOT_CONFTEST = '''\
"""Test classes for the whole toolkit, by marker.

* unmarked: fast and deterministic, synthetic inputs, no network;
* ``slow``: takes minutes; runs by default, ``pytest -m "not slow"`` leaves it out;
* ``live``: reaches the network or an external service; skipped unless ``--live`` or
  ``TOOLKIT_LIVE=1``;
* ``data("<name>")``: reads the named data set from the folder ``TOOLKIT_DATA_<NAME>`` names;
  skipped with the reason when it is unset or absent.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", default=False,
                     help="also run tests marked live (network or external services)")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: takes minutes")
    config.addinivalue_line("markers", "live: reaches the network or an external service; opt-in")
    config.addinivalue_line("markers", "data(name): reads the data set the folder TOOLKIT_DATA_<NAME> names")


def pytest_collection_modifyitems(config, items):
    live = config.getoption("--live") or os.environ.get("TOOLKIT_LIVE") == "1"
    for item in items:
        if item.get_closest_marker("live") and not live:
            item.add_marker(pytest.mark.skip(reason="live: run with --live or TOOLKIT_LIVE=1"))
        for mark in item.iter_markers("data"):
            name = str(mark.args[0]) if mark.args else ""
            variable = "TOOLKIT_DATA_" + name.upper()
            folder = os.environ.get(variable)
            if not folder or not os.path.isdir(folder):
                item.add_marker(pytest.mark.skip(reason="data: %s is absent (%s %s)" % (
                    name, variable, "names no folder" if folder else "is unset")))
'''

ROOT_README = """\
# Toolkit

Band-ratio tools for reduced product tables. `demo_utils` summarises, tabulates and plots a
product; `other_utils` merges tables. Each package's `README.md` is its briefing; the tools run in
the environment `envs/demo/` defines.
"""


def _package_readme(name: str, purpose: str, reference: str) -> str:
    """A package briefing in the house style (research_toolkit ``CLAUDE.md`` §3)."""
    return (
        f"# {name}\n\n## Purpose\n\n{purpose}\n\n## Architecture\n\n`lib/` holds the code a person "
        "calls; `skills/__init__.py` is the pure-data manifest over it; `tests/` ships with it.\n\n"
        f"## Interfaces / Usage\n\nEach tool's manifest entry names its arguments and their kinds.\n\n"
        f"## Dependencies\n\nStandard library only. Runs in `envs/demo/`.\n\n## Reference\n\n{reference}\n\n"
        "## Changelog\n\n- 2026-11-09 — first version.\n"
    )


ENV_YAML = "purpose: the demo tools' stack\nbase: %(base)s\n"
ENV_ACTIVATE = "export DEMO_STACK_READY=1\n"


def write_base_package(
    repo: Path, *, tool_name: str = TOOL_NAME, with_test: bool = True,
    toolkit: str = TOOLKIT_NAME, extra_entries: str = "",
) -> Path:
    """Write the ``demo_utils`` package into ``repo`` and return its root.

    With the defaults this is gate_bench's synthetic base, byte for byte. ``toolkit`` names the
    toolkit in the manifest keys; ``extra_entries`` are further manifest entries (source text, each
    ending ``},``) placed after the summary tool.
    """
    pkg = Path(repo) / PKG_NAME
    (pkg / "lib").mkdir(parents=True, exist_ok=True)
    (pkg / "skills").mkdir(parents=True, exist_ok=True)
    (pkg / "tests").mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        '"""Demonstration toolkit package (CLAUDE.md §9.2 layout)."""\n\n'
        "from demo_utils.skills import TOOL_MANIFEST\n\n"
        '__all__ = ["TOOL_MANIFEST"]\n',
        encoding="utf-8",
    )
    (pkg / "lib" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "lib" / "demo_help.py").write_text(LIB_SOURCE, encoding="utf-8")
    (pkg / "skills" / "__init__.py").write_text(
        MANIFEST_SOURCE % {"tool": tool_name, "toolkit": toolkit, "extra": extra_entries}, encoding="utf-8")
    (pkg / "tests" / "__init__.py").write_text("", encoding="utf-8")
    if with_test:
        (pkg / "tests" / "test_demo_summary.py").write_text(TEST_SOURCE, encoding="utf-8")
    return pkg


def git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return its standard output; raises when git fails."""
    out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    return out.stdout


def init_repo(dest: Path, readme: str, *, branch: str | None = None) -> Path:
    """A git repository at ``dest`` with one commit: ``README.md`` and the toolkit's ignore rules.

    The committer is a plausible operator (gate_bench's reviewer reads ``.git``, 2026-08-28 audit).
    ``branch``: the branch it starts on (git's default when ``None``).
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    git(dest, "init", "-q", *(("-b", branch) if branch else ()))
    git(dest, "config", "user.email", "analyst@localhost")
    git(dest, "config", "user.name", "analyst")
    (dest / "README.md").write_text(readme, encoding="utf-8")
    # The toolkit's ignore rules, so byte-compiled output never reads as a change.
    (dest / ".gitignore").write_text("*/__pycache__/\n*.pyc\n", encoding="utf-8")
    git(dest, "add", "-A")
    git(dest, "commit", "-qm", "base")
    return dest


def build_toolkit(
    dest: Path, *, env_base: str, toolkit: str = TOOLKIT_NAME, branch: str = BRANCH,
) -> Path:
    """The whole fixture toolkit at ``dest``, committed clean on ``branch``; returns its root.

    ``env_base``: the human environment ``envs/demo/`` layers on (a derived definition): the name
    of the configured analysis environment (:func:`write_config`'s ``analysis_prefix`` folder name).
    ``toolkit``: the toolkit name in every manifest key.
    """
    root = init_repo(dest, ROOT_README, branch=branch)
    (root / "conftest.py").write_text(ROOT_CONFTEST, encoding="utf-8")
    demo = write_base_package(root, toolkit=toolkit, extra_entries=PRODUCTS_MANIFEST % {"toolkit": toolkit})
    (demo / "lib" / "demo_products.py").write_text(PRODUCTS_SOURCE, encoding="utf-8")
    (demo / "tests" / "test_demo_products.py").write_text(PRODUCTS_TEST_SOURCE, encoding="utf-8")
    (demo / "tests" / "test_demo_classes.py").write_text(CLASSES_TEST_SOURCE, encoding="utf-8")
    (demo / "README.md").write_text(_package_readme(
        PKG_NAME, "Band ratio, position angle, a band table, a ratio plot and a row count over a reduced "
        "product CSV.",
        "- `summarize`, `write_band_table`, `plot_band_ratio`, `count_rows` (`lib/`).",
    ), encoding="utf-8")
    other = root / OTHER_PKG
    for sub in ("lib", "skills", "tests"):
        (other / sub).mkdir(parents=True)
        (other / sub / "__init__.py").write_text("", encoding="utf-8")
    (other / "__init__.py").write_text(
        '"""Table merging (toolkit layout)."""\n\nfrom other_utils.skills import TOOL_MANIFEST\n\n'
        '__all__ = ["TOOL_MANIFEST"]\n', encoding="utf-8")
    (other / "lib" / "other_help.py").write_text(OTHER_LIB_SOURCE, encoding="utf-8")
    (other / "skills" / "__init__.py").write_text(OTHER_MANIFEST_SOURCE % {"toolkit": toolkit}, encoding="utf-8")
    (other / "tests" / "test_merge_tables.py").write_text(OTHER_TEST_SOURCE, encoding="utf-8")
    (other / "README.md").write_text(_package_readme(
        OTHER_PKG, "Merge band tables that share one header.", "- `merge_tables` (`lib/other_help.py`)."),
        encoding="utf-8")
    for pkg in (demo, other):
        (pkg / "tool_config.yaml").write_text(f"env: {ENV_ID}\ndefault_call_timeout_s: 60\n", encoding="utf-8")
    env = root / "envs" / ENV_ID
    env.mkdir(parents=True)
    (env / "env.yaml").write_text(ENV_YAML % {"base": env_base}, encoding="utf-8")
    (env / "activate.sh").write_text(ENV_ACTIVATE, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture toolkit")
    return root


def write_config(
    config_dir: Path, toolkit_root: Path, *, analysis_prefix: Path, allow: tuple[Path, ...] = (),
    **overrides,
) -> Path:
    """A configuration directory for a run on the toolkit at ``toolkit_root``; returns it.

    ``orchestration.yaml`` holds the keys the toolkit's entry points read: the toolkit contract
    (``toolkits:``, naming ``toolkit_root``, its packages, template, authoring branch and default
    environment), the human environment ``envs/demo/`` layers on (``human_envs``: the
    ``analysis_prefix`` folder's name → that prefix), no externals, ``allow`` (and the prefix,
    read-only) as the jail's allow-list;
    ``overrides`` replace or add keys (``None`` removes one). ``code_gate.yaml`` is the
    repository's. Point a process at it with ``AUTO_RESEARCH_CONFIG_DIR``.
    """
    from auto_research.config_paths import repo_config_dir

    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    prefix = str(Path(analysis_prefix))
    orchestration = {
        "toolkits": [{"name": TOOLKIT_NAME, "root": str(toolkit_root), "package_glob": "*_utils",
                      "template_package": PKG_NAME, "authoring_branch": BRANCH, "default_env": ENV_ID}],
        "human_envs": {Path(prefix).name: prefix},
        "env_externals": {},
        "agent_fs_allow": [*(str(p) for p in allow), prefix],
        "agent_fs_readonly": [prefix],
        "disabled_tools": [],
        "sandbox_call_timeout_s": 120,
        "gate_test_timeout_s": 300,
    }
    for key, value in overrides.items():
        if value is None:
            orchestration.pop(key, None)
        else:
            orchestration[key] = value
    (config_dir / "orchestration.yaml").write_text(yaml.safe_dump(orchestration, sort_keys=False), encoding="utf-8")
    (config_dir / "code_gate.yaml").write_bytes((repo_config_dir() / "code_gate.yaml").read_bytes())
    return config_dir
