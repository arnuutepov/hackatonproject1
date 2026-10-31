PYTHON ?= .venv/bin/python

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
