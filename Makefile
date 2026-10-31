PYTHON ?= .venv/bin/python

.PHONY: install-train category-validate category-splits category-train category-eval category-check
.PHONY: install serve predict test eval-drafts eval-regressions measure benchmark submission-check audit-repo check audit-data prepare-data chain-verify chain-export chain-anchor chain-tamper chain-bench
install:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r requirements-dev.lock
	.venv/bin/python -m pip install --no-deps -e .

serve:
	$(PYTHON) -m uvicorn support_copilot.api:app --host 127.0.0.1 --port 8000 --no-proxy-headers

predict:
	@test -n "$(IN)" -a -n "$(OUT)" || (echo 'Usage: make predict IN=tickets.jsonl OUT=pred.jsonl' >&2; exit 2)
	$(PYTHON) -m support_copilot.cli --input "$(IN)" --output "$(OUT)"

test:
	$(PYTHON) -m pytest -q

eval-drafts:
	$(PYTHON) -m support_copilot.evaluate_drafts --output outputs/wave-3/synthetic-safety.json

eval-regressions:
	$(PYTHON) -m support_copilot.evaluate_regressions --output outputs/wave-4/regressions.json

measure:
	$(PYTHON) -m support_copilot.measure --output outputs/wave-5

benchmark:
	$(PYTHON) -m support_copilot.benchmark --output outputs/wave-5/http-latency.json

submission-check:
	$(PYTHON) -m support_copilot.submission_check --output outputs/wave-7

audit-repo:
	git fetch --quiet --all
	$(PYTHON) -m support_copilot.repo_audit

# Full offline acceptance before submission: tests, safety matrices, HTTP/CLI/demo, Git hygiene.
check: test eval-regressions eval-drafts submission-check audit-repo

chain-verify:
	$(PYTHON) -m support_copilot.chain_cli verify

chain-export:
	$(PYTHON) -m support_copilot.chain_cli export

# WAVE_DOC=docs/wave-8.md also appends the head to a tracked document; commit it as the external witness.
chain-anchor:
	$(PYTHON) -m support_copilot.chain_cli anchor $(if $(WAVE_DOC),--wave-doc "$(WAVE_DOC)")

# Corrupts the chain on purpose: requires COPILOT_CHAIN_TAMPER_DEMO=1 and belongs on a demo database.
chain-tamper:
	$(PYTHON) -m support_copilot.chain_cli tamper $(if $(INDEX),--index $(INDEX))

chain-bench:
	$(PYTHON) -m support_copilot.chain_cli bench

audit-data:
	@test -n "$(IN)" -a -n "$(OUT)" || (echo 'Usage: make audit-data IN=data/tickets_train.jsonl.gz OUT=outputs/audit.json' >&2; exit 2)
	$(PYTHON) -m support_copilot.dataset audit --input "$(IN)" --output "$(OUT)"

prepare-data:
	@test -n "$(IN)" -a -n "$(OUT)" || (echo 'Usage: make prepare-data IN=data/tickets_train.jsonl.gz OUT=outputs/wave1' >&2; exit 2)
	$(PYTHON) -m support_copilot.dataset prepare --input "$(IN)" --output-dir "$(OUT)"

quality-report:
	$(PYTHON) -m support_copilot.quality report --output outputs/roadmap/quality.json

gold-packet:
	$(PYTHON) -m support_copilot.quality prepare-gold --output outputs/roadmap/gold-40

feedback-export:
	$(PYTHON) -m support_copilot.feedback --output outputs/roadmap/approved-feedback.jsonl

retention-preview:
	$(PYTHON) -m support_copilot.privacy --days 30

load-check:
	$(PYTHON) -m support_copilot.benchmark --workers 8 --passes 3 --output outputs/roadmap/load.json

# --- Opt-in local category model (docs/category-model.md). Data and models stay in outputs/. ---
TAXONOMY ?= data/handout-2026-10-27/taxonomy.json
install-train:
	.venv/bin/python -m pip install -r requirements-train.lock

category-validate:
	@test -n "$(IN)" || (echo 'Usage: make category-validate IN=outputs/category-data/labels.jsonl' >&2; exit 2)
	$(PYTHON) -m support_copilot.category_training validate --data "$(IN)" --taxonomy "$(TAXONOMY)"

category-splits:
	@test -n "$(IN)" -a -n "$(OUT)" || (echo 'Usage: make category-splits IN=unsplit.jsonl OUT=split.jsonl [SEED=20261031]' >&2; exit 2)
	$(PYTHON) -m support_copilot.category_training assign-splits --data "$(IN)" --taxonomy "$(TAXONOMY)" --output "$(OUT)" $(if $(SEED),--seed $(SEED))

category-train:
	@test -n "$(IN)" -a -n "$(OUT)" || (echo 'Usage: make category-train IN=split.jsonl OUT=outputs/category-model/<run>' >&2; exit 2)
	$(PYTHON) -m support_copilot.category_training train --data "$(IN)" --taxonomy "$(TAXONOMY)" --output "$(OUT)"

category-eval:
	@test -n "$(MODEL)" -a -n "$(IN)" -a -n "$(OUT)" || (echo 'Usage: make category-eval MODEL=.../model.json IN=split.jsonl OUT=report.json' >&2; exit 2)
	$(PYTHON) -m support_copilot.category_training evaluate --model "$(MODEL)" --data "$(IN)" --taxonomy "$(TAXONOMY)" --output "$(OUT)"

category-check:
	$(PYTHON) -m support_copilot.category_model check $(if $(MODEL),--model "$(MODEL)")
