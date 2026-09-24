"""Provenance: runtime-minted receipts and the check that binds report prose to them."""

from auto_research.provenance.bindings import (
    ATTRIBUTION_KINDS,
    Attribution,
    Binding,
    BindingResult,
    ReceiptLookup,
    bounded_find,
    check_bindings,
    check_report_fields,
    normalise,
    paper_attributions,
    parse_bindings,
    render_bindings,
)
from auto_research.schemas.artifacts import PaperAttribution
from auto_research.provenance.store import ReceiptStore, ScopedLookup, is_synthetic_path, sha256_file

__all__ = [
    "ATTRIBUTION_KINDS",
    "Attribution",
    "PaperAttribution",
    "paper_attributions",
    "Binding",
    "BindingResult",
    "ReceiptLookup",
    "ReceiptStore",
    "ScopedLookup",
    "bounded_find",
    "check_bindings",
    "check_report_fields",
    "is_synthetic_path",
    "normalise",
    "parse_bindings",
    "render_bindings",
    "sha256_file",
]
