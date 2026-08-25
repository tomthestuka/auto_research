"""Deterministic tests for this benchmark, kept beside the instrument they check.

The repository's ``tests/`` tree is the pipeline's own suite. A benchmark is a separate
instrument with its own corpus and its own invariants, so its tests live with it: a change to a
mutant or a trial driver and the test that pins it move together, and the pipeline suite stays a
statement about the pipeline.
"""
