"""The data stores (Phase 3 step 0b): objects over the S3 contract plus a Postgres catalog.

``config`` reads the ``stores:`` block of ``orchestration.yaml`` (bound once per process),
``launch`` starts and stops a local store's servers, ``health`` holds the launch preflight items.
Each submodule is imported where it is used; this package imports nothing.
"""
