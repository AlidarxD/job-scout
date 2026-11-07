FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[llm]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "jobscout.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
