# AGENTS.md — operating instructions for AI agents

## Project type

Python service (FastAPI). Companion to a Chrome extension
(`../20260404_ytt`). Implements the architectural decision in
**ADR-012** (lives in the extension repo, see `docs/adr/README.md`).

## Documentation layout

- `docs/adr/` — vendored system-level ADRs (from the extension repo,
  with provenance header at the top of each file) plus any
  service-internal ADRs. The extension repo remains the canonical
  source for vendored ADRs; if you edit one, edit the canonical
  source first and re-vendor.
- `docs/hld/` — operational design for this service. Worker count,
  queue tech, retry policy, lifecycle, etc. New HLDs start at the next
  number (HLD-002, ...).
- `README.md` — what the service is, how to run it.
- `pyproject.toml` — single source of truth for dependencies and tooling config.

## Tooling

- Package manager: **uv**. Never `pip install` directly; never commit
  `requirements.txt`. Add deps via `uv add` / `uv add --dev`.
- Linter / formatter: **ruff**. `uv run ruff check .` and
  `uv run ruff format .` before committing.
- Tests: **pytest** with `pytest-asyncio` (`asyncio_mode = "auto"`).
  Run with `uv run pytest`.
- Python version: **3.12** (pinned in `.python-version`).

## ADR vs HLD boundary

Before adding decisions to either document, apply the boundary test:

> *What changes in this document if I scale from 1 worker to N workers,
> swap SQLite for Redis, or move from Mac Mini to Jetson?*
>
> If the answer is "nothing", the decision belongs in an **ADR**
> (architecture contract).
>
> If the answer is "everything", the decision belongs in an **HLD**
> (operational design).

ADR-012 already covers the architectural contract for the local
transcription feature. Service-internal architecture decisions (e.g.,
queue schema, lease protocol) that don't change the extension contract
also belong in ADRs — but in **this** repo.

## Code layout

Source under `src/local_transcription_service/` (src-layout, importable
package). Implementation modules (`queue/`, `pipeline/`, `api/`) are
added after the HLD is reviewed. Do not create empty package skeletons
ahead of design decisions.

## Things to avoid

- Do not introduce cloud SDKs (Whisper API, Deepgram, AssemblyAI).
  ADR-012 explicitly rejects cloud inference.
- Do not bind to `0.0.0.0` without an explicit auth scheme. Default
  bind is `127.0.0.1`.
- Do not commit model weights, audio cache, or generated transcripts
  to git. `.gitignore` already excludes them.
- Do not create `docs/adr/*.md` files in this repo without checking
  whether the decision is actually service-internal.