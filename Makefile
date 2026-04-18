SHELL := /usr/bin/env bash

.PHONY: help install lint fmt test run docker tf-init tf-plan tf-apply \
        tokens-init tokens-list tokens-provision

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS=":.*?## "} {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime + dev deps
	pip install -r requirements.txt
	pip install ruff pytest pytest-asyncio

lint: ## Ruff lint + format check
	ruff check app tests
	ruff format --check app tests

fmt: ## Ruff format in place
	ruff format app tests

test: ## Run the test suite (mock mode)
	ALLOW_UNVERIFIED_TOKENS=true ENV=dev pytest -q

run: ## Run the server locally (mock vendors if keys absent)
	ALLOW_UNVERIFIED_TOKENS=true ENV=dev python -m app.main

docker: ## Build the container image
	docker build -t cam-proxy:dev .

tf-init:
	terraform -chdir=infra/terraform init

tf-plan:
	terraform -chdir=infra/terraform plan

tf-apply:
	terraform -chdir=infra/terraform apply

# ---- token admin -----------------------------------------------------------
# Thin wrappers so operators don't have to remember the `-m` incantation.
# All commands honour AWS_REGION / AWS_PROFILE from the current shell.

tokens-init: ## Create the DynamoDB token table (idempotent)
	python -m app.admin.tokens init-table

tokens-list: ## List all hardware tokens (admin-only; DynamoDB scan)
	python -m app.admin.tokens list

tokens-provision: ## Issue a new token.  Usage: make tokens-provision USER=hamin TIER=standard QUOTA=600
	@test -n "$(USER)" || (echo "USER=<id> required" && exit 1)
	python -m app.admin.tokens provision \
	    --user-id "$(USER)" \
	    --tier    "$(if $(TIER),$(TIER),standard)" \
	    --quota   "$(if $(QUOTA),$(QUOTA),600)"
