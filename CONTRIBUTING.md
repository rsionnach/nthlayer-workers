# Contributing to nthlayer-workers

Thank you for considering contributing to **nthlayer-workers** — the unified
Tier 2 background-computation process housing all worker modules (observe,
measure, correlate, respond, learn). We're in active v1.5 development and
welcome feedback from the SRE/DevOps community.

Workers talks to `nthlayer-core` **exclusively over HTTP** — never the SQLite
store directly. Keep that boundary intact.

## Ways to Contribute

- **Report bugs / request features** — [open an issue](https://github.com/rsionnach/nthlayer-workers/issues).
- **Discuss** — [GitHub Discussions](https://github.com/rsionnach/nthlayer/discussions) for the wider ecosystem.
- **Code & docs** — pull requests welcome (see below).

## Development Setup

```bash
# Install uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone alongside nthlayer-common (workers depends on it via a sibling path)
git clone https://github.com/rsionnach/nthlayer-common.git
git clone https://github.com/rsionnach/nthlayer-workers.git
cd nthlayer-workers
uv sync --extra dev                  # creates .venv with test/lint tools

# Run the test suite
uv run pytest -q                     # full suite
uv run pytest tests/test_<name>.py -v  # a single file
uv run pytest -k "<expr>" -v         # by name

# Lint
uv run ruff check src/ tests/
```

> **Sibling dependency & Python.** `pyproject.toml` declares
> `nthlayer-common = { path = "../nthlayer-common", editable = true }`, so
> `nthlayer-common` must sit next to this repo on disk (CI checks it out at
> `path: nthlayer-common`). The relative path must resolve identically
> locally and in CI. Requires Python 3.11+ (`uv` will provision it via
> `uv python install` if needed).

A clean clone to a green `uv run pytest -q` should take well under five
minutes. CI runs ruff + pytest on a py3.11/3.12/3.13 matrix (`fail-fast:
false`) for every push and PR to `main`.

## Pull Request Process

1. Fork the repository and create a feature branch off `main`
   (`git checkout -b feat/your-change`).
2. Make your change with tests.
3. Ensure tests pass: `uv run pytest -q`.
4. Ensure lint passes: `uv run ruff check src/ tests/`.
5. Commit using Conventional Commits (see below).
6. Push to your fork and open a PR against `main`.

Commits land on `main`; `release-please` maintains the release PR. A
Docker-based smoke gate (`tests/release-smoke/`) runs before PyPI publish.

## Development Guidelines

### Code Style

- Python 3.11+, type hints encouraged.
- Ruff lint floor is **frozen** at
  `select = ["E4","E7","E9","F","I","UP","SIM","B"]` (ecosystem ruff-floor
  parity). `E501` and the full `W` family are deliberately **not** in the
  floor — do not add them without ecosystem-wide alignment.
- Severity classification is deterministic in v1.5 (no LLM in the worker
  path). SLO target convention is a 0–100 percentage.

### Commit Messages

```
<type>: <description>

<optional body>
```

`feat` / `fix` / `perf` / `deps` / `refactor` / `docs` surface in the
changelog; `chore` / `test` / `ci` / `build` / `style` are hidden.

### Console Scripts

`nthlayer-workers serve|gate` plus the per-module legacy CLIs. `gate` exit
codes: `0` = APPROVED/WARNING, `1` = eval error, `2` = BLOCKED.

### Testing

- Add tests for new behaviour.
- Run a single file with `-v` while iterating; run the full `-q` suite before
  opening a PR.

## Finding Something to Work On

Browse [open issues](https://github.com/rsionnach/nthlayer-workers/issues) and
look for `good-first-issue` / `help-wanted` labels. Maintainers track detailed
work in **Beads**, a Dolt-backed board in the `opensrm` repo
(`cd ../opensrm && bd ready --json`) — you don't need it to contribute.

## Code of Conduct

Be respectful and constructive — we're all here to build better reliability
tooling.

## Questions?

- [GitHub Issues](https://github.com/rsionnach/nthlayer-workers/issues) — bugs and features.
- [GitHub Discussions](https://github.com/rsionnach/nthlayer/discussions) — general questions.

## License

nthlayer-workers is distributed under the Apache License 2.0. By
contributing, you agree that your contributions will be licensed under the
same terms (see `LICENSE`).

---

**Thank you for helping make NthLayer better!**
