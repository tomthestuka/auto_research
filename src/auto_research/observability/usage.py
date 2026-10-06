"""Cumulative spend rollup over the monitor ledgers.

Answers "how much have I burnt" without a provider admin panel ([I5]). Since 2026-07-29 the cost
record lives **in each run's monitor JSONL** — one ``cost.call`` event per LLM call, priced
litellm-first with a config fallback (see ``costs.py``) and including the tool-authoring coder's
Agent-SDK spend. So the rollup reads monitor ledgers rather than a separate cost log, which makes
per-run attribution intrinsic (each event carries its ``run_id``) instead of a timestamp guess.

    python -m auto_research.observability.usage [--since YYYY-MM-DD] [--top N]
                                               [--jsonl PATH ...] [--runs] [--ledger-dir DIR]

By default it scans ``<runtime_dir>/runs/*/monitor.jsonl`` plus ``<runtime_dir>/monitors/*.jsonl``.
The estimate is against **list prices**; it cannot reconcile to an org's actual negotiated bill
(that needs an admin key). It is the best obtainable from the client side.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from auto_research.observability.costs import runtime_data_dir


def _parse_since(value: str | None) -> datetime | None:
    """Parse a ``--since`` value (``YYYY-MM-DD`` or full ISO-8601) into an aware UTC datetime."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text if "T" in text else f"{text}T00:00:00")
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def default_sources(data_dir: str | Path | None = None) -> list[Path]:
    """Return the monitor JSONLs a plain rollup reads: every run dir plus the default monitor dir."""
    root = Path(data_dir).expanduser() if data_dir is not None else runtime_data_dir()
    from auto_research.sandbox.workspace import _pre_split

    # Each run's ledger wherever it lives (beside the ledger since 2026-09-14, in the run folder
    # before); never a run-folder file of a newer run, which the jailed shell could have planted.
    # A read: nothing is created.
    runs = {
        run / "monitor.jsonl"
        for run in root.glob("runs/*") if run.is_dir() and _pre_split(run, root / "ledgers" / run.name)
    } | set(root.glob("ledgers/*/monitor.jsonl"))
    return sorted(p for p in runs if p.is_file()) + sorted(root.glob("monitors/*.jsonl"))


def load_cost_records(
    paths: Iterable[str | Path], since: datetime | None = None
) -> list[dict[str, Any]]:
    """Read ``cost.call`` payloads from monitor JSONLs into flat rows.

    Each row is the recorded :class:`~auto_research.observability.costs.CallCost` payload plus
    ``run_id`` and ``created_at`` from its event. Rows are de-duplicated on ``(run_id, sequence)``
    so a run whose ledger was copied (e.g. to ``logs/dev``) is never counted twice; corrupt lines
    are skipped rather than aborting a rollup.
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") != "cost.call":
                continue
            key = (str(event.get("run_id", "")), int(event.get("sequence", 0) or 0))
            if key in seen:
                continue
            created = _parse_since(event.get("created_at"))
            if since is not None and (created is None or created < since):
                continue
            seen.add(key)
            row = dict(event.get("data") or {})
            row["run_id"] = key[0]
            row["created_at"] = created
            rows.append(row)
    return rows


def _group_totals(rows: Sequence[dict[str, Any]], key) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[key(row)] += float(row.get("total_cost_usd", 0.0) or 0.0)
    return totals


def _print_group(title: str, totals: dict[str, float], grand: float, top: int | None = None) -> None:
    entries = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    if top:
        entries = entries[:top]
    print(f"\n{title}")
    for name, cost in entries:
        pct = 100 * cost / grand if grand else 0.0
        print(f"  {name:<34} ${cost:>10.4f}  ({pct:4.1f}%)")


def _day(row: dict[str, Any]) -> str:
    created = row.get("created_at")
    return created.date().isoformat() if isinstance(created, datetime) else "?"


def render_report(
    rows: Sequence[dict[str, Any]],
    since: datetime | None,
    top: int | None,
    *,
    by_run: bool = False,
) -> str:
    """Build the text rollup (returned as a string so it is testable)."""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if not rows:
            print("No cost.call events found for the requested window.")
            return buf.getvalue()

        grand = sum(float(r.get("total_cost_usd", 0.0) or 0.0) for r in rows)
        toks = {
            name: sum(int(r.get(f"{name}_tokens", 0) or 0) for r in rows)
            for name in ("input", "output", "cache_read", "cache_write", "reasoning")
        }
        breakdown = {
            name: sum(float(r.get(f"{name}_cost_usd", 0.0) or 0.0) for r in rows)
            for name in ("input", "output", "cache_read", "cache_write")
        }
        dates = [r["created_at"] for r in rows if isinstance(r.get("created_at"), datetime)]
        window = f"since {since.date()}" if since else "all time"
        print(f"auto_research spend rollup ({window}) — estimate vs. list prices")
        span = f"  |  {min(dates).date()} → {max(dates).date()}" if dates else ""
        runs = len({r.get("run_id") for r in rows})
        print(f"  {len(rows)} calls over {runs} run(s){span}")
        print(f"  TOTAL: ${grand:.4f}")
        print("  by class: " + "  ".join(f"{k}=${v:.4f}" for k, v in breakdown.items()))
        print("  tokens:   " + "  ".join(f"{k}={v:,}" for k, v in toks.items()))
        sources = {str(r.get("price_source", "config")) for r in rows}
        if sources == {"config"}:
            print("  (all rows priced from config/models.yaml priceMTk — litellm map not consulted)")
        if "claude_sdk" in sources:
            sdk = sum(
                float(r.get("total_cost_usd", 0.0) or 0.0)
                for r in rows
                if r.get("price_source") == "claude_sdk"
            )
            print(f"  (incl. ${sdk:.4f} reported directly by the coder's Agent SDK)")

        _print_group("By provider:", _group_totals(rows, lambda r: str(r.get("provider", "?"))), grand)
        _print_group("By model:", _group_totals(rows, lambda r: str(r.get("model", "?"))), grand, top)
        _print_group("By role:", _group_totals(rows, lambda r: str(r.get("role", "?"))), grand)
        if by_run:
            _print_group("By run:", _group_totals(rows, lambda r: str(r.get("run_id", "?"))), grand, top)
        _print_group("By day:", _group_totals(rows, _day), grand, top)
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Cumulative auto_research LLM spend rollup.")
    parser.add_argument("--since", help="Only count calls at/after this date (YYYY-MM-DD or ISO-8601).")
    parser.add_argument(
        "--jsonl", action="append", help="Monitor JSONL to read (repeatable; default: runtime_dir)."
    )
    parser.add_argument("--ledger-dir", help="Runtime data dir to scan instead of the configured one.")
    parser.add_argument("--runs", action="store_true", help="Add a per-run breakdown.")
    parser.add_argument(
        "--top", type=int, default=15, help="Cap the by-model / by-run / by-day tables (default 15)."
    )
    args = parser.parse_args()

    paths = (
        [Path(p).expanduser() for p in args.jsonl]
        if args.jsonl
        else default_sources(args.ledger_dir)
    )
    if not paths:
        raise SystemExit("No monitor JSONL found — pass --jsonl PATH or --ledger-dir DIR.")
    since = _parse_since(args.since)
    print(render_report(load_cost_records(paths, since), since, args.top, by_run=args.runs), end="")


if __name__ == "__main__":
    main()
