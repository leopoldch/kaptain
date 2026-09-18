FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Every module, not a list that goes stale: an omitted one only shows up at startup, in a
# CrashLoopBackOff, which is how config.py, metrics.py and telemetry.py were first missed.
COPY *.py ./
COPY strategies ./strategies

EXPOSE 8888
CMD ["uv", "run", "--no-sync", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8888"]
