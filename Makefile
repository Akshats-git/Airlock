.PHONY: test reset
test:
	uv run pytest -q
reset:
	uv run python -m airlock.reset
