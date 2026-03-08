.DEFAULT_GOAL := help

.PHONY: setup
setup:  ## Initial project setup (install Python, deps, pre-commit)
	@command -v pyenv >/dev/null 2>&1 || { echo "pyenv not found, install it first"; exit 1; }
	@command -v uv >/dev/null 2>&1 || { echo "uv not found, install it first"; exit 1; }
	pyenv install --skip-existing $$(cat .python-version)
	uv sync --all-extras
	uv run pre-commit install

.PHONY: lint
lint:  ## Run ruff linter
	uv run ruff check src/

.PHONY: format
format:  ## Format code with ruff
	uv run ruff format src/

.PHONY: format-check
format-check:  ## Check code formatting without changes
	uv run ruff format --check src/

.PHONY: check
check: lint format-check  ## Run all checks

.PHONY: clean
clean:  ## Remove generated files
	rm -rf .venv/
	rm -rf .ruff_cache/
	rm -rf dist/
	rm -rf *.egg-info/
	find . -type d -name __pycache__ -exec rm -rf {} +

.PHONY: service-install
service-install:  ## Install and enable systemd user service
	cp touchpad-zones.service ~/.config/systemd/user/touchpad-zones.service
	systemctl --user daemon-reload
	systemctl --user enable touchpad-zones.service
	@echo "Service installed. Start with: systemctl --user start touchpad-zones"

.PHONY: service-status
service-status:  ## Show systemd user service status
	systemctl --user status touchpad-zones.service

.PHONY: service-log
service-log:  ## Show full service log
	journalctl --user -u touchpad-zones.service --no-pager

.PHONY: service-log-tail
service-log-tail:  ## Follow service log output (tail -f)
	journalctl --user -u touchpad-zones.service -f

.PHONY: service-restart
service-restart:  ## Restart the systemd user service
	systemctl --user restart touchpad-zones.service
	systemctl --user status touchpad-zones.service

.PHONY: help
help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
