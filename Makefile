PYTHON ?= .venv/bin/python

.PHONY: install serve predict test
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
