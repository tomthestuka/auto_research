"""Cumulative spend rollup over the local cost ledger.

Answers "how much have I burnt" without a provider admin panel ([I5]): the ledger accretes one row
per LLM call across **all** runs (``<runtime_dir>/cost_ledger.jsonl``), priced litellm-first with a
config fallback (see ``costs.py``). This aggregates it — total, and by provider / model / role / day
— with an optional ``--since`` cutoff (e.g. "this month").

    python -m auto_research.observability.usage [--since YYYY-MM-DD] [--ledger PATH] [--top N]

The estimate is against **list prices**; it cannot reconcile to an org's actual negotiated bill
(that needs an admin key). It is the best obtainable from the client side, and accurate to list
prices once cache/reasoning tokens are counted.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from auto_research.observability.costs import CostLedgerEntry, cost_ledger_path


def _parse_since(value: str | None) -> datetime | None:
    """Parse a ``--since`` value (``YYYY-MM-DD`` or full ISO-8601) into an aware UTC datetime."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text if "T" in text else f"{text}T00:00:00")
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def load_entries(path: Path, since: datetime | None = None) -> list[CostLedgerEntry]:
    """Read + validate ledger rows (old rows without cache fields validate via defaults),
    keeping those at or after ``since``."""
    entries: list[CostLedgerEntry] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                entry = CostLedgerEntry.model_validate_json(line)
            except Exception:  # noqa: BLE001 — skip a corrupt/legacy-incompatible row, never abort
                continue
            if since is not None and entry.created_at < since:
                continue
            entries.append(entry)
    return entries


def _group_totals(entries: list[CostLedgerEntry], key) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for entry in entries:
        totals[key(entry)] += entry.total_cost_usd
    return totals


def _print_group(title: str, totals: dict[str, float], grand: float, top: int | None = None) -> None:
    rows = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    if top:
        rows = rows[:top]
    print(f"\n{title}")
    for name, cost in rows:
        pct = 100 * cost / grand if grand else 0.0
        print(f"  {name:<34} ${cost:>10.4f}  ({pct:4.1f}%)")


def render_report(entries: list[CostLedgerEntry], since: datetime | None, top: int | None) -> str:
    """Build the text rollup (returned as a string so it is testable)."""
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if not entries:
            print("No cost ledger rows found for the requested window.")
            return buf.getvalue()

        grand = sum(e.total_cost_usd for e in entries)
        toks = {
            "input": sum(e.input_tokens for e in entries),
            "output": sum(e.output_tokens for e in entries),
            "cache_read": sum(e.cache_read_tokens for e in entries),
            "cache_write": sum(e.cache_write_tokens for e in entries),
            "reasoning": sum(e.reasoning_tokens for e in entries),
        }
        breakdown = {
            "input": sum(e.input_cost_usd for e in entries),
            "output": sum(e.output_cost_usd for e in entries),
            "cache_read": sum(e.cache_read_cost_usd for e in entries),
            "cache_write": sum(e.cache_write_cost_usd for e in entries),
        }
        first = min(e.created_at for e in entries)
        last = max(e.created_at for e in entries)
        window = f"since {since.date()}" if since else "all time"
        print(f"auto_research spend rollup ({window}) — estimate vs. list prices")
        print(f"  {len(entries)} calls  |  {first.date()} → {last.date()}")
        print(f"  TOTAL: ${grand:.4f}")
        print(
            "  by class: "
            + "  ".join(f"{k}=${v:.4f}" for k, v in breakdown.items())
        )
        print(
            "  tokens:   "
            + "  ".join(f"{k}={v:,}" for k, v in toks.items())
        )
        # If everything priced via config (litellm absent/unknown models), say so — the number
        # is only as good as config/models.yaml priceMTk then.
        sources = {e.price_source for e in entries}
        if sources == {"config"}:
            print("  (all rows priced from config/models.yaml priceMTk — litellm map not consulted)")

        _print_group("By provider:", _group_totals(entries, lambda e: e.provider), grand)
        _print_group("By model:", _group_totals(entries, lambda e: e.model), grand, top)
        _print_group("By role:", _group_totals(entries, lambda e: e.role), grand)
        _print_group(
            "By day:", _group_totals(entries, lambda e: e.created_at.date().isoformat()), grand, top
        )
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Cumulative auto_research LLM spend rollup.")
    parser.add_argument("--since", help="Only count calls at/after this date (YYYY-MM-DD or ISO-8601).")
    parser.add_argument("--ledger", help="Path to cost_ledger.jsonl (default: runtime_dir).")
    parser.add_argument(
        "--top", type=int, default=15, help="Cap the by-model / by-day tables (default 15)."
    )
    args = parser.parse_args()

    path = Path(args.ledger).expanduser() if args.ledger else cost_ledger_path()
    if not path.exists():
        raise SystemExit(f"No cost ledger at {path}")
    entries = load_entries(path, _parse_since(args.since))
    print(render_report(entries, _parse_since(args.since), args.top), end="")


if __name__ == "__main__":
    main()
