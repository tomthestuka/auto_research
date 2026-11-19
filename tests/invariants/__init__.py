"""The invariant freeze (Phase 3 step 0): CLAUDE.md §9.3 as one deterministic suite over today's code.

``pytest tests/invariants`` is the parity oracle for Phase 3 steps 1-7. Every test drives a role
through ``tests/invariants/drivers.py``, the only role-aware code here, and asserts on what the run
records: the filed report, the exception raised, the ledger, the receipts.
"""
