export DATABASE_URL ?= postgresql://app:app-dev-only@127.0.0.1:55213/app
PY = ./venv/bin/python
BASE = http://127.0.0.1:8213

bootstrap:      ## local virtualenv + database container
	python3 -m venv venv && ./venv/bin/pip install -q -r requirements-dev.txt
	docker compose up -d db
seed:           ## migrate and load the synthetic world (no-op if already seeded)
	$(PY) -m sf.seed --if-empty
dev:            ## API with reload on :8213, jobs run by a local worker
	($(PY) -m core.jobs sf.api &) && $(PY) -m uvicorn sf.api:app --reload --port 8213
test:
	$(PY) -m pytest --cov=sf --cov=core --cov-report=term-missing
demo:           ## the full stack in Docker, then the demo scenario against it
	docker compose up --build -d --wait && $(PY) -m core.scenario $(BASE)
load-test:      ## run `make demo` first
	$(PY) -m core.loadtest $(BASE) fleet-viewer-demo 32 10 "GET /v1/spacecraft/d4914160-ad52-5e2d-a156-bb0b0183c92e/state" "GET /v1/conjunctions?risk_min=0.00001" "GET /v1/commands" 
reset:          ## drop all data (volume included)
	docker compose down -v
.PHONY: bootstrap seed dev test demo load-test reset
