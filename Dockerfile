FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python

FROM build AS test
RUN uv sync --frozen --no-install-project --python /usr/local/bin/python
COPY . .
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "pytest", "-q"]

FROM python:3.13-slim
RUN groupadd -g 10001 petreport && useradd -r -u 10001 -g petreport petreport && mkdir -p /data/media && chown -R 10001:10001 /data
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=build /app/.venv /app/.venv
COPY petreport ./petreport
COPY migrations ./migrations
COPY alembic.ini ./
USER 10001:10001
EXPOSE 8092
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8092/healthz', timeout=3)"
CMD ["uvicorn", "petreport.main:app", "--host", "0.0.0.0", "--port", "8092", "--no-access-log"]
