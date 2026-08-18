FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 appuser
USER appuser

ENTRYPOINT ["vikunja-mcp"]
