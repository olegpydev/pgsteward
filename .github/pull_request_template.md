## What and why

<!-- What changes, and what problem it solves. Link the issue if there is one. -->

## Checks

- [ ] `uv run ruff check .` and `uv run ruff format --check .`
- [ ] `uv run mypy pgsteward`
- [ ] `uv run pytest -m "not integration"`
- [ ] `uv run pytest -m integration` against a live PostgreSQL (if the change
      touches SQL, the adapter, or the security layer)

## Security

- [ ] This does not widen what an agent can write, or the change is explicit
      and documented in `docs/security.md`.
- [ ] No statement text or parameter values were added to any log line.
