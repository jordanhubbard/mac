# mac control plane — production container.
#
# Multi-stage build: install deps into a slim image, run as non-root with a
# pinned working directory. SQLite database is bind-mounted at /var/lib/mac/db.
# MAC_SECRET_KEY MUST be provided at runtime; the container refuses to start
# without it.

FROM python:3.12.7-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && \
    python -m build --wheel --outdir /wheels


FROM python:3.12.7-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAC_DB=/var/lib/mac/mac.db

RUN groupadd --system mac && \
    useradd --system --gid mac --home-dir /var/lib/mac --shell /usr/sbin/nologin mac && \
    mkdir -p /var/lib/mac && \
    chown -R mac:mac /var/lib/mac

COPY --from=builder /wheels/*.whl /tmp/
# Install with both optional extras so the single image serves every
# console-script the K8s topology needs:
#   [postgres]  PostgresStore (mac-api)
#   [k8s]       kubernetes client (mac-k8s-runner, mac-k8s-controller)
# Backend selection is runtime via MAC_DATABASE_URL / which entry point runs.
RUN WHEEL=$(ls /tmp/*.whl) && pip install --no-cache-dir "${WHEEL}[postgres,k8s]" && rm -f /tmp/*.whl

USER mac
WORKDIR /var/lib/mac
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); \
        sys.exit(0 if r.status == 200 else 1)" || exit 1

CMD ["uvicorn", "mac.api:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--log-level", "info"]
