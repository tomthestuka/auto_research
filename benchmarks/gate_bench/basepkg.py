"""The synthetic base: a minimal, correct ``demo_utils`` toolkit package, plus the file
editing helpers every mutant builds its changeset with.

One of the corpus's two bases (CLAUDE.md §9.2 layout; the other is the real toolkit at a
pinned ref). The package is deliberately *real*: pure standard library, so it runs anywhere,
but with genuine arithmetic (a two-band count ratio with propagated errors, and an
error-weighted position angle on the 180-degree circle) that a defect can be planted in and
a committed test can actually detect. Written unmodified it passes every gate layer, which is
what makes it usable as the clean control.

Self-contained by design: a benchmark must not import from ``tests/``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PKG_NAME = "demo_utils"
TOOL_NAME = "band_pa_summary"

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
    "research_toolkit.demo_utils.%(tool)s": {
        "module": "demo_utils.lib.demo_help",
        "fn": "summarize",
        "description": (
            "Band ratio and error-weighted mean position angle from a reduced product CSV."
        ),
        "args_schema": {
            "type": "object",
            "properties": {
                "csv_path": {
                    "type": "string",
                    "description": "Path of the reduced product CSV to summarize.",
                },
                "min_total_counts": {
                    "type": "number",
                    "description": "Exclude rows whose two bands sum below this count.",
                },
            },
            "required": ["csv_path"],
        },
    },
}
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
# Writing the base package
# ----------------------------------------------------------------------------


def write_base_package(repo: Path, *, tool_name: str = TOOL_NAME, with_test: bool = True) -> Path:
    """Write the correct ``demo_utils`` package into ``repo`` and return its root."""
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
    (pkg / "skills" / "__init__.py").write_text(MANIFEST_SOURCE % {"tool": tool_name}, encoding="utf-8")
    (pkg / "tests" / "__init__.py").write_text("", encoding="utf-8")
    if with_test:
        (pkg / "tests" / "test_demo_summary.py").write_text(TEST_SOURCE, encoding="utf-8")
    return pkg


def make_synthetic_repo(dest: Path) -> Path:
    """Initialise a throwaway git repo with one base commit (no toolkit package yet).

    The mutant then writes the package as untracked files, which is the shape the gate sees
    for a ``new_package`` authoring: whole-file scan scope.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, "init", "-q")
    _git(dest, "config", "user.email", "gate-bench@localhost")
    _git(dest, "config", "user.name", "gate-bench")
    (dest / "README.md").write_text("Throwaway base for the gate benchmark.\n", encoding="utf-8")
    # Mirror the real toolkit's ignore rules, so byte-compiled output does not masquerade as
    # part of a changeset (it would otherwise inflate the post-commit sweep with artifacts the
    # production repository never sees).
    (dest / ".gitignore").write_text("*/__pycache__/\n*.pyc\n", encoding="utf-8")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "base")
    return dest


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return out.stdout


# ----------------------------------------------------------------------------
# Changeset editing helpers (used by every mutant builder, both bases)
# ----------------------------------------------------------------------------


def patch(path: Path, old: str, new: str, *, count: int = 1) -> None:
    """Replace ``old`` with ``new`` in ``path``, asserting it occurs exactly ``count`` times.

    Failing loudly on a missed anchor is what keeps a real-base mutant honest: if the pinned
    toolkit ref moves under the corpus, the mutant errors instead of silently planting
    nothing and recording a false miss.
    """
    text = Path(path).read_text(encoding="utf-8")
    found = text.count(old)
    if found != count:
        raise AssertionError(
            f"anchor occurs {found} time(s), expected {count}, in {path}: {old[:80]!r}"
        )
    Path(path).write_text(text.replace(old, new, count), encoding="utf-8")


def append(path: Path, text: str) -> None:
    """Append ``text`` to ``path`` (creating it if absent)."""
    path = Path(path)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(existing + text, encoding="utf-8")


def write(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, creating parent directories."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
