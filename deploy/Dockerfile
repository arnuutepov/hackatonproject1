FROM docker.io/library/python@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
WORKDIR /app
COPY requirements-dev.lock pyproject.toml README.md ./
COPY support_copilot ./support_copilot
RUN python -m pip install --no-cache-dir -r requirements-dev.lock && python -m pip install --no-deps . \
    && useradd --uid 10001 --create-home app && mkdir -p /state && chown app:app /state
USER 10001:10001
CMD ["python", "-m", "uvicorn", "support_copilot.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers", "--no-access-log"]
