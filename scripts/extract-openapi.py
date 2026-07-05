"""Extract the OpenAPI spec from the running service and write it to docs/openapi.json.

The service exposes a FastAPI-generated `/openapi.json` endpoint that
is the canonical machine-readable form of the HTTP API. This script
fetches it (either from a live URL or by importing `create_app` in
this repo) and writes a pretty-printed JSON file that is committed
to `docs/openapi.json`. The Markdown companion document,
`docs/api-contract.md`, is the human-readable source of truth — this
script just gives us an artefact that other tools (codegen, contract
tests) can consume and that exact-matches the served spec at the time
of the last regeneration.

Usage:

    # 1) from a running service (preferred — what the network actually sees):
    python scripts/extract-openapi.py --url http://192.168.0.99:8766/openapi.json

    # 2) from the local app, without launching the HTTP server:
    python scripts/extract-openapi.py --from-app

The default mode is `--from-app` because it works in CI (no live
service needed) and produces a deterministic artifact that exactly
matches the code on disk. Run the `--url` mode right before a release
to spot any drift between in-repo types and the running service.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def _from_app() -> dict[str, object]:
    """Import the FastAPI app and return its OpenAPI schema as a dict.

    Side effect: imports a lot of the service graph (settings,
    queue, pipeline, STT engine, auth, log config). All of those are
    already loaded by `app.create_app`, so no extra wiring beyond
    a synthetic `Settings` is required for the spec — FastAPI
    generates paths/types lazily, but endpoints are only registered
    inside `create_app`, so we call it.
    """
    # Local import so this script can be inspected even when the
    # venv isn't activated (avoids ImportError at module-collection
    # time for users that just `python -i scripts/extract-openapi.py`).
    from local_transcription_service.app import create_app
    from local_transcription_service.config import Settings

    # Build a synthetic settings object. Two required fields are
    # satisfied by placeholders that satisfy validation but cannot be
    # confused with real secrets:
    # - `auth_token` (`min_length=16`) → no real token, used for shape only.
    # - `stt_engine="mock"` disables the `stt_api_key` required-when-
    #   openai cross-field validator. We do not need a real STT
    #   engine to generate the OpenAPI spec — only the registered
    #   routes and pydantic types.
    settings = Settings(
        _env_file=None,
        auth_token="dummy-token-for-openapi-extraction-only-do-not-use",
        stt_engine="mock",
    )
    store = None  # not used by FastAPI's schema generator
    return create_app(settings=settings, store=store, pipeline=None).openapi()  # type: ignore[arg-type]  


def _from_url(url: str) -> dict[str, object]:
    """Fetch the OpenAPI schema from a running service via HTTP."""
    import httpx

    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        help=(
            "Fetch the spec from a running service at this URL "
            "(e.g. http://192.168.0.99:8766/openapi.json). "
            "Mutually exclusive with --from-app."
        ),
    )
    parser.add_argument(
        "--from-app",
        action="store_true",
        help="Build the spec in-process from create_app() (CI-friendly).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_PATH,
        help="Destination path. Default: docs/openapi.json (next to this script).",
    )
    args = parser.parse_args(argv)

    if args.url and args.from_app:
        parser.error("--url and --from-app are mutually exclusive")

    if args.url:
        schema = _from_url(args.url)
        source_desc = f"fetched from {args.url}"
    else:
        schema = _from_app()
        source_desc = "built from create_app()"

    # Note: openapi() returns a Schema (pydantic model); convert if needed.
    if not isinstance(schema, dict):
        schema = schema.model_dump(exclude_none=True)  # type: ignore[attr-defined]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(schema, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    # Brief stats so operators can sanity-check the result.
    paths = schema.get("paths", {})
    print(f"Wrote {args.out} ({source_desc})")
    print(f"  openapi: {schema.get('openapi', '?')}")
    print(f"  title:   {schema.get('info', {}).get('title', '?')}")
    print(f"  version: {schema.get('info', {}).get('version', '?')}")
    print(f"  paths:   {len(paths)} ({', '.join(sorted(paths))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
