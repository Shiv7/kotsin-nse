UV ?= uv
NPM ?= npm

.PHONY: help setup test lint typecheck ui run dev check clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## install backend and frontend dependencies
	cd backend && $(UV) sync
	cd frontend && $(NPM) install

test: ## run the backend test suite (offline)
	cd backend && $(UV) run pytest -q

lint: ## ruff + import contracts
	cd backend && $(UV) run ruff check kotsin_nse tests && $(UV) run lint-imports

ui: ## typecheck and build the frontend into frontend/dist
	cd frontend && $(NPM) run build

check: lint test ui ## everything CI runs

run: ui ## build the UI and serve engine + API + UI on one port
	cd backend && $(UV) run kotsin-nse

dev: ## backend only; run `cd frontend && npm run dev` alongside for hot reload
	cd backend && $(UV) run kotsin-nse

clean:
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.import_linter_cache frontend/dist
