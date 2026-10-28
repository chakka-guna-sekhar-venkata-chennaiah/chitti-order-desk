# Short names for common commands. Run `make help`.
PY ?= python3

.PHONY: help install init demo test serve sender scheduler local devbot-dealer devbot-shop \
        skills eval-offline eval-reader eval-sql eval-chat scorecard

help:            ## show this list
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

install:         ## create .venv and install the project with test tools
	$(PY) -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

init:            ## create the database and load the demo data
	order-desk init

demo:            ## run one full order offline (no keys needed)
	order-desk demo

test:            ## run all tests (offline)
	pytest -q

serve:           ## MCP server (dealer + shop endpoints)
	order-desk serve

sender:          ## outbox sender (Telegram messages)
	order-desk sender

scheduler:       ## timed jobs (expiry, reminders)
	order-desk scheduler

local:           ## serve + sender + scheduler together; Ctrl-C stops all
	./scripts/start_local.sh

devbot-dealer:   ## reference runtime: dealer bot without NemoClaw
	order-desk devbot --role dealer

devbot-shop:     ## reference runtime: shop bot without NemoClaw
	order-desk devbot --role shop

skills:          ## regenerate hermes/*/SOUL.md and SKILL.md
	order-desk export-skills

eval-offline:    ## matcher eval (no keys)
	$(PY) -m evals.run_matching

eval-reader:     ## chitti reader eval (Token Factory)
	$(PY) -m evals.run_chit_reader

eval-sql:        ## text-to-SQL eval (Token Factory)
	$(PY) -m evals.run_text2sql

eval-chat:       ## conversation eval with a simulated dealer (Token Factory)
	$(PY) -m evals.run_conversations

scorecard:       ## write evals/results/SCORECARD.md
	$(PY) -m evals.report
