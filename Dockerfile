FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY brand_agent ./brand_agent
COPY config ./config
COPY data/sample_items.json ./data/sample_items.json

RUN pip install --upgrade pip && pip install .

RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 10000

CMD ["sh", "-c", "python -m brand_agent serve --host 0.0.0.0 --port ${PORT:-10000}"]
