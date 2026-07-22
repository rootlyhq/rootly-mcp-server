.DEFAULT_GOAL := help

PYTHON_VERSION := 3.12
SPEC_URL       := https://rootly-heroku.s3.amazonaws.com/swagger/v1/swagger.json
SPEC_PATH      := src/rootly_mcp_server/data/swagger.json
PKG            := src/rootly_mcp_server
UV             := uv run --python $(PYTHON_VERSION)

IMAGE := rootly-mcp-server

.PHONY: help install build run \
        test test-unit test-integration \
        lint format format-check typecheck security \
        fetch-spec audit-spec \
        docker-build docker-test \
        check ci hooks clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies and the pinned Python via uv
	uv sync --dev --python $(PYTHON_VERSION)

build: ## Build the sdist and wheel
	uv build

run: ## Run the MCP server locally (needs ROOTLY_API_TOKEN)
	$(UV) rootly-mcp-server

# --- tests ---
test: ## Run the full test suite (with coverage)
	$(UV) pytest

test-unit: ## Run unit tests only
	$(UV) pytest tests/unit/

test-integration: ## Run local integration tests (needs ROOTLY_API_TOKEN)
	$(UV) pytest tests/integration/local/ -x

# --- quality ---
lint: ## Lint with ruff
	$(UV) ruff check .

format: ## Auto-format with ruff
	$(UV) ruff format .

format-check: ## Check formatting without modifying files (CI gate)
	$(UV) ruff format --check .

typecheck: ## Type-check with pyright and mypy
	$(UV) pyright
	$(UV) mypy $(PKG)

security: ## Security scan (bandit blocking; pip-audit advisory, mirrors CI)
	$(UV) bandit -r src/
	-$(UV) pip-audit

# --- OpenAPI spec ---
fetch-spec: ## Refresh the bundled OpenAPI spec from source (never hand-edit it)
	curl -fsS $(SPEC_URL) -o $(SPEC_PATH)

audit-spec: ## Audit the bundled spec for MCP tool-generation issues
	$(UV) python scripts/audit_openapi.py --filtered-defaults --instantiate-server

# --- docker (mirrors the CI container-tests job) ---
docker-build: ## Build the Docker image
	docker build -t $(IMAGE) .

docker-test: docker-build ## Build the image and run the containerized server smoke test (needs ROOTLY_API_TOKEN)
	docker run -d --name $(IMAGE) -p 8000:8000 -e ROOTLY_API_TOKEN="$(ROOTLY_API_TOKEN)" $(IMAGE)
	timeout 30s bash -c 'until curl -fsS http://localhost:8000/health || curl -fsS http://localhost:8000/; do sleep 2; done'
	MCP_SERVER_URL=http://localhost:8000 $(UV) pytest tests/integration/remote/test_essential.py -v --timeout=60; \
		status=$$?; docker stop $(IMAGE) && docker rm $(IMAGE); exit $$status

# --- aggregates ---
check: lint format-check typecheck test-unit ## Fast pre-push gate (mirrors CI quality + unit tests)

ci: lint format-check typecheck security test-unit audit-spec ## Everything CI enforces (excl. docker/remote)

# --- misc ---
hooks: ## Install the git pre-commit hook
	./scripts/setup-hooks.sh

clean: ## Remove build and test artifacts
	rm -rf dist build .pytest_cache .mypy_cache .ruff_cache coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
