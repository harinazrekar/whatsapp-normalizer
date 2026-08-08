# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder -- compilers and dev headers live here and are thrown away. Some of
# the dependency tree (uvloop, httptools via uvicorn[standard]) may fall back to
# building from source on platforms without wheels, and gcc is not something to
# ship on an internet-facing box.
# ---------------------------------------------------------------------------
FROM python:3.11.9-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

# A self-contained venv is the unit copied into the runtime stage: it keeps the
# copy to a single predictable path instead of chasing site-packages layouts.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.11.9-slim-bookworm AS runtime

# PYTHONUNBUFFERED keeps structured logs flowing to the container log driver in
# real time instead of sitting in a block buffer until the process exits --
# which is exactly when you most want to have already seen them.
# PYTHONDONTWRITEBYTECODE avoids .pyc writes into an otherwise read-only-ish
# filesystem owned by root.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Fixed uid/gid so a bind-mounted volume has stable ownership across rebuilds.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Application code is owned by root and merely readable by the runtime user, so
# a compromised process cannot rewrite the code it is running.
COPY --chown=root:root app ./app

USER app

EXPOSE 8000

# Checked with stdlib urllib rather than curl -- curl is not in the slim base and
# adding it just for a healthcheck grows the attack surface for no benefit. The
# start period covers the Redis connectivity check in /health during boot.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
