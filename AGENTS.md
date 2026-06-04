# nthlayer-workers — agent-facing commands

Unified Tier 2 background computation. Houses all worker modules:
observe, measure, correlate, respond, learn. Talks to nthlayer-core
exclusively via HTTP — never touches the SQLite store directly.

## Stack

- Python ≥3.11, `uv`-managed.
- Tests: `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`),
  `respx>=0.21` (HTTP mocking).
- Lint: `ruff`. **Floor frozen** at
  `select = ["E4", "E7", "E9", "F", "I", "UP", "SIM", "B"]`
  post-opensrm-po23; `E501` and the full `W` family are separate
  hygiene calls, not part of the floor.
- Typecheck: **not configured** (no `mypy.ini`, no `pyrightconfig.json`,
  no `[tool.mypy]`/`[tool.pyright]` in `pyproject.toml`). TODO: wire
  one when the rest of the ecosystem standardises.

## Build / test / lint / run commands

```bash
uv sync --extra dev                    # set up .venv
uv pip install -e .                    # editable install (one-shot)
uv run pytest -q                       # full suite (1873 passed, 1 skipped)
uv run pytest tests/test_<name>.py -v  # single file
uv run pytest -k "<expr>" -v           # single test by name
uv run ruff check src/ tests/          # lint

# Run all worker modules in one process
nthlayer-workers serve \
  --core-url http://localhost:8000 \
  --instance-id worker-0 \
  --prometheus-url http://localhost:9090 \
  [--collect-interval 60] [--drift-interval 1800] \
  [--topology-interval 86400] [--correlate-interval 10] \
  [--topology-drift-interval 3600] [--contract-interval 3600] \
  [--measure-interval 60] [--respond-interval 30] \
  [--outcome-interval 60] [--retrospective-interval 30] \
  [--expiry-threshold-days 7] [--min-resolution-age-hours 1] \
  [--tempo-endpoint http://tempo:3200]

# CLI-only deploy gate (no HTTP API in v1.5)
nthlayer-workers gate \
  --service <svc> [--tier critical] [--commit-sha <sha>] \
  --core-url http://localhost:8000
# Exit codes: 0=APPROVED/WARNING, 1=eval error, 2=BLOCKED.

# Per-module CLIs still exist for the legacy paths:
nthlayer-observe --help
nthlayer-measure --help
nthlayer-correlate --help
nthlayer-respond --help
nthlayer-learn --help
```

## CI

- `.github/workflows/test.yml` triggers on push/PR to `main`.
- Matrix: Python 3.11 / 3.12 / 3.13. `fail-fast: false` (full signal
  across all versions on failure).
- Steps:
  1. Checkout `nthlayer-workers` + `nthlayer-common` as siblings
     (`path: nthlayer-workers` / `path: nthlayer-common`) — required
     because `pyproject.toml` declares
     `nthlayer-common = { path = "../nthlayer-common", editable =
     true }` and the relative path must resolve the same way locally
     and in CI.
  2. Install uv via `astral-sh/setup-uv@v7`.
  3. `uv sync --extra dev --python ${{ matrix.python-version }}`.
  4. `uv run ruff check src/ tests/`.
  5. `uv run pytest -q`.
- First-run baseline: green, ~44s.

## Release

- `googleapis/release-please-action@v4`. Push to `main` inspects
  Conventional Commits and maintains a release PR. Config:
  `release-please-config.json` + `.release-please-manifest.json`.
- Conventional Commit taxonomy: `feat`/`fix`/`perf`/`deps`/`refactor`/
  `docs` surface in the changelog; `chore`/`test`/`ci`/`build`/`style`
  are hidden.
- Release PR merge → release-please cuts the tag → `release.yml` runs
  trusted-publishing PyPI flow.
- **Docker smoke gate** between `twine check` and PyPI publish: a
  `python:3.11-slim` container mounts `dist/` and
  `tests/release-smoke/` read-only, installs the freshly-built wheel +
  pytest, runs the smoke suite. Failure blocks publish.
- **Known trigger issue** (`opensrm-pdoe`): `release.yml` fires on
  `release: published`. The `GITHUB_TOKEN`-cascade-block means
  release-please-created releases do not trigger `release.yml`
  automatically. Remediation: pivot to `push: tags: ['v*']` or
  configure release-please with a PAT.
- Dependabot: two ecosystems (`uv` + `github-actions`) on
  Monday-morning Europe/Dublin schedule. Sibling `nthlayer-*` packages
  and dev deps grouped into weekly PRs. Auto-merge policy in
  `.github/workflows/dependabot-automerge.yml`: external runtime patch
  and dev patch/minor auto-merge; sibling packages and any major bump
  require review.
