# Contributing

Thanks for taking the time to contribute.

## Development setup

```bash
git clone https://github.com/olegpydev/pgsteward
cd pgsteward
uv sync
cp .env.example .env   # fill in at least one connection
```

If you already run pgsteward for yourself, note that
`~/.config/pgsteward/.env` is searched before the one in the checkout and
wins. Point `PGSTEWARD_ENV_FILE` at the checkout copy, or the server you
start from here will quietly use your own connections. The test suite is
insulated from both files and from `~/.config/pgsteward/` — see
`tests/conftest.py`.

## Checks

Everything CI runs, you can run locally:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy pgsteward
uv run pytest --cov=pgsteward --cov-report=term-missing
```

`pytest` runs the unit suite: integration tests are excluded by default
and selected with `-m integration`.

CI fails the unit job below 95% coverage. The threshold sits under the
actual number on purpose: it is there to catch a module dropping out of
the suite, not to be argued with line by line.

Integration tests need a throwaway PostgreSQL. They are skipped unless
`PGSTEWARD_TEST_DSN` is set, and — when it is a URI, which is the only
form the database name can be read from — refused unless that name
contains `test`. The fixtures run `DROP SCHEMA ... CASCADE` and
`DROP ROLE`, and a typo in an environment variable should not be able to
reach anything you care about:

```bash
docker run --rm -d --name pgsteward-test \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=pgsteward_test \
  -p 5432:5432 postgres:17

PGSTEWARD_TEST_DSN=postgresql://postgres:postgres@127.0.0.1:5432/pgsteward_test \
  uv run pytest -m integration
```

CI runs this against PostgreSQL 14 and 17. Catalog queries are the part
most likely to disagree between versions, so run both before changing one.

## Testing philosophy

The split between the two suites is described in
[docs/architecture.md](docs/architecture.md#testing-strategy). What it
asks of a contribution: keep the integration set small and purposeful, and
do not add a test that only re-states what the type checker already
guarantees.

## Other database engines

pgsteward is PostgreSQL-only, and pull requests adding another engine will
not be merged. The reasoning, and why the `DBAdapter` protocol is not an
extension point, are in
[docs/architecture.md](docs/architecture.md#why-there-is-no-second-engine).
Please do not reintroduce an engine field in the configuration or in
responses.

## Code style

- Line length 79, single quotes (enforced by `ruff format`).
- Docstrings explain *why*, not *what* — the code already says what.
- Russian docstrings and comments are accepted. Anything a user or a model
  reads is English: tool descriptions, error messages, documentation, and
  the example relationship map.
- `# --- section ---` separators are English even in a Russian file — they
  are navigation, not prose.

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).
