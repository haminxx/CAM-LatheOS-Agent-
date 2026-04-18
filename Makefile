SHELL := /usr/bin/env bash

.PHONY: help install lint fmt test run docker tf-init tf-plan tf-apply

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
