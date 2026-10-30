PYTHON ?= .venv/bin/python

.PHONY: install serve predict test audit-data prepare-data
install:
	python3 -m venv .venv
	.venv/bin/python -m pip install -r requirements-dev.lock
	.venv/bin/python -m pip install --no-deps -e .

serve:
	$(PYTHON) -m uvicorn support_copilot.api:app --host 127.0.0.1 --port 8000

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

audit-data:
	@test -n "$(IN)" -a -n "$(OUT)" || (echo 'Usage: make audit-data IN=data/tickets_train.jsonl.gz OUT=outputs/audit.json' >&2; exit 2)
	$(PYTHON) -m support_copilot.dataset audit --input "$(IN)" --output "$(OUT)"

prepare-data:
	@test -n "$(IN)" -a -n "$(OUT)" || (echo 'Usage: make prepare-data IN=data/tickets_train.jsonl.gz OUT=outputs/wave1' >&2; exit 2)
	$(PYTHON) -m support_copilot.dataset prepare --input "$(IN)" --output-dir "$(OUT)"
