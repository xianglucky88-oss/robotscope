PY=python3
run:
	PYTHONPATH=src $(PY) -m robotscope.cli
plan:
	PYTHONPATH=src $(PY) -m robotscope.cli --plan --input fixtures/crawl.json --offline fixtures/offline_routes.json --db robotscope-plan.db --format summary
crawl:
	PYTHONPATH=src $(PY) -m robotscope.cli --run --input fixtures/crawl.json --offline fixtures/offline_routes.json --db robotscope-run.db --format summary
test:
	@if PYTHONPATH=src $(PY) -c "import pytest" 2>/dev/null; then \
		PYTHONPATH=src $(PY) -m pytest -q; \
	else \
		PYTHONPATH=src $(PY) -m unittest discover -s tests -v; \
	fi
clean:
	rm -rf .pytest_cache **/__pycache__ *.db
