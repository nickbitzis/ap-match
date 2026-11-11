PORT ?= 8887
POS ?= 25000

.PHONY: install test lint format simulate gate run docker-build docker-run screenshot clean

install:
	uv sync --python 3.12

test:
	uv run --group dev pytest -q

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff format .
	uvx ruff check . --fix

simulate:  ## a year of AP on 25,000 POs, writes data/report.json (25 to 70 seconds)
	uv run apmatch run --pos $(POS)

gate:  ## what CI runs: fail if a planted duplicate or bank fraud is approved, or touchless drops below 78%
	uv run apmatch run --pos 8000 --suppliers 160 --out data/ci/report.json --gate --min-touchless 0.78

run:
	PORT=$(PORT) APMATCH_POS=$(POS) uv run apmatch serve

docker-build:
	docker build -t ap-match .

docker-run:
	docker run --rm -p $(PORT):8887 ap-match

screenshot:  ## needs the server running on $(PORT) and Google Chrome
	uv run python scripts/screenshot.py --port $(PORT)

clean:
	rm -rf .pytest_cache .ruff_cache data
