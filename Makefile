.PHONY: dev check test security prod-status rollback
CONTROL := $(HOME)/Library/Application Support/SRTube/production

dev:
	uv run srtube --env-file .env.dev
check:
	uv run ruff check src tests ops scripts
	uv run ruff format --check src tests ops scripts
test:
	uv run pytest -q
security:
	uv run bandit -q -r src ops scripts
	uv run pip-audit --progress-spinner off
	uv run python scripts/check_secrets.py
prod-status:
	/usr/bin/python3 -I "$(CONTROL)/release.py" status --root "$(CONTROL)"
rollback:
	/usr/bin/python3 -I "$(CONTROL)/release.py" rollback --root "$(CONTROL)"
