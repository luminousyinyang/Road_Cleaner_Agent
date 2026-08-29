# Road Cleaner
#
# `make setup && make demo` is the whole quickstart. Everything below runs
# locally with no credentials.

.DEFAULT_GOAL := help
VENV := .venv
PY   := $(VENV)/bin/python
PIP  := uv pip

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

$(VENV):
	uv venv --python 3.11

.PHONY: setup
setup: $(VENV) ## Create the venv and install everything
	$(PIP) install -e ".[dev]"
	@[ -f .env ] || cp .env.example .env
	@echo "\nReady. Try:  make demo"

.PHONY: setup-cloud
setup-cloud: $(VENV) ## Also install the Google Cloud / Gemini extras
	$(PIP) install -e ".[dev,cloud]"

.PHONY: doctor
doctor: ## Show which adapter is wired to each port
	$(VENV)/bin/road-cleaner doctor

.PHONY: seed
seed: ## Load the camera registry
	$(VENV)/bin/road-cleaner seed

.PHONY: demo
demo: ## Watch a simulated week of roads, then serve the dashboard
	$(VENV)/bin/road-cleaner demo --days 7
	@echo "\nStarting the dashboard on http://127.0.0.1:8080 — Ctrl-C to stop."
	$(VENV)/bin/road-cleaner serve

.PHONY: run
run: ## Run the pipeline against the real clock
	$(VENV)/bin/road-cleaner run

.PHONY: serve
serve: ## Start the dashboard
	$(VENV)/bin/road-cleaner serve

.PHONY: outbox
outbox: ## Read the reports that would have been sent
	$(VENV)/bin/road-cleaner outbox

.PHONY: test
test: ## Run every test (no credentials needed)
	$(VENV)/bin/pytest -q

.PHONY: test-fast
test-fast: ## Unit tests only
	$(VENV)/bin/pytest tests/unit -q

.PHONY: lint
lint: ## Check formatting and lints
	$(VENV)/bin/ruff check src tests

.PHONY: fix
fix: ## Auto-fix what can be auto-fixed
	$(VENV)/bin/ruff check --fix src tests

.PHONY: clean
clean: ## Delete generated data (database, frames, outbox)
	rm -rf data/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache

.PHONY: diagrams
diagrams: ## Re-render docs/img/*.png from the mermaid in docs/diagram.md
	@command -v npx >/dev/null || { echo "needs node on PATH"; exit 1; }
	$(VENV)/bin/python docs/render_diagrams.py

.PHONY: deploy
deploy: ## Deploy to Google Cloud:  make deploy PROJECT=my-project
	@[ -n "$(PROJECT)" ] || { echo "usage: make deploy PROJECT=your-gcp-project"; exit 1; }
	./deploy/deploy.sh $(PROJECT) $(or $(REGION),us-central1)

.PHONY: teardown
teardown: ## Tear the deployment down again
	@[ -n "$(PROJECT)" ] || { echo "usage: make teardown PROJECT=your-gcp-project"; exit 1; }
	./deploy/teardown.sh $(PROJECT) $(or $(REGION),us-central1)
