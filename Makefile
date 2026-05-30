# cc_auto — local CI surface.
#
# Parent's Dagger pipeline (post-submodule-conversion) calls `make ci`
# from this directory; lint + tests run as a single fail-fast target.
# Per-target invocations exist for the inner loop: `make lint`, `make
# test`, `make fmt`.
#
# All targets shell out through `uv run --group dev` so ruff + pytest
# resolve regardless of the host's default-group config.

.PHONY: ci lint fmt fmt-check test

ci: lint test

lint:
	uv run --group dev ruff check src/ tests/

# Reformat in place (developer convenience; NOT run by `make ci`).
fmt:
	uv run --group dev ruff format src/ tests/

# Format-check (advisory — wire into `ci` if/when parent's Dagger asks).
fmt-check:
	uv run --group dev ruff format --check src/ tests/

test:
	uv run --group dev pytest tests/
