# Pinned by digest: a tag can move between two deployments of the same commit.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Every module, not a list that goes stale: an omitted one only shows up at startup, in a
# CrashLoopBackOff, which is how metrics.py was first missed.
COPY *.py ./
COPY strategies ./strategies

# The decider only answers HTTP: no root needed.
ENV UV_NO_CACHE=1
USER 65534:65534

EXPOSE 8888
CMD ["uv", "run", "--no-sync", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8888"]
