"""HTTP API: routers, schemas, auth dependency.

The `health` and `jobs` routers are mounted by `app.create_app`.
Schemas live in `schemas.py` and are kept separate from the
internal domain types in `models.py` so the wire contract is
independent from storage and pipeline code.
"""
