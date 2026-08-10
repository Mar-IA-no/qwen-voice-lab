.PHONY: bootstrap test lint build dev gpu stop-gpu

bootstrap:
	./scripts/bootstrap.sh

test:
	uv run pytest

lint:
	uv run ruff check src tests
	cd frontend && npm run build

build:
	./scripts/build_frontend.sh
	uv build

dev:
	./scripts/run_dev.sh

gpu:
	./scripts/run_gpu.sh

stop-gpu:
	./scripts/stop_gpu.sh
